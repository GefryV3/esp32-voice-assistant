// Отрисовка кадра полосами по 16 строк. Каждая полоса считается в 8-битный RGB: фон с
// «живым» цветом, сияние по краю, глаза, часы, надписи. Потом сводится к 16 битам с
// дизерингом (без ступенек на тёмных переходах) и уходит в экран по DMA. Полосы рисуют
// оба ядра сразу. Внешний вид — по макету design/screen-mockup.html.
#include <atomic>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <strings.h>

#include "display/lcd.h"
#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "face/face.h"
#include "face/text.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

namespace face {
namespace {

const char *TAG = "face";
constexpr int W = 240, H = 240, C = 120, SH = 16, NSTRIP = H / SH;
constexpr int NOUT = 4;  // буферов RGB565: в экран уходят по порядку, рисуют их два ядра
constexpr float TAU = 6.28318530718f, PI = 3.14159265359f;

// Полосу рисует то ядро, которое её взяло, поэтому «текущая полоса» у каждой задачи своя.
thread_local uint8_t *s_work = nullptr;  // полоса SH×W, RGB по байту
thread_local int s_y0 = 0, s_y1 = 0;     // её строки [s_y0, s_y1)
thread_local int s_worker = 0;           // 0 — ядро 1 (главное), 1 — помощник на ядре 0
uint8_t *s_works[2] = {nullptr, nullptr};
uint8_t *s_out[NOUT] = {};
uint16_t *s_qr = nullptr;  // четверть экрана 120×120: радиус ×64
uint8_t *s_qa = nullptr;   // и угол: 0…64 на четверть оборота
uint8_t s_lut5[16][256], s_lut6[16][256];  // дизеринг 8 → 5/6 бит, уже с учётом яркости
int s_lut_bright = -1;
float s_exp[258];

const uint8_t BAYER[16] = {0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5};

struct Rgb {
  float r, g, b;
};
const Rgb INK = {236, 240, 247};

inline float clamp01(float v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
inline float smooth(float t) {
  t = clamp01(t);
  return t * t * (3 - 2 * t);
}
// e^(−x) по таблице, x ≥ 0
inline float expn(float x) {
  if (x <= 0) return 1;
  if (x >= 8) return 0;
  const float f = x * 32;
  const int i = (int)f;
  return s_exp[i] + (s_exp[i + 1] - s_exp[i]) * (f - i);
}
// неотрицательное число → байт (быстрее u8, где знак заранее известен)
inline uint8_t u8f(float v) { return v >= 255 ? 255 : (uint8_t)(int)(v + 0.5f); }
inline uint8_t u8(float v) { return v <= 0 ? 0 : (v >= 255 ? 255 : (uint8_t)(v + 0.5f)); }
inline uint8_t *px(int x, int y) { return s_work + ((y - s_y0) * W + x) * 3; }
inline void add(uint8_t *p, float r, float g, float b) {
  p[0] = u8(p[0] + r);
  p[1] = u8(p[1] + g);
  p[2] = u8(p[2] + b);
}
inline void mix(uint8_t *p, const Rgb &c, float a) {
  p[0] = u8(p[0] + (c.r - p[0]) * a);
  p[1] = u8(p[1] + (c.g - p[1]) * a);
  p[2] = u8(p[2] + (c.b - p[2]) * a);
}
inline Rgb rgb(const float *c) { return {c[0], c[1], c[2]}; }

// Пересечение прямоугольника с текущей полосой; false — не пересекается.
bool clip(float fx0, float fy0, float fx1, float fy1, int &x0, int &y0, int &x1, int &y1) {
  x0 = fx0 < 0 ? 0 : (int)fx0;
  y0 = fy0 < s_y0 ? s_y0 : (int)fy0;
  x1 = fx1 > W - 1 ? W - 1 : (int)ceilf(fx1);
  y1 = fy1 > s_y1 - 1 ? s_y1 - 1 : (int)ceilf(fy1);
  return x0 <= x1 && y0 <= y1;
}

// ── Форма глаза: расстояние до края (signed distance), общая для свечения и тела ──

struct EyeGeo {
  float cx, cy, w, h, r, invW, bend, lidBase, lidK, Rb, cyc;
  bool lidT, lidB;
};

EyeGeo eye_geo(const Eye &e) {
  EyeGeo g;
  g.cx = e.cx;
  g.cy = e.cy;
  g.w = e.w;
  g.h = e.h;
  g.r = e.r;
  g.invW = 1 / e.w;
  g.bend = e.bend;
  g.lidT = e.lidT > 0.001f;
  g.lidBase = -e.h + e.lidT * 2 * e.h;
  g.lidK = fminf(1, e.lidT * 4) * e.lidA * e.side;
  g.lidB = e.lidB > 0.001f;
  g.Rb = e.w * 1.7f;
  g.cyc = e.h + g.Rb + 1.5f - e.lidB * e.h * 1.45f;
  return g;
}

// qx0, py0 — точка относительно центра глаза; py — та же точка после изгиба
inline float eye_sdf(const EyeGeo &g, float qx0, float py0, float &py) {
  float tt = qx0 * g.invW;
  tt = tt < -1 ? -1 : (tt > 1 ? 1 : tt);
  py = py0 - g.bend * (1 - tt * tt);
  const float qx = fabsf(qx0) - g.w + g.r, qy = fabsf(py) - g.h + g.r;
  float d;
  if (qx > 0) d = (qy > 0 ? sqrtf(qx * qx + qy * qy) : qx) - g.r;
  else d = (qy > 0 ? qy : fmaxf(qx, qy)) - g.r;
  if (g.lidT) {
    const float l = g.lidBase + g.lidK * qx0 - py;
    if (l > d) d = l;
  }
  if (g.lidB) {
    const float dy = py - g.cyc, c = g.Rb - sqrtf(qx0 * qx0 + dy * dy);
    if (c > d) d = c;
  }
  return d;
}

// ── Фон: сетка 31×31 узел через 8 px, между узлами — плавная интерполяция. В сетку же
//    входит мягкое свечение вокруг глаз: оно плавное, считать его на каждый пиксель незачем ──

constexpr int GS = 8, GN = W / GS + 1;
int16_t s_grid[GN][GN][3];  // цвет ×64, 0…255
float s_base[GN][GN];       // радиальный подъём фона в узле
float s_vign[GN][GN];       // затемнение к краю для живого фона

void init_grid() {
  for (int gy = 0; gy < GN; gy++)
    for (int gx = 0; gx < GN; gx++) {
      const float dx = gx * GS + 0.5f - C, dy = gy * GS + 0.5f - C, r = sqrtf(dx * dx + dy * dy);
      const float k = fmaxf(0, 1 - r / 124), rr = r < 120 ? r / 120 : 1;
      s_base[gy][gx] = k * k;
      s_vign[gy][gx] = 1 - 0.55f * rr * rr * rr;
    }
}

void build_grid(const Scene &s) {
  const float inv = 1.0f / (64 * 64);
  const bool aura = s.aura > 0.003f;
  float ac[3][3];
  for (int b = 0; b < 3; b++)
    for (int i = 0; i < 3; i++) ac[b][i] = s.aura_col[b][i] * s.aura;
  // где светятся глаза
  EyeGeo eg[2];
  // свечение в два слоя: плотное у края (glow_r) и широкое мягкое (glow_r2)
  float gamt[2], gamt2[2], gx0[2], gx1[2], gy0[2], gy1[2];
  const float invgR = 1 / s.glow_r, invgR2 = 1 / s.glow_r2;
  for (int e = 0; e < 2; e++) {
    eg[e] = eye_geo(s.eye[e]);
    gamt[e] = s.glow * s.eye[e].bright;
    gamt2[e] = s.glow2 * s.eye[e].bright;
    const float pad = fmaxf(s.glow_r, s.glow_r2) * 4.2f + GS, ab = fabsf(s.eye[e].bend);
    gx0[e] = eg[e].cx - eg[e].w - pad;
    gx1[e] = eg[e].cx + eg[e].w + pad;
    gy0[e] = eg[e].cy - eg[e].h - pad - ab;
    gy1[e] = eg[e].cy + eg[e].h + pad + ab;
  }
  for (int gy = 0; gy < GN; gy++) {
    const float y = gy * GS + 0.5f;
    for (int gx = 0; gx < GN; gx++) {
      const float x = gx * GS + 0.5f, k = s_base[gy][gx] * s.bg;
      float c[3] = {15 * k, 17 * k, 23 * k};
      if (aura) {
        const float v = s_vign[gy][gx];
        for (int b = 0; b < 3; b++) {
          const float ex = x - s.blob_x[b], ey = y - s.blob_y[b];
          float w = 1 / (1 + (ex * ex + ey * ey) * inv);
          w *= w * v;
          c[0] += ac[b][0] * w;
          c[1] += ac[b][1] * w;
          c[2] += ac[b][2] * w;
        }
      }
      float g = 0;
      for (int e = 0; e < 2; e++) {
        if (gamt[e] < 0.004f || x < gx0[e] || x > gx1[e] || y < gy0[e] || y > gy1[e]) continue;
        float py;
        const float d = eye_sdf(eg[e], x - eg[e].cx, y - eg[e].cy, py);
        g += gamt[e] * expn((d - 0.5f) * invgR) + gamt2[e] * expn((d - 0.5f) * invgR2);
      }
      if (g > 0.002f) {
        c[0] += s.glow_col[0] * g;
        c[1] += s.glow_col[1] * g;
        c[2] += s.glow_col[2] * g;
      }
      for (int i = 0; i < 3; i++) s_grid[gy][gx][i] = (int16_t)(c[i] >= 255 ? 255 * 64 : c[i] * 64);
    }
  }
}

static_assert(GS == 8, "fill_bg рассчитан на шаг сетки 8 px");

IRAM_ATTR void fill_bg() {
  for (int y = s_y0; y < s_y1; y++) {
    const int gy = y / GS, fy = y % GS;
    int32_t row[GN][3];  // ×512
    for (int gx = 0; gx < GN; gx++)
      for (int i = 0; i < 3; i++) row[gx][i] = s_grid[gy][gx][i] * (GS - fy) + s_grid[gy + 1][gx][i] * fy;
    uint8_t *p = px(0, y);
    for (int gx = 0; gx < GN - 1; gx++, p += 3 * GS) {
      for (int i = 0; i < 3; i++) {
        const int32_t d = row[gx + 1][i] - row[gx][i];
        int32_t q = row[gx][i] * GS;  // ×4096
        uint8_t *c = p + i;
        for (int k = 0; k < GS; k++, c += 3, q += d) *c = q >> 12;
      }
    }
  }
}

// ── Сияние по краю (слушает, думает, говорит) и янтарный пульс сработавшего таймера.
//    Яркость зависит только от угла и расстояния до края, поэтому раз в кадр считается
//    таблица «угол × расстояние», а пиксели берут из неё готовый цвет ──

constexpr int RING_E = 48;                 // сияние — до 48 px от края
constexpr int RING_A = 128;                // углов в таблице: шаг ~6 px по краю, цвет и ширина меняются плавнее
uint8_t (*s_polar)[RING_E + 2][3] = nullptr;  // добавка цвета: угол × расстояние до края 0…49 px (PSRAM)
bool s_ring_on = false;
float s_sin[257];
const float STOPS[4][3] = {{10, 132, 255}, {191, 90, 242}, {255, 55, 95}, {255, 159, 10}};

inline float fsin(float x) {  // синус по таблице
  float f = x * (256 / TAU);
  f -= floorf(f / 256) * 256;
  const int i = (int)f;
  return s_sin[i] + (s_sin[i + 1] - s_sin[i]) * (f - i);
}

float s_rim[RING_E + 2];  // тонкая яркая кромка у самого края: 0.6·e^(−e/1.6)

void build_ring(const Scene &s) {
  const bool ring = s.ring > 0.004f, alert = s.alert > 0.004f;
  s_ring_on = ring || alert;
  if (!s_ring_on) return;
  float ar[RING_E + 2], ag[RING_E + 2], ab[RING_E + 2];  // янтарный пульс от угла не зависит
  for (int e = 0; e < RING_E + 2; e++) {
    const float a = alert ? s.alert * s.pulse * expn(e / 13.0f) : 0;
    ar[e] = s.alert_col[0] * a;
    ag[e] = s.alert_col[1] * a;
    ab[e] = s.alert_col[2] * a;
  }
  for (int i = 0; i < RING_A; i++) {
    const float a = i * TAU / RING_A;
    float w = s.ring_w;
    if (!s.calm) w *= 0.78f + 0.22f * fsin(3 * a + s.t * 1.9f) + 0.14f * fsin(5 * a - s.t * 1.3f);
    const float winv = 1 / fmaxf(w, 1);
    float k = ring ? s.ring : 0;
    if (s.reveal < 0.99f) {  // начало разговора: сияние «наливается» снизу вверх по обе стороны
      float db = a - PI / 2;
      db = fabsf(db - TAU * floorf(db / TAU + 0.5f)) / PI;
      k *= clamp01((s.reveal * 1.25f - db) / 0.2f);
    }
    if (s.think > 0.004f) {  // «огонёк» бежит по кругу, пока думает
      float d = a - s.comet;
      d -= TAU * floorf(d / TAU + 0.5f);
      k *= (1 - s.think) + s.think * (0.1f + 0.9f * expn(d * d / 0.6f));
    }
    float h = (a + s.ring_rot) / TAU;
    h -= floorf(h);
    const float f = h * 4;
    const int j = (int)f;
    const float t = smooth(f - j);
    const float *A = STOPS[j & 3], *B = STOPS[(j + 1) & 3];
    const float cr = (A[0] + (B[0] - A[0]) * t) * k, cg = (A[1] + (B[1] - A[1]) * t) * k,
                cb = (A[2] + (B[2] - A[2]) * t) * k;
    // e^(−e·winv) по шагам: каждый следующий = предыдущий × q
    const float q = expn(winv);
    float v = 1;
    uint8_t *P = s_polar[i][0];
    for (int e = 0; e < RING_E + 2; e++, P += 3, v *= q) {
      const float I = v + s_rim[e];
      P[0] = u8f(cr * I + ar[e]);
      P[1] = u8f(cg * I + ag[e]);
      P[2] = u8f(cb * I + ab[e]);
    }
  }
}

// Угол пикселя 0…255 (0 — вправо, по часовой) по таблице четверти экрана
inline int angle8(int x, int y, int qa) {
  const int a = x >= C ? (y >= C ? qa : 256 - qa) : (y >= C ? 128 - qa : 128 + qa);
  return a & 255;
}

inline void add_sat(uint8_t *p, int c, int v) {
  const int s = p[c] + v;
  p[c] = s > 255 ? 255 : (uint8_t)s;
}

IRAM_ATTR void draw_ring() {
  if (!s_ring_on) return;
  constexpr float rmin = 120 - RING_E;
  for (int y = s_y0; y < s_y1; y++) {
    const int qy = y < C ? C - 1 - y : y - C;
    const float dyc = qy + 0.5f;
    const float in = dyc < rmin ? sqrtf(rmin * rmin - dyc * dyc) : 0;
    const int qmin = in > 0.5f ? (int)(in - 0.5f) : 0;
    for (int side = 0; side < 2; side++) {
      const int xa = side ? C + qmin : 0, xb = side ? W - 1 : C - 1 - qmin;
      uint8_t *p = px(xa, y);
      for (int x = xa; x <= xb; x++, p += 3) {
        const int qx = x < C ? C - 1 - x : x - C, i = qy * C + qx;
        const int rq = s_qr[i];  // радиус ×64
        if (rq >= 121 * 64) continue;
        const uint8_t *P = s_polar[angle8(x, y, s_qa[i]) * RING_A >> 8][0];
        const int eq = 120 * 64 - rq;  // расстояние до края ×64
        if (eq <= 0) {                 // самый край: сглаживание
          const int k = 121 * 64 - rq;
          for (int c = 0; c < 3; c++) add_sat(p, c, (P[c] * k) >> 6);
          continue;
        }
        const int ei = eq >> 6, ef = eq & 63;
        if (ei > RING_E) continue;
        const uint8_t *A = P + ei * 3, *B = A + 3;
        if (ei < 4) {  // у края яркость меняется быстро — между соседними значениями
          for (int c = 0; c < 3; c++) add_sat(p, c, A[c] + (((B[c] - A[c]) * ef) >> 6));
        } else if (A[0] | A[1] | A[2]) {
          for (int c = 0; c < 3; c++) add_sat(p, c, A[c]);
        }
      }
    }
  }
}

// ── Глаза: скруглённый прямоугольник, веки, изгиб, объём. Свечение уже в фоне ──

IRAM_ATTR void draw_eye(const Scene &s, const Eye &e) {
  const float w = e.w, h = e.h;
  if (h <= 0) return;
  const EyeGeo geo = eye_geo(e);
  const float ab = fabsf(e.bend);
  int x0, y0, x1, y1;
  if (!clip(e.cx - w - 1, e.cy - h - ab - 1, e.cx + w + 1, e.cy + h + ab + 1, x0, y0, x1, y1)) return;
  const Rgb col = rgb(s.eye_col), gc = rgb(s.glow_col);
  // край стекла подкрашен свечением и ловит цвет фона (accent) — как «жидкое стекло»
  const Rgb edge = {(gc.r + col.r) * 0.35f + s.accent[0] * 0.3f, (gc.g + col.g) * 0.35f + s.accent[1] * 0.3f,
                    (gc.b + col.b) * 0.35f + s.accent[2] * 0.3f};
  const float invDepth = 1 / fmaxf(2, fminf(w, h) * 0.8f);
  const float glint = s.glass ? 0.6f * clamp01((h - 10 * s.scale) / (14 * s.scale)) : 0;
  const float invGw = 1 / (w * 0.24f), invGh = 1 / (h * 0.15f);
  const float inv2h = 1 / (2 * h);
  for (int y = y0; y <= y1; y++) {
    const float py0 = y + 0.5f - e.cy;
    uint8_t *p = px(x0, y);
    for (int x = x0; x <= x1; x++, p += 3) {
      const float qx0 = x + 0.5f - e.cx;
      float py;
      const float d = eye_sdf(geo, qx0, py0, py);
      if (d > 0.5f) continue;
      const float a = fminf(1, 0.5f - d) * e.bright;
      const float k = 1.04f - 0.1f * clamp01((py + h) * inv2h);  // чуть светлее сверху
      Rgb f = {col.r * k, col.g * k, col.b * k};
      if (s.glass) {
        const float core = 0.25f + 0.75f * smooth(-d * invDepth);
        f = {edge.r + (f.r - edge.r) * core, edge.g + (f.g - edge.g) * core, edge.b + (f.b - edge.b) * core};
        if (glint > 0) {  // блик сверху слева
          const float u = (qx0 + w * 0.36f - e.gpx) * invGw, v = (py + h * 0.5f - e.gpy) * invGh;
          const float sg = (1 - u * u - v * v) * glint;
          if (sg > 0) f = {f.r + (255 - f.r) * sg, f.g + (255 - f.g) * sg, f.b + (255 - f.b) * sg};
        }
      }
      // смешивание в целых числах: дешевле, чем три перевода float → байт
      const int ai = (int)(a * 256), fr = (int)fminf(f.r, 255), fg = (int)fminf(f.g, 255), fb = (int)fminf(f.b, 255);
      p[0] = (uint8_t)(p[0] + (((fr - p[0]) * ai) >> 8));
      p[1] = (uint8_t)(p[1] + (((fg - p[1]) * ai) >> 8));
      p[2] = (uint8_t)(p[2] + (((fb - p[2]) * ai) >> 8));
    }
  }
}

// ── Сглаженные примитивы для значков ──

// Отрезок с круглыми концами
void capsule(float ax, float ay, float bx, float by, float wid, const Rgb &c, float alpha) {
  const float hw = wid / 2;
  int x0, y0, x1, y1;
  if (!clip(fminf(ax, bx) - hw - 1, fminf(ay, by) - hw - 1, fmaxf(ax, bx) + hw + 1, fmaxf(ay, by) + hw + 1, x0, y0, x1, y1))
    return;
  const float vx = bx - ax, vy = by - ay, len2 = vx * vx + vy * vy, inv = len2 > 0 ? 1 / len2 : 0;
  for (int y = y0; y <= y1; y++) {
    for (int x = x0; x <= x1; x++) {
      const float qx = x + 0.5f - ax, qy = y + 0.5f - ay, t = clamp01((qx * vx + qy * vy) * inv);
      const float dx = qx - vx * t, dy = qy - vy * t, d = sqrtf(dx * dx + dy * dy) - hw;
      const float a = clamp01(0.5f - d) * alpha;
      if (a > 0.003f) mix(px(x, y), c, a);
    }
  }
}

// Дуга окружности от a0 до a1 по часовой (как arc() в canvas), с круглыми концами
void arc(float cx, float cy, float R, float wid, float a0, float a1, const Rgb &c, float alpha) {
  const float hw = wid / 2;
  int x0, y0, x1, y1;
  if (!clip(cx - R - hw - 1, cy - R - hw - 1, cx + R + hw + 1, cy + R + hw + 1, x0, y0, x1, y1)) return;
  float sweep = a1 - a0;
  if (sweep <= 0) sweep += TAU;
  if (sweep > TAU) sweep = TAU;
  const float p0x = cx + R * cosf(a0), p0y = cy + R * sinf(a0);
  const float p1x = cx + R * cosf(a0 + sweep), p1y = cy + R * sinf(a0 + sweep);
  for (int y = y0; y <= y1; y++) {
    for (int x = x0; x <= x1; x++) {
      const float dx = x + 0.5f - cx, dy = y + 0.5f - cy, dist = sqrtf(dx * dx + dy * dy);
      if (fabsf(dist - R) > hw + 1) continue;
      float rel = atan2f(dy, dx) - a0;
      rel -= floorf(rel / TAU) * TAU;
      float d;
      if (rel <= sweep) {
        d = fabsf(dist - R) - hw;
      } else {
        const float ex = x + 0.5f - p0x, ey = y + 0.5f - p0y, fx = x + 0.5f - p1x, fy = y + 0.5f - p1y;
        d = sqrtf(fminf(ex * ex + ey * ey, fx * fx + fy * fy)) - hw;
      }
      const float a = clamp01(0.5f - d) * alpha;
      if (a > 0.003f) mix(px(x, y), c, a);
    }
  }
}

void disc(float cx, float cy, float R, const Rgb &c, float alpha) {
  int x0, y0, x1, y1;
  if (!clip(cx - R - 1, cy - R - 1, cx + R + 1, cy + R + 1, x0, y0, x1, y1)) return;
  for (int y = y0; y <= y1; y++)
    for (int x = x0; x <= x1; x++) {
      const float dx = x + 0.5f - cx, dy = y + 0.5f - cy;
      const float a = clamp01(R + 0.5f - sqrtf(dx * dx + dy * dy)) * alpha;
      if (a > 0.003f) mix(px(x, y), c, a);
    }
}

// Мягкое пятно: яркость падает к краю (как радиальный градиент)
void soft(float cx, float cy, float R, const Rgb &c, float alpha) {
  int x0, y0, x1, y1;
  if (!clip(cx - R, cy - R, cx + R, cy + R, x0, y0, x1, y1)) return;
  for (int y = y0; y <= y1; y++)
    for (int x = x0; x <= x1; x++) {
      const float dx = x + 0.5f - cx, dy = y + 0.5f - cy;
      const float a = (1 - sqrtf(dx * dx + dy * dy) / R) * alpha;
      if (a > 0.003f) mix(px(x, y), c, a);
    }
}

// Полумесяц: круг без смещённого круга
void crescent(float cx, float cy, float R, float ox, float oy, float R2, const Rgb &c, float alpha) {
  int x0, y0, x1, y1;
  if (!clip(cx - R - 1, cy - R - 1, cx + R + 1, cy + R + 1, x0, y0, x1, y1)) return;
  for (int y = y0; y <= y1; y++)
    for (int x = x0; x <= x1; x++) {
      const float dx = x + 0.5f - cx, dy = y + 0.5f - cy, ex = dx - ox, ey = dy - oy;
      const float d = fmaxf(sqrtf(dx * dx + dy * dy) - R, R2 - sqrtf(ex * ex + ey * ey));
      const float a = clamp01(0.5f - d) * alpha;
      if (a > 0.003f) mix(px(x, y), c, a);
    }
}

// ── Текст ──

// Глиф с масштабом и поворотом; (ox, oy) — точка на базовой линии слева от символа.
void glyph(const text::Font &f, const text::Glyph *g, float ox, float oy, float sc, float cs, float sn, const Rgb &c,
           float alpha) {
  if (!g || !g->w || !g->h || alpha < 0.003f) return;
  if (sc == 1 && cs == 1 && sn == 0 && ox == floorf(ox) && oy == floorf(oy)) {  // без масштаба — копия маски
    const int bx = (int)ox + g->x, by = (int)oy + g->y;
    const int ya = by > s_y0 ? by : s_y0, yb = by + g->h < s_y1 ? by + g->h : s_y1;
    const int xa = bx > 0 ? bx : 0, xb = bx + g->w < W ? bx + g->w : W;
    const float k = alpha / 255;
    for (int y = ya; y < yb; y++) {
      const uint8_t *src = f.bits + g->off + (y - by) * g->w + (xa - bx);
      uint8_t *p = px(xa, y);
      for (int x = xa; x < xb; x++, p += 3, src++)
        if (*src) mix(p, c, *src * k);
    }
    return;
  }
  const float gx0 = g->x * sc, gy0 = g->y * sc, gx1 = (g->x + g->w) * sc, gy1 = (g->y + g->h) * sc;
  const float xs[4] = {gx0, gx1, gx0, gx1}, ys[4] = {gy0, gy0, gy1, gy1};
  float minx = 1e9f, miny = 1e9f, maxx = -1e9f, maxy = -1e9f;
  for (int k = 0; k < 4; k++) {
    const float X = ox + xs[k] * cs - ys[k] * sn, Y = oy + xs[k] * sn + ys[k] * cs;
    minx = fminf(minx, X);
    maxx = fmaxf(maxx, X);
    miny = fminf(miny, Y);
    maxy = fmaxf(maxy, Y);
  }
  int x0, y0, x1, y1;
  if (!clip(minx - 1, miny - 1, maxx + 1, maxy + 1, x0, y0, x1, y1)) return;
  const float inv = 1 / sc;
  const uint8_t *bm = f.bits + g->off;
  const int gw = g->w, gh = g->h;
  auto at = [&](int u, int v) -> float {
    return (u < 0 || v < 0 || u >= gw || v >= gh) ? 0.0f : (float)bm[v * gw + u];
  };
  for (int y = y0; y <= y1; y++) {
    for (int x = x0; x <= x1; x++) {
      const float dx = x + 0.5f - ox, dy = y + 0.5f - oy;
      const float u = (dx * cs + dy * sn) * inv - g->x - 0.5f, v = (-dx * sn + dy * cs) * inv - g->y - 0.5f;
      if (u <= -1 || v <= -1 || u >= gw || v >= gh) continue;
      const int iu = (int)floorf(u), iv = (int)floorf(v);
      const float fu = u - iu, fv = v - iv;
      const float sm = (at(iu, iv) * (1 - fu) + at(iu + 1, iv) * fu) * (1 - fv) +
                       (at(iu, iv + 1) * (1 - fu) + at(iu + 1, iv + 1) * fu) * fv;
      const float a = sm * (1 / 255.0f) * alpha;
      if (a > 0.003f) mix(px(x, y), c, a);
    }
  }
}

// Строка; center — по центру от x, иначе от x вправо; tracking — разрядка. Возвращает ширину.
float draw_text(const text::Font &f, const char *s, float x, float base, float sc, bool center, const Rgb &c,
                float alpha, float tracking = 0) {
  if (base - f.size > s_y1 || base + f.size * 0.4f < s_y0) return 0;  // строка не в этой полосе
  const float w = text::width(f, s, tracking) * sc;
  float pen = center ? x - w / 2 : x;
  for (uint32_t cp; (cp = text::next(s));) {
    const text::Glyph *g = text::find(f, cp);
    if (!g) continue;
    if (sc == 1) glyph(f, g, roundf(pen), roundf(base), 1, 1, 0, c, alpha);
    else glyph(f, g, pen, base, sc, 1, 0, c, alpha);
    pen += (g->adv / 16.0f + tracking) * sc;
  }
  return w;
}

// Обрезать строку по ширине, добавив «…»
void fit(const text::Font &f, const char *s, float maxw, char *out, size_t n) {
  if (text::width(f, s) <= maxw) {
    snprintf(out, n, "%s", s);
    return;
  }
  const float ell = text::width(f, "\xE2\x80\xA6");
  float w = 0;
  size_t len = 0;
  for (const char *p = s; *p;) {
    const char *start = p;
    const uint32_t cp = text::next(p);
    const text::Glyph *g = text::find(f, cp);
    w += g ? g->adv / 16.0f : 0;
    if (w + ell > maxw) break;
    len = (size_t)(p - s);
    (void)start;
  }
  if (len + 4 > n) len = n - 4;
  memcpy(out, s, len);
  while (len && out[len - 1] == ' ') len--;
  memcpy(out + len, "\xE2\x80\xA6", 4);  // вместе с нулём в конце
}

// Заглавные буквы для латиницы и кириллицы (UTF-8)
void upper(const char *s, char *out, size_t n) {
  size_t i = 0;
  for (const uint8_t *p = (const uint8_t *)s; *p && i + 2 < n; p++) {
    uint8_t c = *p;
    if (c >= 'a' && c <= 'z') {
      out[i++] = (char)(c - 32);
    } else if (c == 0xD0 && p[1] >= 0xB0 && p[1] <= 0xBF) {  // а…п
      out[i++] = (char)0xD0;
      out[i++] = (char)(p[1] - 0x20);
      p++;
    } else if (c == 0xD1 && p[1] >= 0x80 && p[1] <= 0x8F) {  // р…я
      out[i++] = (char)0xD0;
      out[i++] = (char)(p[1] + 0x20);
      p++;
    } else if (c == 0xD1 && p[1] == 0x91) {  // ё
      out[i++] = (char)0xD0;
      out[i++] = (char)0x81;
      p++;
    } else {
      out[i++] = (char)c;
    }
  }
  out[i] = 0;
}

// Цифры одинаковой ширины по центру (cx, cy) — часы и таймер
void draw_digits(const text::Font &f, const char *s, float cx, float cy, float size, const Rgb &c, float alpha) {
  float sc = size / f.size;
  const bool exact = fabsf(sc - 1) < 0.01f;  // родной кегль — буквы копируются без пересчёта
  if (exact) {
    sc = 1;
    cx = roundf(cx);
    cy = roundf(cy);
  }
  const float cell = text::digit_cell(f) * sc;
  const text::Glyph *colon = text::find(f, ':');
  const float cw = (colon ? colon->adv / 16.0f : 0) * 1.1f * sc;
  float total = 0;
  for (const char *p = s; *p; p++) total += *p == ':' ? cw : cell;
  float x = cx - total / 2;
  const float base = cy + f.cap * sc / 2;
  for (const char *p = s; *p; p++) {
    const bool is_colon = *p == ':';
    const float w = is_colon ? cw : cell;
    if (const text::Glyph *g = text::find(f, (uint8_t)*p)) {
      const float gw = g->adv / 16.0f * sc;
      float gx = x + (w - gw) / 2, gy = is_colon ? base - size * 0.05f : base;
      if (exact) {
        gx = roundf(gx);
        gy = roundf(gy);
      }
      glyph(f, g, gx, gy, sc, 1, 0, c, alpha);
    }
    x += w;
  }
}

// ── Надписи по дуге. Буквы в них повёрнуты, поэтому растеризуются заранее в маленькие
//    плитки — заново, только когда меняется текст (раз в сутки или по команде сервера) ──

struct Tile {
  int16_t x, y;
  uint8_t w, h;
  uint16_t off;
  bool accent;  // цветом времени суток, иначе белым
  float k;      // своя прозрачность
};
constexpr int MAX_TILES = 64, TILE_BYTES = 16384;
Tile s_tiles[MAX_TILES];
int s_ntiles = 0, s_tile_used = 0;
uint8_t *s_tile_bits = nullptr;
char s_arc_key[256] = "\x01";  // из какого текста построены плитки

void add_tile(const text::Font &f, const text::Glyph *g, float ox, float oy, float cs, float sn, bool accent,
              float k) {
  if (!g || !g->w || !g->h || s_ntiles == MAX_TILES) return;
  const float gx0 = g->x, gy0 = g->y, gx1 = g->x + g->w, gy1 = g->y + g->h;
  const float xs[4] = {gx0, gx1, gx0, gx1}, ys[4] = {gy0, gy0, gy1, gy1};
  float minx = 1e9f, miny = 1e9f, maxx = -1e9f, maxy = -1e9f;
  for (int i = 0; i < 4; i++) {
    const float X = ox + xs[i] * cs - ys[i] * sn, Y = oy + xs[i] * sn + ys[i] * cs;
    minx = fminf(minx, X);
    maxx = fmaxf(maxx, X);
    miny = fminf(miny, Y);
    maxy = fmaxf(maxy, Y);
  }
  const int x0 = (int)fmaxf(0, floorf(minx) - 1), y0 = (int)fmaxf(0, floorf(miny) - 1);
  const int x1 = (int)fminf(W - 1, ceilf(maxx) + 1), y1 = (int)fminf(H - 1, ceilf(maxy) + 1);
  const int tw = x1 - x0 + 1, th = y1 - y0 + 1;
  if (tw <= 0 || th <= 0 || tw > 255 || th > 255 || s_tile_used + tw * th > TILE_BYTES) return;
  const uint8_t *bm = f.bits + g->off;
  const int gw = g->w, gh = g->h;
  auto at = [&](int u, int v) -> float {
    return (u < 0 || v < 0 || u >= gw || v >= gh) ? 0.0f : (float)bm[v * gw + u];
  };
  uint8_t *dst = s_tile_bits + s_tile_used;
  for (int y = y0; y <= y1; y++)
    for (int x = x0; x <= x1; x++, dst++) {
      const float dx = x + 0.5f - ox, dy = y + 0.5f - oy;
      const float u = dx * cs + dy * sn - g->x - 0.5f, v = -dx * sn + dy * cs - g->y - 0.5f;
      const int iu = (int)floorf(u), iv = (int)floorf(v);
      const float fu = u - iu, fv = v - iv;
      const float sm = (at(iu, iv) * (1 - fu) + at(iu + 1, iv) * fu) * (1 - fv) +
                       (at(iu, iv + 1) * (1 - fu) + at(iu + 1, iv + 1) * fu) * fv;
      *dst = (uint8_t)(sm + 0.5f);
    }
  s_tiles[s_ntiles++] = {(int16_t)x0, (int16_t)y0, (uint8_t)tw, (uint8_t)th, (uint16_t)s_tile_used, accent, k};
  s_tile_used += tw * th;
}

// Сверху — по часовой, снизу — против (чтобы читалось слева направо)
struct Part {
  const char *s;
  bool accent;
};
void layout_arc(const Part *parts, int n, float r, bool bottom, float k) {
  const text::Font &f = text::kCaps;
  const float spc = f.size * 0.12f;
  float total = -spc;
  for (int i = 0; i < n; i++) {
    const char *s = parts[i].s;
    for (uint32_t cp; (cp = text::next(s));)
      if (const text::Glyph *g = text::find(f, cp)) total += g->adv / 16.0f + spc;
  }
  if (total <= 0) return;
  float th = bottom ? PI / 2 + total / 2 / r : -PI / 2 - total / 2 / r;
  for (int i = 0; i < n; i++) {
    const char *s = parts[i].s;
    for (uint32_t cp; (cp = text::next(s));) {
      const text::Glyph *g = text::find(f, cp);
      if (!g) continue;
      const float w = g->adv / 16.0f, mid = bottom ? th - w / 2 / r : th + w / 2 / r;
      const float rot = bottom ? mid - PI / 2 : mid + PI / 2, cs = cosf(rot), sn = sinf(rot);
      add_tile(f, g, C + r * cosf(mid) - w / 2 * cs, C + r * sinf(mid) - w / 2 * sn, cs, sn, parts[i].accent, k);
      th = bottom ? th - (w + spc) / r : th + (w + spc) / r;
    }
  }
}

// Перенос по словам: не больше двух строк
int wrap2(const text::Font &f, const char *s, float maxw, char out[2][128], bool *more = nullptr) {
  out[0][0] = out[1][0] = 0;
  int line = 0;
  size_t len = 0;
  while (*s && line < 2) {
    while (*s == ' ') s++;
    const char *e = s;
    while (*e && *e != ' ') e++;
    char cand[128];
    const size_t wl = (size_t)(e - s);
    if (len + 1 + wl >= sizeof cand) break;
    memcpy(cand, out[line], len);
    size_t cl = len;
    if (cl) cand[cl++] = ' ';
    memcpy(cand + cl, s, wl);
    cl += wl;
    cand[cl] = 0;
    if (len && text::width(f, cand) > maxw) {
      line++;
      len = 0;
      continue;
    }
    memcpy(out[line], cand, cl + 1);
    len = cl;
    s = e;
  }
  while (*s == ' ') s++;
  if (more) *more = *s != 0;
  return out[1][0] ? 2 : (out[0][0] ? 1 : 0);
}

// Две строки; если текст не влез — многоточие в конце второй
int wrap2_fit(const text::Font &f, const char *s, float maxw, char out[2][128]) {
  bool more = false;
  const int n = wrap2(f, s, maxw, out, &more);
  if (more && n > 0) {
    char tmp[136];
    snprintf(tmp, sizeof tmp, "%s\xE2\x80\xA6", out[n - 1]);
    fit(f, tmp, maxw, out[n - 1], 128);
  }
  return n;
}

// ── Значки ──

void sun(float x, float y, float s, float alpha) {
  const Rgb c = {255, 204, 0};
  arc(x, y, 4.2f * s, 1.7f, 0, TAU, c, alpha);
  for (int i = 0; i < 8; i++) {
    const float a = i * TAU / 8, cs = cosf(a), sn = sinf(a);
    capsule(x + cs * 7 * s, y + sn * 7 * s, x + cs * 9.4f * s, y + sn * 9.4f * s, 1.7f, c, alpha);
  }
}

void cloud(float x, float y, float alpha) {
  const Rgb dark = {0, 0, 0}, line = {198, 206, 220};
  // силуэт, чтобы спрятать солнце за облаком
  disc(x - 5, y + 1, 4, dark, .85f * alpha);
  disc(x + 0.5f, y - 2, 5.5f, dark, .85f * alpha);
  disc(x + 6, y + 1.5f, 3.5f, dark, .85f * alpha);
  capsule(x - 5, y + 3, x + 6, y + 3, 4, dark, .85f * alpha);
  arc(x - 5, y + 1, 4, 1.7f, PI * 0.5f, PI * 1.5f, line, alpha);
  arc(x + 0.5f, y - 2, 5.5f, 1.7f, PI * 1.08f, PI * 1.98f, line, alpha);
  arc(x + 6, y + 1.5f, 3.5f, 1.7f, PI * 1.5f, PI * 0.5f, line, alpha);
  capsule(x - 5, y + 5, x + 6, y + 5, 1.7f, line, alpha);
}

// Погода: значок и температура сверху. cond: 0 ясно, 1 переменная облачность, 2 облачно,
// 3 дождь, 4 снег, 5 гроза, 6 туман, 7 ясная ночь.
void draw_weather(const Scene &s) {
  const float a = s.wx_a;
  if (a < 0.01f || s.wcond < 0) return;
  char label[24];
  if (s.wtemp < 0) snprintf(label, sizeof label, "\xE2\x88\x92%d\xC2\xB0", -s.wtemp);  // «−5°»
  else snprintf(label, sizeof label, "%d\xC2\xB0", s.wtemp);
  const float tw = text::width(text::kUi, label), start = C - (22 + 6 + tw) / 2, x = start + 11, y = 43;
  int x0, y0, x1, y1;
  if (!clip(start - 4, y - 16, start + 32 + tw, y + 16, x0, y0, x1, y1)) return;
  const int cond = s.wcond;
  if (cond == 0) {
    sun(x, y, 1, a);
  } else if (cond == 7) {
    crescent(x, y, 6.5f, 3.5f, -2.5f, 5.2f, {224, 232, 255}, a);
  } else if (cond == 6) {
    const Rgb fog = {198, 206, 220};
    capsule(x - 8, y - 4, x + 7, y - 4, 1.7f, fog, a);
    capsule(x - 6, y, x + 9, y, 1.7f, fog, a);
    capsule(x - 8, y + 4, x + 5, y + 4, 1.7f, fog, a);
  } else {
    if (cond == 1) sun(x - 4, y - 4, 0.75f, a);
    const float cy = cond == 1 ? y + 1 : y - 2;
    cloud(x + (cond == 1 ? 1.5f : 0), cy, a);
    if (cond == 3) {
      for (int i = -1; i <= 1; i++) capsule(x + i * 4.5f, cy + 8.5f, x + i * 4.5f - 1.6f, cy + 12, 1.7f, {100, 170, 255}, a);
    } else if (cond == 4) {
      for (int i = -1; i <= 1; i++) disc(x + i * 4.5f, cy + 10, 1.3f, {230, 238, 250}, a);
    } else if (cond == 5) {
      const Rgb bolt = {255, 214, 10};
      capsule(x + 1, cy + 6, x - 2, cy + 10, 1.7f, bolt, a);
      capsule(x - 2, cy + 10, x + 2, cy + 10, 1.7f, bolt, a);
      capsule(x + 2, cy + 10, x - 1, cy + 14, 1.7f, bolt, a);
    }
  }
  draw_text(text::kUi, label, start + 28, y + 6, 1, false, rgb(s.ink), a);
}

void draw_clock(const Scene &s) {
  if (s.clock_a < 0.01f || !s.have_time) return;
  const float half = s.clock_size * 0.6f;
  if (s.clock_y + half < s_y0 || s.clock_y - half > s_y1) return;
  char t[24];
  snprintf(t, sizeof t, "%d:%02d", s.hour, s.minute);
  draw_digits(text::kClock, t, C, s.clock_y, s.clock_size, rgb(s.ink), s.clock_a);
}

const char *const DAYS[7] = {"ВС", "ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ"};
const char *const MONTHS[12] = {"ЯНВАРЯ", "ФЕВРАЛЯ", "МАРТА", "АПРЕЛЯ", "МАЯ", "ИЮНЯ",
                                "ИЮЛЯ", "АВГУСТА", "СЕНТЯБРЯ", "ОКТЯБРЯ", "НОЯБРЯ", "ДЕКАБРЯ"};

// Сверху по дуге — день и дата, снизу — ближайшее дело. Перестраивается при смене текста.
void build_arcs(const Scene &s) {
  if (!s_tile_bits) return;
  char key[256];
  snprintf(key, sizeof key, "%d|%d|%d|%d|%s|%s", s.have_time, s.wday, s.mday, s.month, s.agenda_accent,
           s.agenda_text);
  if (strcmp(key, s_arc_key) == 0) return;
  strcpy(s_arc_key, key);
  s_ntiles = s_tile_used = 0;
  if (s.have_time) {
    char date[64];
    snprintf(date, sizeof date, " \xC2\xB7 %d %s", s.mday, MONTHS[s.month % 12]);
    const Part top[2] = {{DAYS[s.wday % 7], true}, {date, false}};
    layout_arc(top, 2, 94, false, 0.95f);
  }
  if (s.agenda_accent[0] || s.agenda_text[0]) {
    char rest[96];
    snprintf(rest, sizeof rest, "%s%s", s.agenda_accent[0] && s.agenda_text[0] ? " " : "", s.agenda_text);
    const Part bottom[2] = {{s.agenda_accent, true}, {rest, false}};
    layout_arc(bottom, 2, 102, true, 0.8f);
  }
}

void draw_arcs(const Scene &s) {
  const float a = s.arc_a;
  if (a < 0.01f) return;
  const Rgb acc = rgb(s.accent);
  for (int i = 0; i < s_ntiles; i++) {
    const Tile &t = s_tiles[i];
    if (t.y >= s_y1 || t.y + t.h <= s_y0) continue;
    const Rgb &c = t.accent ? acc : INK;
    const float k = a * t.k / 255;
    const int ya = t.y > s_y0 ? t.y : s_y0, yb = t.y + t.h < s_y1 ? t.y + t.h : s_y1;
    for (int y = ya; y < yb; y++) {
      const uint8_t *src = s_tile_bits + t.off + (y - t.y) * t.w;
      uint8_t *p = px(t.x, y);
      for (int x = 0; x < t.w; x++, p += 3)
        if (src[x]) mix(p, c, src[x] * k);
    }
  }
}

void bulb(float x, float y, float alpha) {
  const Rgb c = {255, 204, 0};
  soft(x, y, 16, c, 0.35f * alpha);
  arc(x, y - 2, 5.5f, 1.7f, PI * 0.8f, PI * 2.2f, c, alpha);
  capsule(x + 4.45f, y + 1.23f, x + 2.5f, y + 6, 1.7f, c, alpha);
  capsule(x + 2.5f, y + 6, x - 2.5f, y + 6, 1.7f, c, alpha);
  capsule(x - 2.5f, y + 6, x - 4.45f, y + 1.23f, 1.7f, c, alpha);
  capsule(x - 2.5f, y + 9, x + 2.5f, y + 9, 1.7f, c, alpha);
}

// Низ экрана в разговоре: что слышит, что понял, что сделал
void draw_caption(const Scene &s) {
  const float a = s.cap_a;
  if (a < 0.01f || s.caption == Caption::None || !s.caption_text[0] || s_y1 < 145) return;
  char lines[2][128];
  const int n = wrap2(text::kUi, s.caption_text, 168, lines);
  const float lift = (1 - a) * 6;
  if (s.caption == Caption::Reply) {
    float y0 = n > 1 ? 176 : 184;
    if (s.icon == Icon::Light) {
      bulb(C, 160 + lift, a);
      y0 = 190;
    }
    for (int i = 0; i < n; i++) draw_text(text::kUi, lines[i], C, y0 + i * 19 + lift, 1, true, rgb(s.ink), a);
  } else {
    const float k = s.caption == Caption::Heard ? 0.85f : 0.45f;
    const float y0 = n > 1 ? 180 : 188;
    for (int i = 0; i < n; i++) draw_text(text::kUi, lines[i], C, y0 + i * 19 + lift, 1, true, rgb(s.ink), a * k);
  }
}

// Кольцо таймера по краю: остаток — оранжевым, по часовой от верха
// Кольцо по краю: дорожка (track — её прозрачность) и дуга от верха по часовой на долю frac.
// Из него же — таймер, плеер и волны звонка.
void ring_band(float R, float hw, float frac, const Rgb &o, float alpha, float track) {
  if (alpha < 0.004f) return;
  const float ex = C + R * sinf(frac * TAU), ey = C - R * cosf(frac * TAU);
  const int fq = (int)(frac * 256);
  for (int y = s_y0; y < s_y1; y++) {
    const int qy = y < C ? C - 1 - y : y - C;
    const float dyc = qy + 0.5f, lo = R - hw - 1, hi = R + hw + 1;
    if (dyc > hi) continue;
    const int qmin = dyc < lo ? (int)sqrtf(lo * lo - dyc * dyc) : 0;
    const int qmax = (int)ceilf(sqrtf(hi * hi - dyc * dyc));
    for (int side = 0; side < 2; side++) {
      for (int q = qmin; q <= qmax && q < C; q++) {
        const int x = side ? C + q : C - 1 - q;
        const int i = qy * C + q;
        const float r = s_qr[i] * (1.0f / 64), dr = fabsf(r - R);
        if (dr > hw + 1) continue;
        uint8_t *p = px(x, y);
        if (track > 0) mix(p, o, clamp01(0.5f - (dr - hw)) * track * alpha);  // дорожка
        if (frac <= 0.002f) continue;
        const float dx = x + 0.5f - C, dy = y + 0.5f - C;
        // угол от верха по часовой: грубо по таблице, точно — только у концов дуги
        const int u8 = (angle8(x, y, s_qa[i]) + 64) & 255;
        bool inside;
        if (frac >= 0.999f) {
          inside = true;
        } else if (abs(u8 - fq) <= 1 || u8 <= 1 || u8 >= 255) {
          float u = atan2f(dx, -dy) / TAU;
          if (u < 0) u += 1;
          inside = u <= frac;
        } else {
          inside = u8 < fq;
        }
        float d;
        if (inside) {
          d = dr - hw;
        } else {
          const float sx = dx, sy = dy + R, fx = x + 0.5f - ex, fy = y + 0.5f - ey;
          d = sqrtf(fminf(sx * sx + sy * sy, fx * fx + fy * fy)) - hw;
        }
        const float k = clamp01(0.5f - d) * alpha;
        if (k > 0.003f) mix(p, o, k);
      }
    }
  }
}

void timer_ring(float frac, float alpha) { ring_band(110, 3, frac, {255, 159, 10}, alpha, 0.2f); }

void draw_timer(const Scene &s) {
  const float a = s.timer_a;
  if (a < 0.01f) return;
  timer_ring(s.alert_mode ? 0 : (float)s.timer_rem / s.timer_total, a);
  if (s_y1 < 100 || s_y0 > 180) return;
  const uint32_t secs = (s.timer_rem + 999) / 1000;
  char t[40];
  if (secs >= 3600) snprintf(t, sizeof t, "%u:%02u:%02u", (unsigned)(secs / 3600), (unsigned)(secs / 60 % 60), (unsigned)(secs % 60));
  else snprintf(t, sizeof t, "%u:%02u", (unsigned)(secs / 60), (unsigned)(secs % 60));
  draw_digits(text::kTimer, t, C, 130, secs >= 3600 ? 38 : 50, rgb(s.ink), a * s.timer_pulse);
  const char *label = s.alert_mode ? "Готово" : s.timer_label;
  draw_text(text::kUi, label, C, 172, 1, true, {255, 159, 10}, a);
}

void draw_zs(const Scene &s) {
  const text::Glyph *g = text::find(text::kUi, 'z');
  for (int i = 0; i < s.nz; i++) {
    const Scene::Z &z = s.z[i];
    if (!g) break;
    const float sc = z.size / text::kUi.size, gw = g->adv / 16.0f * sc;
    glyph(text::kUi, g, z.x - gw / 2, z.y, sc, 1, 0, rgb(s.ink), z.a);
  }
}

// Нет связи с сервером: перечёркнутый значок Wi-Fi слева от погоды
void draw_offline(const Scene &s) {
  if (!s.offline || s_y0 > 64 || s_y1 < 36) return;
  const float x = C - 46, y = 50;
  const Rgb red = {255, 69, 58};
  arc(x, y, 4.5f, 1.6f, PI * 1.25f, PI * 1.75f, red, 1);
  arc(x, y, 8.5f, 1.6f, PI * 1.25f, PI * 1.75f, red, 1);
  disc(x, y - 0.5f, 1.4f, red, 1);
  capsule(x - 7, y - 11, x + 7, y + 1, 1.6f, red, 1);
}

// ── Карточки с телефона и мини-плеер (без подложек — как короткий взгляд на Apple Watch) ──

// Скруглённый прямоугольник: stroke > 0 — контур этой толщины, иначе заливка
void rrect(float cx, float cy, float hw, float hh, float r, const Rgb &c, float alpha, float stroke) {
  int x0, y0, x1, y1;
  const float pad = stroke + 1;
  if (!clip(cx - hw - pad, cy - hh - pad, cx + hw + pad, cy + hh + pad, x0, y0, x1, y1)) return;
  for (int y = y0; y <= y1; y++)
    for (int x = x0; x <= x1; x++) {
      const float qx = fabsf(x + 0.5f - cx) - hw + r, qy = fabsf(y + 0.5f - cy) - hh + r;
      const float ox = qx > 0 ? qx : 0, oy = qy > 0 ? qy : 0;
      float d = sqrtf(ox * ox + oy * oy) + fminf(fmaxf(qx, qy), 0) - r;
      if (stroke > 0) d = fabsf(d) - stroke / 2;
      const float a = clamp01(0.5f - d) * alpha;
      if (a > 0.003f) mix(px(x, y), c, a);
    }
}

void bell_icon(float x, float y, const Rgb &c, float a) {
  capsule(x - 6, y + 4, x - 5, y - 1, 1.6f, c, a);
  arc(x, y - 1, 5, 1.6f, PI, TAU, c, a);
  capsule(x + 5, y - 1, x + 6, y + 4, 1.6f, c, a);
  capsule(x - 6, y + 4, x + 6, y + 4, 1.6f, c, a);
  capsule(x - 2, y + 7, x + 2, y + 7, 1.6f, c, a);
}

void phone_icon(float x, float y, const Rgb &c, float a) {
  rrect(x, y, 4.5f, 7.5f, 2.5f, c, a, 1.6f);
  capsule(x - 1.5f, y + 5, x + 1.5f, y + 5, 1.6f, c, a);
}

void buds_icon(float x, float y, const Rgb &c, float a) {
  for (int s = -1; s <= 1; s += 2) {
    arc(x + s * 4, y - 3, 3, 1.6f, 0, TAU, c, a);
    capsule(x + s * 4, y, x + s * 4, y + 7, 1.6f, c, a);
  }
}

void laptop_icon(float x, float y, const Rgb &c, float a) {
  rrect(x, y - 2, 7, 5, 1.5f, c, a, 1.6f);
  capsule(x - 9, y + 5, x + 9, y + 5, 1.6f, c, a);
}

// Кружок приложения с первой буквой названия
void app_badge(float x, float y, const char *app, float a) {
  struct Known {
    const char *name;
    Rgb c;
  };
  static const Known KNOWN[] = {{"telegram", {42, 171, 238}}, {"whatsapp", {37, 211, 102}},
                                {"instagram", {225, 48, 108}}, {"gmail", {234, 67, 53}},
                                {"discord", {88, 101, 242}},    {"youtube", {255, 0, 51}}};
  Rgb c = {142, 142, 147};
  for (const Known &k : KNOWN)
    if (strcasecmp(app, k.name) == 0) c = k.c;
  disc(x, y, 11, c, a);
  char up[8];
  const char *p = app;
  const uint32_t first = text::next(p);
  if (!first) return;
  char one[5] = {};
  memcpy(one, app, (size_t)(p - app) < 4 ? (size_t)(p - app) : 4);
  upper(one, up, sizeof up);
  draw_text(text::kCaps, up, x, y + 4.5f, 1, true, {255, 255, 255}, a);
}

// Строка проверки перед сном: значок по первому слову и текст с заглавной
void check_row(const Scene &s, const char *row, float y, float a) {
  const Rgb acc = rgb(s.accent);
  if (strncmp(row, "будильник", 18) == 0) bell_icon(66, y - 4, acc, a);
  else if (strncmp(row, "телефон", 14) == 0) phone_icon(66, y - 5, acc, a);
  else if (strncmp(row, "ноутбук", 14) == 0) laptop_icon(66, y - 4, acc, a);
  else buds_icon(66, y - 4, acc, a);
  char cap[96], first[8], tmp[5] = {};
  const char *p = row;
  text::next(p);
  const size_t fl = (size_t)(p - row) < 4 ? (size_t)(p - row) : 4;
  memcpy(tmp, row, fl);
  upper(tmp, first, sizeof first);
  snprintf(cap, sizeof cap, "%s%s", first, row + fl);
  char line[96];
  fit(text::kUi, cap, 132, line, sizeof line);
  draw_text(text::kUi, line, 82, y + 1, 1, false, rgb(s.ink), a * 0.9f);
}

void draw_player(const Scene &s, float a) {
  const Rgb acc = rgb(s.accent), ink = rgb(s.ink);
  ring_band(113, 1.5f, s.player_frac, acc, a, 0.18f);
  if (s_y1 < 135 || s_y0 > 205) return;
  const float lift = (1 - a) * 14;
  char line[96];
  fit(text::kUiBold, s.player_title, 170, line, sizeof line);
  draw_text(text::kUiBold, line, C, 154 + lift, 1, true, ink, a);
  fit(text::kUi, s.player_artist, 170, line, sizeof line);
  draw_text(text::kUi, line, C, 174 + lift, 1, true, ink, a * 0.55f);
  for (int i = 0; i < 3; i++) {  // эквалайзер
    const float h = s.player_playing ? 4 + 9 * fabsf(sinf(s.t * (3.1f + i * 1.7f) + i * 1.3f)) : 3;
    const float x = C - 7 + i * 7;
    capsule(x, 200 + lift - h + 2, x, 200 + lift - 2, 4, acc, a);
  }
}

void draw_card(const Scene &s) {
  const float a = s.card_a;
  if (a < 0.01f) return;
  if (s.player) {
    draw_player(s, a);
    return;
  }
  const Rgb ink = rgb(s.ink), acc = rgb(s.accent);
  const float lift = (1 - a) * 14;
  char lines[2][128];
  char line[128];
  if (s.card == Card::Call) {  // волны от центра — их видно по всему экрану
    const Rgb green = {48, 209, 88};
    for (int i = 0; i < 3; i++) {
      float ph = s.t * 0.55f + i / 3.0f;
      ph -= floorf(ph);
      ring_band(46 + 70 * ph, 1, 1, green, a * (1 - ph) * (1 - ph) * 0.5f, 0);
    }
  }
  if (s_y1 < 88 || s_y0 > 212) return;
  switch (s.card) {
    case Card::Msg: {
      app_badge(C, 104 + lift, s.card_app, a);
      char up[64];
      upper(s.card_app, up, sizeof up);
      draw_text(text::kCaps, up, C, 130 + lift, 1, true, acc, a * 0.9f, 1.2f);
      fit(text::kUiBold, s.card_title, 176, line, sizeof line);
      draw_text(text::kUiBold, line, C, 152 + lift, 1, true, ink, a);
      const int n = wrap2_fit(text::kUi, s.card_text, 176, lines);
      for (int i = 0; i < n; i++) draw_text(text::kUi, lines[i], C, 172 + i * 19 + lift, 1, true, ink, a * 0.75f);
      break;
    }
    case Card::Call: {
      fit(text::kTitle, s.card_title, 180, line, sizeof line);
      draw_text(text::kTitle, line, C, 164 + lift, 1, true, ink, a);
      char up[64];
      upper(s.card_text, up, sizeof up);
      draw_text(text::kCaps, up, C, 184 + lift, 1, true, {48, 209, 88}, a * (0.6f + 0.4f * fabsf(sinf(s.t * 3))), 1.2f);
      break;
    }
    case Card::Battery: {
      const int lvl = s.card_level < 0 ? 0 : (s.card_level > 100 ? 100 : s.card_level);
      const Rgb fillc = lvl <= 10 ? Rgb{255, 69, 58} : Rgb{255, 159, 10};
      const float y = 126 + lift, frame_a = a * 0.7f;
      rrect(C, y, 23, 11, 6, ink, frame_a, 1.8f);
      rrect(C + 26.5f, y, 1.5f, 4, 1.5f, ink, frame_a, 0);
      const float wf = fmaxf(3, 40 * lvl / 100.0f);
      rrect(C - 20 + wf / 2, y, wf / 2, 8, 3, fillc, a, 0);
      fit(text::kUiBold, s.card_title, 176, line, sizeof line);
      draw_text(text::kUiBold, line, C, 166 + lift, 1, true, ink, a);
      fit(text::kUi, s.card_text, 176, line, sizeof line);
      draw_text(text::kUi, line, C, 186 + lift, 1, true, ink, a * 0.7f);
      break;
    }
    case Card::Check: {
      char up[64];
      upper(s.card_title, up, sizeof up);
      draw_text(text::kCaps, up, C, 98 + lift, 1, true, acc, a * 0.9f, 1.2f);
      // строки в тексте разделены « · »
      const char *p = s.card_text;
      for (int i = 0; i < 4 && *p; i++) {
        const char *sep = strstr(p, " \xC2\xB7 ");
        const size_t len = sep ? (size_t)(sep - p) : strlen(p);
        char row[96];
        snprintf(row, sizeof row, "%.*s", (int)(len < sizeof row ? len : sizeof row - 1), p);
        check_row(s, row, 126 + i * 27 + lift, a);
        p = sep ? sep + 4 : p + len;
      }
      break;
    }
    case Card::Remark:
    case Card::Wake:
    case Card::Info: {
      const int n = wrap2_fit(text::kUiBold, s.card_title, 176, lines);
      const float y0 = (s.card == Card::Remark ? 168 : 160) + lift;
      const float ta = s.card == Card::Remark ? 0.8f : 1, xa = s.card == Card::Remark ? 0.5f : 0.7f;
      for (int i = 0; i < n; i++) draw_text(text::kUiBold, lines[i], C, y0 + i * 20, 1, true, ink, a * ta);
      fit(text::kUi, s.card_text, 176, line, sizeof line);
      draw_text(text::kUi, line, C, y0 + n * 20 + 2, 1, true, ink, a * xa);
      break;
    }
    case Card::None:
      break;
  }
}

#include "face/games.inc"

// Таблицы дизеринга: порог Байера × значение канала → 5/6 бит, сразу с яркостью
void build_lut(int bright) {
  for (int t = 0; t < 16; t++)
    for (int v = 0; v < 256; v++) {
      const float th = (t + 0.5f) / 16, vv = v * bright / 255.0f;
      const int r5 = (int)floorf(vv * 31 / 255.0f + th), g6 = (int)floorf(vv * 63 / 255.0f + th);
      s_lut5[t][v] = (uint8_t)(r5 > 31 ? 31 : r5);
      s_lut6[t][v] = (uint8_t)(g6 > 63 ? 63 : g6);
    }
  s_lut_bright = bright;
}

// 8 бит на канал → RGB565 с упорядоченным дизерингом 4×4, старший байт первым
IRAM_ATTR void to565(uint8_t *out8) {
  uint16_t *out = reinterpret_cast<uint16_t *>(out8);
  const uint8_t *p = s_work;
  for (int y = s_y0; y < s_y1; y++) {
    const uint8_t *b = BAYER + (y & 3) * 4;
    const uint8_t *r0 = s_lut5[b[0]], *r1 = s_lut5[b[1]], *r2 = s_lut5[b[2]], *r3 = s_lut5[b[3]];
    const uint8_t *g0 = s_lut6[b[0]], *g1 = s_lut6[b[1]], *g2 = s_lut6[b[2]], *g3 = s_lut6[b[3]];
    for (int x = 0; x < W; x += 4, p += 12, out += 4) {
      out[0] = __builtin_bswap16((uint16_t)((r0[p[0]] << 11) | (g0[p[1]] << 5) | r0[p[2]]));
      out[1] = __builtin_bswap16((uint16_t)((r1[p[3]] << 11) | (g1[p[4]] << 5) | r1[p[5]]));
      out[2] = __builtin_bswap16((uint16_t)((r2[p[6]] << 11) | (g2[p[7]] << 5) | r2[p[8]]));
      out[3] = __builtin_bswap16((uint16_t)((r3[p[9]] << 11) | (g3[p[10]] << 5) | r3[p[11]]));
    }
  }
}

// Только для заставки: рисует одна задача, буферы по кругу
void send_strip(int k) {
  uint8_t *out = s_out[k % NOUT];
  lcd::wait(1);  // в очереди не больше двух передач — этот буфер уже свободен
  to565(out);
  lcd::blit_async(s_y0, SH, out);
}

// ── Два ядра. Полосы кадра берут по очереди главная задача (ядро 1) и помощник (ядро 0).
//    Номера полос сквозные через все кадры: полоса g — это строка (g % NSTRIP) кадра
//    g / NSTRIP. В экран полосы уходят строго по порядку, отправляет только главная задача.
//    Помощник работает с низшим приоритетом: Wi-Fi и звук на ядре 0 всегда важнее ──

const Scene *s_job = nullptr;
std::atomic<uint32_t> s_take{0};      // следующая свободная полоса
std::atomic<uint32_t> s_end{0};       // конец текущего кадра (не включая)
std::atomic<uint32_t> s_sent{0};      // столько полос уже отправлено в экран
std::atomic<uint32_t> s_ready[NOUT];  // какая полоса лежит готовой в буфере
std::atomic<uint32_t> s_finished{0};  // столько полос нарисовано и сведено к 16 битам
uint32_t s_next_send = 0;             // следующая к отправке (знает только задача отправки)
TaskHandle_t s_helper = nullptr, s_sender = nullptr;

// Сколько времени уходит на каждый проход — у каждого ядра свой счёт
struct Prof {
  int64_t bg = 0, ring = 0, eyes = 0, over = 0, conv = 0, wait = 0;
  int strips = 0;
};
Prof s_prof[2];
int64_t s_prof_tables = 0;
int s_prof_frames = 0;

class Tick {
 public:
  explicit Tick(int64_t &acc) : acc_(acc), t_(esp_timer_get_time()) {}
  ~Tick() { acc_ += esp_timer_get_time() - t_; }

 private:
  int64_t &acc_;
  int64_t t_;
};

void render_strip(const Scene &s, uint32_t g) {
  Prof &pf = s_prof[s_worker];
  s_y0 = (int)(g % NSTRIP) * SH;
  s_y1 = s_y0 + SH;
  {
    Tick t(pf.bg);
    fill_bg();
  }
  {
    Tick t(pf.ring);
    draw_ring();
  }
  {
    Tick t(pf.eyes);
    draw_eye(s, s.eye[0]);
    draw_eye(s, s.eye[1]);
  }
  {
    Tick t(pf.over);
    draw_weather(s);
    draw_offline(s);
    draw_arcs(s);
    draw_clock(s);
    draw_caption(s);
    draw_timer(s);
    draw_card(s);
    draw_task(s);
    draw_reminders(s);
    draw_game(s);
    draw_zs(s);
  }
  pf.strips++;
}

// Отправка в экран — своя задача: библиотека экрана перед каждой полосой ждёт, пока уйдёт
// предыдущая, и пусть ждёт она, а ядра тем временем рисуют дальше.
void sender_task(void *) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(5));
    for (;;) {
      const uint32_t j = s_next_send;
      if (j >= s_end.load() || s_ready[j % NOUT].load() != j) break;
      lcd::wait(1);
      lcd::blit_async((int)(j % NSTRIP) * SH, SH, s_out[j % NOUT]);
      s_next_send = j + 1;
      s_sent.store(j + 1);
    }
  }
}

// Взять следующую полосу кадра, нарисовать и положить готовой. false — полос не осталось.
bool work_one() {
  uint32_t g = s_take.load();
  do {
    if (g >= s_end.load()) return false;
  } while (!s_take.compare_exchange_weak(g, g + 1));
  render_strip(*s_job, g);
  Prof &pf = s_prof[s_worker];
  {
    // Буфер g % NOUT свободен, когда в экран ушла полоса g − 2: перед каждой отправкой
    // очередь сжимается до одной передачи, значит полоса g − 4 к этому времени уже ушла.
    Tick t(pf.wait);
    while (s_sent.load() + 1 < g) esp_rom_delay_us(5);
  }
  {
    Tick t(pf.conv);
    to565(s_out[g % NOUT]);
  }
  s_ready[g % NOUT].store(g);
  s_finished.fetch_add(1);
  xTaskNotifyGive(s_sender);
  return true;
}

void helper_task(void *) {
  s_worker = 1;
  s_work = s_works[1];
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    while (work_one()) {
    }
  }
}

}  // namespace

bool init() {
  for (auto &w : s_works) w = (uint8_t *)heap_caps_malloc(W * SH * 3, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  for (auto &o : s_out) o = (uint8_t *)heap_caps_malloc(W * SH * 2, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
  // Таблицы читаются только сиянием и таймером — им хватает внешней памяти через кэш
  s_qr = (uint16_t *)heap_caps_malloc(C * C * 2, MALLOC_CAP_SPIRAM);
  s_qa = (uint8_t *)heap_caps_malloc(C * C, MALLOC_CAP_SPIRAM);
  s_polar = (uint8_t(*)[RING_E + 2][3])heap_caps_malloc(RING_A * (RING_E + 2) * 3, MALLOC_CAP_SPIRAM);
  s_tile_bits = (uint8_t *)heap_caps_malloc(TILE_BYTES, MALLOC_CAP_SPIRAM);
  if (!s_qr) s_qr = (uint16_t *)malloc(C * C * 2);
  if (!s_qa) s_qa = (uint8_t *)malloc(C * C);
  if (!s_polar) s_polar = (uint8_t(*)[RING_E + 2][3])malloc(RING_A * (RING_E + 2) * 3);
  if (!s_tile_bits) s_tile_bits = (uint8_t *)malloc(TILE_BYTES);
  bool ok = s_qr && s_qa && s_polar && s_tile_bits;
  for (auto *b : s_works) ok = ok && b;
  for (auto *b : s_out) ok = ok && b;
  if (!ok) {
    ESP_LOGE(TAG, "нет памяти под буферы кадра");
    return false;
  }
  for (int qy = 0; qy < C; qy++)
    for (int qx = 0; qx < C; qx++) {
      const float dx = qx + 0.5f, dy = qy + 0.5f;
      s_qr[qy * C + qx] = (uint16_t)(sqrtf(dx * dx + dy * dy) * 64 + 0.5f);
      s_qa[qy * C + qx] = (uint8_t)(atan2f(dy, dx) / TAU * 256 + 0.5f);
    }
  for (int i = 0; i < 258; i++) s_exp[i] = expf(-i / 32.0f);
  init_grid();
  for (int i = 0; i < 257; i++) s_sin[i] = sinf(i * TAU / 256);
  for (int e = 0; e < RING_E + 2; e++) s_rim[e] = 0.6f * expf(-e / 1.6f);
  for (auto &r : s_ready) r.store(UINT32_MAX);
  build_lut(255);
  if (xTaskCreatePinnedToCore(sender_task, "lcd_send", 3072, nullptr, 6, &s_sender, 1) != pdPASS) {
    ESP_LOGE(TAG, "не запустилась отправка в экран");
    return false;
  }
  if (xTaskCreatePinnedToCore(helper_task, "face2", 6144, nullptr, 1, &s_helper, 0) != pdPASS) {
    s_helper = nullptr;  // справится и одно ядро
    ESP_LOGW(TAG, "второе ядро не подключено — рисую на одном");
  }
  ESP_LOGI(TAG, "буферы готовы, свободно внутренней памяти %u КБ",
           (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024));
  return true;
}

void splash() {
  if (!s_works[0]) return;
  s_work = s_works[0];
  for (int k = 0; k < NSTRIP; k++) {
    s_y0 = k * SH;
    s_y1 = s_y0 + SH;
    for (int y = s_y0; y < s_y1; y++) {
      const uint8_t c[3] = {(uint8_t)(y < 80 ? 255 : 0), (uint8_t)(y >= 80 && y < 160 ? 255 : 0),
                            (uint8_t)(y >= 160 ? 255 : 0)};
      uint8_t *p = px(0, y);
      for (int x = 0; x < W; x++, p += 3) memcpy(p, c, 3);
    }
    draw_text(text::kClock, "123", C, 55, 1, true, {255, 255, 255}, 1);
    send_strip(k);
  }
  lcd::wait(0);
}

void log_profile() {
  const int n = s_prof_frames ? s_prof_frames : 1;
  const float k = 1000.0f * n;
  for (int w = 0; w < 2; w++) {
    const Prof &p = s_prof[w];
    ESP_LOGI(TAG, "%s: полос %.1f, фон %.1f, край %.1f, глаза %.1f, надписи %.1f, вывод %.1f, ждёт %.1f мс",
             w ? "ядро 0" : "ядро 1", (float)p.strips / n, p.bg / k, p.ring / k, p.eyes / k, p.over / k, p.conv / k,
             p.wait / k);
  }
  ESP_LOGI(TAG, "общие таблицы кадра %.1f мс", s_prof_tables / k);
  for (auto &p : s_prof) p = Prof{};
  s_prof_tables = 0;
  s_prof_frames = 0;
}

Timing render(const Scene &s) {
  if (!s_works[0]) return {};
  s_worker = 0;
  s_work = s_works[0];
  const int64_t t0 = esp_timer_get_time();
  {
    Tick t(s_prof_tables);
    build_grid(s);
    build_ring(s);
    build_arcs(s);
    if (s.bright != s_lut_bright) build_lut(s.bright);
  }
  s_job = &s;
  const uint32_t end = s_end.load() + NSTRIP;
  s_end.store(end);  // с этого момента помощник может брать полосы нового кадра
  if (s_helper) xTaskNotifyGive(s_helper);
  int64_t waited = 0;
  while (s_finished.load() < end) {
    if (work_one()) continue;
    // свои полосы кончились — ждём, пока второе ядро дорисует последние
    const int64_t w0 = esp_timer_get_time();
    esp_rom_delay_us(5);
    waited += esp_timer_get_time() - w0;
  }
  s_prof_frames++;
  const int64_t t1 = esp_timer_get_time();
  return {(uint32_t)(t1 - t0 - waited), (uint32_t)waited};
}

}  // namespace face
