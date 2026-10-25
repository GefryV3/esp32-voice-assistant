// Отрисовка кадра полосами по 16 строк. Каждая полоса считается в 8-битный RGB: фон с
// «живым» цветом, сияние по краю, глаза, часы, надписи. Потом сводится к 16 битам с
// дизерингом (без ступенек на тёмных переходах) и уходит в экран по DMA. Полосы рисуют
// оба ядра сразу. Внешний вид — по макету design/screen-mockup.html.
#include <atomic>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <initializer_list>
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
// Проверка по битам: сборка с -ffast-math может выбросить обычные isfinite/isnan
inline bool finite_bits(float f) {
  uint32_t u;
  memcpy(&u, &f, sizeof u);
  return ((u >> 23) & 0xFF) != 0xFF;
}

bool clip(float fx0, float fy0, float fx1, float fy1, int &x0, int &y0, int &x1, int &y1) {
  // NaN/inf (например, из реакций глаз на звук) раньше давали отрицательную строку и запись мимо буфера (паника)
  if (!(finite_bits(fx0) && finite_bits(fy0) && finite_bits(fx1) && finite_bits(fy1))) {
    static bool warned = false;
    if (!warned) {
      warned = true;
      ESP_LOGW("face", "clip: нечисло в геометрии (%g %g %g %g), кадр пропущен", (double)fx0, (double)fy0, (double)fx1, (double)fy1);
    }
    return false;
  }
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

[[maybe_unused]] void build_grid(const Scene &s) {
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

[[maybe_unused]] IRAM_ATTR void fill_bg() {
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

[[maybe_unused]] void build_ring(const Scene &s) {
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

[[maybe_unused]] IRAM_ATTR void draw_ring() {
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

[[maybe_unused]] IRAM_ATTR void draw_eye(const Scene &s, const Eye &e) {
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
float draw_text_raw(const text::Font &f, const char *s, float x, float base, float sc, bool center, const Rgb &c,
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

// Text with a thin dark outline: it stays readable straight on top of the LED grid (no backing plate).
// The outline is drawn first, in black, at eight small offsets around the glyphs.
bool s_halo = true;
float draw_text(const text::Font &f, const char *s, float x, float base, float sc, bool center, const Rgb &c, float alpha,
                float tracking = 0) {
  if (s_halo && alpha > 0.02f && (c.r + c.g + c.b) > 30) {
    static const float OFF[8][2] = {{-1.6f, 0}, {1.6f, 0}, {0, -1.6f}, {0, 1.6f}, {-1.2f, -1.2f}, {1.2f, -1.2f}, {-1.2f, 1.2f}, {1.2f, 1.2f}};
    for (const auto &o : OFF) draw_text_raw(f, s, x + o[0], base + o[1], sc, center, {0, 0, 0}, alpha * 0.9f, tracking);
  }
  return draw_text_raw(f, s, x, base, sc, center, c, alpha, tracking);
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

// A title that fits stays on one line; otherwise it is split at the space that makes the wider of the two lines
// as narrow as possible ("Будильник выключен," / "а ты спишь", as in the mockup, not a greedy "… выключен, а" / "ты спишь").
// Falls back to wrap2_fit when no split fits.
int wrap2_balanced(const text::Font &f, const char *s, float maxw, char out[2][128]) {
  out[0][0] = out[1][0] = 0;
  if (strlen(s) < 128 && text::width(f, s) <= maxw) {
    snprintf(out[0], 128, "%s", s);
    return 1;
  }
  const size_t len = strlen(s);
  float best = 1e9f;
  size_t cut = 0;
  if (len < 128)
    for (size_t i = 1; i + 1 < len; i++) {
      if (s[i] != ' ') continue;
      char left[128];
      memcpy(left, s, i);
      left[i] = 0;
      const float w1 = text::width(f, left), w2 = text::width(f, s + i + 1), w = fmaxf(w1, w2);
      if (w1 <= maxw && w2 <= maxw && w < best) {
        best = w;
        cut = i;
      }
    }
  if (!cut) return wrap2_fit(f, s, maxw, out);
  memcpy(out[0], s, cut);
  out[0][cut] = 0;
  snprintf(out[1], 128, "%s", s + cut + 1);
  return 2;
}

// Word wrap into at most n (<= 4) lines; widths[i] is the width limit of line i (lines near the bottom of the
// circle are narrower). When text is left over, the last line gets an ellipsis and *cut is set.
int wrap_n(const text::Font &f, const char *s, const float *widths, int n, char out[4][128], bool *cut = nullptr) {
  for (int i = 0; i < n; i++) out[i][0] = 0;
  int line = 0;
  size_t len = 0;
  while (*s && line < n) {
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
    if (len && text::width(f, cand) > widths[line]) {
      line++;
      len = 0;
      continue;
    }
    memcpy(out[line], cand, cl + 1);
    len = cl;
    s = e;
  }
  while (*s == ' ') s++;
  int used = 0;
  for (int i = 0; i < n; i++)
    if (out[i][0]) used = i + 1;
  if (cut) *cut = *s != 0;
  if (*s && used > 0) {
    char tmp[136];
    snprintf(tmp, sizeof tmp, "%sâ¦", out[used - 1]);
    fit(f, tmp, widths[used - 1], out[used - 1], 128);
  }
  return used;
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
[[maybe_unused]] void draw_weather(const Scene &s) {
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

[[maybe_unused]] void draw_clock(const Scene &s) {
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
[[maybe_unused]] void build_arcs(const Scene &s) {
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

[[maybe_unused]] void draw_arcs(const Scene &s) {
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
[[maybe_unused]] void draw_caption(const Scene &s) {
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

[[maybe_unused]] void draw_timer(const Scene &s) {
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

[[maybe_unused]] void draw_zs(const Scene &s) {
  const text::Glyph *g = text::find(text::kUi, 'z');
  for (int i = 0; i < s.nz; i++) {
    const Scene::Z &z = s.z[i];
    if (!g) break;
    const float sc = z.size / text::kUi.size, gw = g->adv / 16.0f * sc;
    glyph(text::kUi, g, z.x - gw / 2, z.y, sc, 1, 0, rgb(s.ink), z.a);
  }
}

// ── Карточки с телефона и мини-плеер (без подложек — как короткий взгляд на Apple Watch) ──

// Скруглённый прямоугольник: stroke > 0 — контур этой толщины, иначе заливка
[[maybe_unused]] void rrect(float cx, float cy, float hw, float hh, float r, const Rgb &c, float alpha, float stroke) {
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

[[maybe_unused]] void bell_icon(float x, float y, const Rgb &c, float a) {
  capsule(x - 6, y + 4, x - 5, y - 1, 1.6f, c, a);
  arc(x, y - 1, 5, 1.6f, PI, TAU, c, a);
  capsule(x + 5, y - 1, x + 6, y + 4, 1.6f, c, a);
  capsule(x - 6, y + 4, x + 6, y + 4, 1.6f, c, a);
  capsule(x - 2, y + 7, x + 2, y + 7, 1.6f, c, a);
}

// First letter of the app name in upper case ("telegram" -> "Telegram")
void cap_first(const char *s, char *out, size_t n) {
  const char *p = s;
  text::next(p);
  const size_t fl = (size_t)(p - s) < 4 ? (size_t)(p - s) : 4;
  char tmp[5] = {};
  memcpy(tmp, s, fl);
  char first[8];
  upper(tmp, first, sizeof first);
  snprintf(out, n, "%s%s", first, s + fl);
}

// Colour of the little app dot by name
Rgb app_color(const char *app) {
  struct Known {
    const char *name;
    Rgb c;
  };
  static const Known KNOWN[] = {{"telegram", {42, 171, 238}}, {"whatsapp", {37, 211, 102}},
                                {"instagram", {225, 48, 108}}, {"gmail", {234, 67, 53}},
                                {"discord", {88, 101, 242}},    {"youtube", {255, 0, 51}}};
  for (const Known &k : KNOWN)
    if (strcasecmp(app, k.name) == 0) return k.c;
  return {142, 142, 147};
}

// "Before sleep" row: the icon itself is stamped into the dot table (see build_led); here is only the text
int check_kind(const char *row) {
  if (strncmp(row, "будильник", 18) == 0) return 0;
  if (strncmp(row, "телефон", 14) == 0) return 1;
  if (strncmp(row, "ноутбук", 14) == 0) return 2;
  return 3;
}

void check_row(const Scene &s, const char *row, float y, float a, bool compact) {
  char cap[96];
  cap_first(row, cap, sizeof cap);
  char line[96];
  // four rows: the text starts closer to the icons and is set a bit smaller (13 px); the room to the circle's edge shrinks toward the bottom
  // (three rows use the same layout: the icons stand at -14 in both cases, see the sprite stamping)
  const float x0 = 72, sc = 13.0f / 15, dy = y + 5 - 120;
  (void)compact;
  const float room = 120 + sqrtf(fmaxf(0.0f, 112 * 112 - dy * dy)) - x0;
  fit(text::kUi, cap, room / sc, line, sizeof line);
  draw_text(text::kUi, line, x0, y + 5, sc, false, rgb(s.ink), a * 0.92f);
}

// Rows of the "before sleep" list; the text separator is " · "
int check_split(const char *text_in, char rows[4][96]) {
  int n = 0;
  const char *p = text_in;
  while (n < 4 && *p) {
    const char *sep = strstr(p, " \xC2\xB7 ");
    const size_t len = sep ? (size_t)(sep - p) : strlen(p);
    snprintf(rows[n++], 96, "%.*s", (int)(len < 95 ? len : 95), p);
    p = sep ? sep + 4 : p + len;
  }
  return n;
}

void draw_player(const Scene &s, float a) {
  if (s_y1 < 150 || s_y0 > 205) return;  // the dot bar and the beat are in the dot table; this is only text
  const float lift = (1 - a) * 14;
  char line[96];
  fit(text::kUiBold, s.player_title, 142, line, sizeof line);
  draw_text(text::kUiBold, line, C, 168 + lift, 18.0f / 15, true, rgb(s.ink), a);  // mockup: 18 px
  fit(text::kUi, s.player_artist, 170, line, sizeof line);
  draw_text(text::kUi, line, C, 187 + lift, 1, true, rgb(s.ink), a * 0.55f);
}

void draw_card(const Scene &s) {
  const float a = s.card_a;
  if (a < 0.01f) return;
  if (s.player) {
    draw_player(s, a);
    return;
  }
  const Rgb ink = rgb(s.ink);
  const float lift = (1 - a) * 8;
  char lines[2][128];
  char line[128];
  // the "before sleep" title sits at y=78 with four rows: its top part lies in the strips above 88
  if (s_y1 < (s.card == Card::Check ? 64 : 88) || s_y0 > 212) return;
  switch (s.card) {
    case Card::Msg: {
      char nm[64];
      cap_first(s.card_app, nm, sizeof nm);
      disc(84, 106 + lift, 3.5f, app_color(s.card_app), a);
      draw_text(text::kUi, nm, 126, 110 + lift, 1, true, ink, a * 0.55f);
      fit(text::kTitle, s.card_title, 176 * 24.0f / 21, line, sizeof line);
      draw_text(text::kTitle, line, C, 142 + lift, 21.0f / 24, true, ink, a);  // mockup: 21 px
      const int n = wrap2_fit(text::kUi, s.card_text, 176, lines);
      for (int i = 0; i < n; i++) draw_text(text::kUi, lines[i], C, 166 + i * 19 + lift, 1, true, ink, a * 0.75f);
      break;
    }
    case Card::Call: {
      fit(text::kTitle, s.card_title, 180 * 24.0f / 26, line, sizeof line);
      draw_text(text::kTitle, line, C, 168, 26.0f / 24, true, ink, a);  // mockup: 26 px
      draw_text(text::kUi, s.card_text, C, 190, 1, true, {48, 209, 88}, a * (0.6f + 0.4f * fabsf(sinf(s.t * 3))));
      break;
    }
    case Card::Battery: {
      const int lvl = s.card_level < 0 ? 0 : (s.card_level > 100 ? 100 : s.card_level);
      const Rgb fillc = lvl <= 10 ? Rgb{255, 69, 58} : (lvl <= 30 ? Rgb{255, 159, 10} : Rgb{48, 209, 88});
      fit(text::kUiBold, s.card_title, 176, line, sizeof line);
      draw_text(text::kUiBold, line, C, 174, 1, true, ink, a);
      fit(text::kUi, s.card_text, 176, line, sizeof line);
      draw_text(text::kUi, line, C, 195, 1, true, fillc, a * 0.9f);
      break;
    }
    case Card::Check: {
      char rows[4][96];
      const int n = check_split(s.card_text, rows);
      draw_text(text::kUiBold, s.card_title, C, n > 3 ? 78 : 88, 1, true, ink, a * 0.6f);
      const float step = 36, y0 = n > 3 ? 92 : 114;
      for (int i = 0; i < n; i++) check_row(s, rows[i], y0 + i * step, a * clamp01((s.card_t - 0.2f - i * 0.18f) / 0.4f), n > 3);
      break;
    }
    case Card::Remark:
    case Card::Wake:
    case Card::Info: {
      // mockup rows: info 140 / 166 (amber), remark 172 / 192, wake 160, 179 / 198 (amber); title sizes 17 / 15 / 14 px
      const bool rem = s.card == Card::Remark, wk = s.card == Card::Wake;
      const float tsc = rem ? 1.0f : (wk ? 14.0f / 15 : 17.0f / 15);
      // mockup line breaks: the night remark is one line, "get up" breaks after "Будильник выключен,"
      const float wmax = rem ? 200 : 176;
      const int n = wrap2_balanced(text::kUiBold, s.card_title, wmax / tsc, lines);
      const float y0 = (rem ? 172 : (wk ? 160 : 140)) + lift, step = wk ? 19 : 20, gap = s.card == Card::Info ? 6 : 0;
      const float ta = rem ? 0.8f : 1, xa = rem ? 0.5f : (wk ? 0.85f : 0.95f);
      for (int i = 0; i < n; i++) draw_text(text::kUiBold, lines[i], C, y0 + i * step, tsc, true, ink, a * ta);
      const float xsc = rem ? 13.0f / 15 : (wk ? 12.0f / 15 : 14.0f / 15);  // mockup sizes of the last line: 13 / 12 / 14 px
      fit(text::kUi, s.card_text, 176 / xsc, line, sizeof line);
      draw_text(text::kUi, line, C, y0 + n * step + gap, xsc, true, wk || s.card == Card::Info ? Rgb{255, 179, 64} : ink, a * xa);
      break;
    }
    case Card::None:
      break;
  }
}

#include "face/games.inc"

// ── LED panel. The face is a 41x41 grid of LEDs with a 6 px pitch (mockup: design/screen-variants-9.html).
//    Eyes, time, the edge wave, the comet, rain, "Z" and the power-on sweep are computed once per frame
//    into a dot table; the screen strips only lay out glowing squares from it ──

constexpr int LK = 20, LN = 2 * LK + 1, NB = 72;
constexpr float LP = 6;
struct Dot {
  uint8_t r, g, b, a, v;  // colour; a - core opacity; v - brightness (size and glow); a == 0 - no dot
};
Dot s_dots[LN][LN];
uint8_t s_txt[LN][LN], s_txt_c[LN][LN];  // stencil of digits and icons: opacity and colour index
float s_spec[NB];                        // heights of the edge wave bars
float s_led_t = -1, s_boot_t0 = -1, s_boot = 0;
float s_rain_on[LN], s_rain_ph[LN];

// ── Shape layer. The same signed-distance shapes as in the web mockup (design/screen-rest.html): each shape lights the
//    dots it covers (a soft edge one pitch wide) and a dot keeps the brightest layer, exactly like the mockup's put() ──
struct Shp {
  uint8_t v, r, g, b;  // brightness 0..255 and colour; v == 0 - nothing here
};
Shp s_sh[LN][LN];

inline void sh_put(int k, int m, float v, const Rgb &c) {
  if (k < -LK || k > LK || m < -LK || m > LK || v <= 0.001f) return;
  Shp &d = s_sh[m + LK][k + LK];
  const uint8_t u = u8f(clamp01(v) * 255);
  if (u > d.v) {
    d.v = u;
    d.r = u8f(c.r);
    d.g = u8f(c.g);
    d.b = u8f(c.b);
  }
}

// sd(x, y) - distance to the shape edge in px (negative inside); only dots inside the box are evaluated
template <typename F>
void sh_shape(F sd, const Rgb &c, float v, float bx0, float by0, float bx1, float by1) {
  if (v <= 0.002f) return;
  int k0 = (int)ceilf((bx0 - C) / LP), k1 = (int)floorf((bx1 - C) / LP);
  int m0 = (int)ceilf((by0 - C) / LP), m1 = (int)floorf((by1 - C) / LP);
  if (k0 < -LK) k0 = -LK;
  if (k1 > LK) k1 = LK;
  if (m0 < -LK) m0 = -LK;
  if (m1 > LK) m1 = LK;
  for (int m = m0; m <= m1; m++)
    for (int k = k0; k <= k1; k++) {
      const float q = clamp01(0.5f - sd(C + k * LP, C + m * LP) / (LP * 1.05f)) * v;
      if (q > 0.01f) sh_put(k, m, q, c);
    }
}

// Pixel sprite into the shape layer ('#' - lit dot)
void sh_sprite(const char *const *rows, int n, int k0, int m0, const Rgb &c, float v) {
  for (int ry = 0; ry < n; ry++)
    for (int rx = 0; rows[ry][rx]; rx++)
      if (rows[ry][rx] == '#') sh_put(k0 + rx, m0 + ry, v, c);
}

inline auto sd_disc(float cx, float cy, float r) {
  return [=](float x, float y) { return sqrtf((x - cx) * (x - cx) + (y - cy) * (y - cy)) - r; };
}
inline auto sd_line(float ax, float ay, float bx, float by, float w) {
  return [=](float x, float y) {
    const float vx = bx - ax, vy = by - ay, l2 = vx * vx + vy * vy;
    const float t = clamp01(l2 > 0 ? ((x - ax) * vx + (y - ay) * vy) / l2 : 0);
    const float dx = x - ax - vx * t, dy = y - ay - vy * t;
    return sqrtf(dx * dx + dy * dy) - w * 0.5f;
  };
}
inline auto sd_box(float cx, float cy, float hw, float hh, float r) {
  return [=](float x, float y) {
    const float qx = fabsf(x - cx) - hw + r, qy = fabsf(y - cy) - hh + r;
    const float ox = qx > 0 ? qx : 0, oy = qy > 0 ? qy : 0;
    return sqrtf(ox * ox + oy * oy) + fminf(fmaxf(qx, qy), 0.0f) - r;
  };
}
template <typename F>
inline auto sd_outline(F sd, float w) {
  return [=](float x, float y) { return fabsf(sd(x, y)) - w * 0.5f; };
}
// Arc of a circle: centre, radius, middle direction (canvas angle, y down), half opening, stroke width
inline auto sd_arc(float cx, float cy, float R, float mid, float half, float w) {
  return [=](float x, float y) {
    const float dx = x - cx, dy = y - cy;
    float a = fmodf(atan2f(dy, dx) - mid + PI, TAU);
    if (a < 0) a += TAU;
    a -= PI;
    if (fabsf(a) <= half) return fabsf(sqrtf(dx * dx + dy * dy) - R) - w * 0.5f;
    const float e = mid + (a < 0 ? -half : half), ex = cx + R * cosf(e), ey = cy + R * sinf(e);
    return sqrtf((x - ex) * (x - ex) + (y - ey) * (y - ey)) - w * 0.5f;
  };
}

const uint8_t G3[11][5] = {{7, 5, 5, 5, 7}, {2, 6, 2, 2, 7}, {7, 1, 7, 4, 7}, {7, 1, 7, 1, 7}, {5, 5, 7, 1, 1}, {7, 4, 7, 1, 7},
                           {7, 4, 7, 5, 7}, {7, 1, 1, 1, 1}, {7, 5, 7, 5, 7}, {7, 5, 7, 1, 7}, {7, 1, 2, 4, 7}};  // 0..9 and 'z', 3 bits per row
const Rgb L_THINK = {191, 90, 242}, L_SPEAK = {100, 210, 255}, L_TIMER = {255, 179, 64};
const Rgb L_MUS_A = {48, 209, 88}, L_MUS_B = {100, 225, 255}, L_WHITE = {255, 255, 255}, L_Z = {150, 175, 255};

// Colours of the stencil by index: 0 - ink (set per frame), the rest are fixed
const Rgb L_ICON = {140, 205, 255}, L_GREEN = {48, 209, 88}, L_RED = {255, 69, 58}, L_AMBER2 = {255, 159, 10}, L_ZR = {255, 130, 110};
const Rgb *const TXT_COL[] = {nullptr, &L_TIMER, &L_Z, &L_ICON, &L_GREEN, &L_RED, &L_AMBER2, &L_ZR};

void stamp_sprite(const char *const *rows, int n, int k0, int m0, uint8_t a, uint8_t ci) {
  for (int ry = 0; ry < n; ry++)
    for (int rx = 0; rows[ry][rx]; rx++) {
      const int k = k0 + rx, m = m0 + ry;
      if (rows[ry][rx] != '#' || k < -LK || k > LK || m < -LK || m > LK) continue;
      s_txt[m + LK][k + LK] = a;
      s_txt_c[m + LK][k + LK] = ci;
    }
}

inline float hash3(float a, float b, float c) {
  const float x = sinf(a * 12.9898f + b * 78.233f + c * 37.719f) * 43758.5453f;
  return x - floorf(x);
}
inline Rgb lerp_rgb(const Rgb &a, const Rgb &b, float t) { return {a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t, a.b + (b.b - a.b) * t}; }

Rgb hsl(float h, float s, float l) {
  h -= 360 * floorf(h / 360);
  const float c = (1 - fabsf(2 * l - 1)) * s, x = c * (1 - fabsf(fmodf(h / 60, 2) - 1)), m = l - c / 2;
  float r = 0, g = 0, b = 0;
  if (h < 60) r = c, g = x;
  else if (h < 120) r = x, g = c;
  else if (h < 180) g = c, b = x;
  else if (h < 240) g = x, b = c;
  else if (h < 300) r = x, b = c;
  else r = c, b = x;
  return {(r + m) * 255, (g + m) * 255, (b + m) * 255};
}

void stamp_glyph(const uint8_t *g, int k0, int m0, int sc, uint8_t a, uint8_t ci) {
  for (int ry = 0; ry < 5; ry++)
    for (int rx = 0; rx < 3; rx++) {
      if (!((g[ry] >> (2 - rx)) & 1)) continue;
      for (int dy = 0; dy < sc; dy++)
        for (int dx = 0; dx < sc; dx++) {
          const int k = k0 + rx * sc + dx, m = m0 + ry * sc + dy;
          if (k < -LK || k > LK || m < -LK || m > LK) continue;
          s_txt[m + LK][k + LK] = a;
          s_txt_c[m + LK][k + LK] = ci;
        }
    }
}

// "12:34" in 3x5 dot digits (sc - how many dots one cell is stretched to); column 0 is the screen centre
void stamp_time(const char *s, int sc, int m0, uint8_t a, uint8_t ci, bool colon_on) {
  int total = 0, n = 0;
  for (const char *p = s; *p; p++, n++) total += (*p == ':' ? sc : 3 * sc) + (n ? 1 : 0);
  int k = -(total / 2);
  for (const char *p = s; *p; p++) {
    if (*p == ':') {
      if (colon_on)
        for (int r = 1; r <= 3; r += 2)
          for (int dy = 0; dy < sc; dy++)
            for (int dx = 0; dx < sc; dx++) {
              const int kk = k + dx, mm = m0 + r * sc + dy;
              if (kk < -LK || kk > LK || mm < -LK || mm > LK) continue;
              s_txt[mm + LK][kk + LK] = a;
              s_txt_c[mm + LK][kk + LK] = ci;
            }
      k += sc + 1;
    } else {
      if (*p >= '0' && *p <= '9') stamp_glyph(G3[*p - '0'], k, m0, sc, a, ci);
      k += 3 * sc + 1;
    }
  }
}

// ── Scenes made of shapes (mockup: design/screen-rest.html). Everything below is drawn into the shape layer once
//    per frame; text stays in the overlay passes ──

const Rgb L_WARM = {255, 214, 120}, L_BLUE = {10, 132, 255}, L_HOT = {255, 90, 78}, L_GREY = {200, 205, 235};
const Rgb L_GOLD = {255, 205, 90}, L_VIOLET = {191, 90, 242}, L_CYAN = {100, 210, 255}, L_YELLOW = {255, 214, 10}, L_PINK = {255, 55, 95};
const Rgb L_GRID_RED = {255, 90, 70};

inline float ease_out3(float p) {
  p = clamp01(p);
  return 1 - (1 - p) * (1 - p) * (1 - p);
}

// Bell: a dome, a waist and a flared lip swinging about its hook; the clapper swings the other way
void led_bell(float cx, float cy, float swing, float v) {
  const float hx = cx, hy = cy - 20, cs = cosf(swing), sn = sinf(swing);
  const auto dome_sd = sd_disc(cx, cy - 6, 12), knob_sd = sd_disc(cx, cy - 19, 3);
  const auto waist_sd = sd_box(cx, cy + 1, 12, 6, 1), flare_sd = sd_box(cx, cy + 8, 18, 3.5f, 2);
  auto body = [=](float x, float y) {
    const float dx = x - hx, dy = y - hy;
    const float u = hx + dx * cs + dy * sn, w = hy - dx * sn + dy * cs;
    return fminf(fminf(dome_sd(u, w), waist_sd(u, w)), fminf(flare_sd(u, w), knob_sd(u, w)));
  };
  sh_shape(body, L_TIMER, 0.95f * v, cx - 44, cy - 40, cx + 44, cy + 40);
  sh_shape(sd_disc(cx - swing * 18, cy + 17, 4), L_TIMER, 0.95f * v, cx - 30, cy + 4, cx + 30, cy + 30);
}

// ── Games ──

const Rgb *const WHEEL_COL[8] = {&L_BLUE, &L_GREEN, &L_TIMER, &L_VIOLET, &L_RED, &L_CYAN, &L_YELLOW, &L_PINK};
const char *const GL_HEADS[8] = {".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###.", ""};  // "О"
const char *const GL_TAILS[8] = {"####.", "#...#", "#...#", "####.", "#....", "#....", "#....", ""};  // "Р"

void led_coin(const Scene &s, float a) {
  const float lt = s.game_t, T = 2.4f, p = clamp01(lt / T);
  const int result = s.game_p0 ? 1 : 0;  // 0 - heads ("Орёл"), 1 - tails ("Решка"); an even number of half-turns lands on heads
  const float spin = ease_out3(p) * (8 + result) * PI, cs = cosf(spin);
  const float rx = fmaxf(fabsf(cs) * 44, 2.5f), ry = 44;
  const float air = p < 1 ? sinf(fminf(p / 0.85f, 1.0f) * PI) : 0, cx = 120, cy = 128 - 30 * air;
  // the shadow on the floor shrinks while the coin is high
  const float sw = 30 - 10 * air;
  sh_shape([=](float x, float y) { return (sqrtf((x - cx) * (x - cx) / (sw * sw) + (y - 190) * (y - 190) / 16) - 1) * 4; }, {255, 255, 255},
           0.28f * (1 - 0.6f * air) * a, cx - 44, 176, cx + 44, 204);
  auto ell = [=](float k) {
    return [=](float x, float y) {
      return (sqrtf((x - cx) * (x - cx) / (rx * k * rx * k) + (y - cy) * (y - cy) / (ry * k * ry * k)) - 1) * fminf(rx * k, ry * k);
    };
  };
  const float bx0 = cx - rx - 12, bx1 = cx + rx + 12, by0 = cy - ry - 12, by1 = cy + ry + 12;
  sh_shape(ell(1), L_GOLD, 0.42f * a, bx0, by0, bx1, by1);
  sh_shape(sd_outline(ell(1), 4.4f), L_GOLD, a, bx0, by0, bx1, by1);
  if (rx > 14) {
    sh_shape(sd_outline(ell(0.78f), 2.4f), L_GOLD, 0.55f * a, bx0, by0, bx1, by1);
    sh_sprite(cs >= 0 ? GL_HEADS : GL_TAILS, 7, -2, (int)lroundf((cy - 120) / LP) - 3, {255, 240, 200}, 0.95f * clamp01((rx - 14) / 14) * a);
  }
}

void led_dice(const Scene &s, float a) {
  static const int8_t PIPS[7][6][2] = {{}, {{0, 0}}, {{-1, -1}, {1, 1}}, {{-1, -1}, {0, 0}, {1, 1}}, {{-1, -1}, {1, -1}, {-1, 1}, {1, 1}},
                                       {{-1, -1}, {1, -1}, {0, 0}, {-1, 1}, {1, 1}}, {{-1, -1}, {1, -1}, {-1, 0}, {1, 0}, {-1, 1}, {1, 1}}};
  const float lt = s.game_t, T = 1.5f, p = clamp01(lt / T);
  const int result = s.game_p0 < 1 ? 1 : (s.game_p0 > 6 ? 6 : s.game_p0);
  const int face = p < 1 ? (int)(lt * 14) % 6 + 1 : result;  // faces flicker while it tumbles, then the result sits
  const float shake = p < 1 ? sinf(lt * 40) * 3 * (1 - p) : 0, cx = 120 + shake, cy = 124;
  const auto box = sd_box(cx, cy, 40, 40, 12);
  sh_shape(box, {255, 255, 255}, 0.2f * a, cx - 52, cy - 52, cx + 52, cy + 52);
  sh_shape(sd_outline(box, 2.4f), {255, 255, 255}, 0.95f * a, cx - 52, cy - 52, cx + 52, cy + 52);
  for (int i = 0; i < face; i++) {  // the number of pips equals the face
    const float px_ = cx + PIPS[face][i][0] * 22, py_ = cy + PIPS[face][i][1] * 22;
    sh_shape(sd_disc(px_, py_, 5.5f), {255, 255, 255}, a, px_ - 16, py_ - 16, px_ + 16, py_ + 16);
  }
}

void led_wheel(const Scene &s, float a) {
  const int count = s.game_p0 < 2 ? 2 : (s.game_p0 > 8 ? 8 : s.game_p0);
  const int result = s.game_p1 < 0 ? 0 : (s.game_p1 >= count ? count - 1 : s.game_p1);
  const float lt = s.game_t, T = 2.7f, p = clamp01(lt / T), S = TAU / count, cx = 120, cy = 138, R = 50;
  const float rot = ease_out3(p) * (TAU * 3 + (TAU - (result + 0.5f) * S));
  for (int m = -LK; m <= LK; m++)
    for (int k = -LK; k <= LK; k++) {
      const float x = C + k * LP, y = C + m * LP, r = sqrtf((x - cx) * (x - cx) + (y - cy) * (y - cy));
      if (fabsf(r - R) > 4) continue;
      float ang = fmodf(atan2f(x - cx, cy - y) - rot, TAU);
      if (ang < 0) ang += TAU;
      const int seg = (int)(ang / S) % count;
      const float on = p < 1 ? (fabsf(fmodf(ang, S) - S * 0.5f) < S * 0.42f ? 1.0f : 0.3f) : (seg == result ? 1.0f : 0.18f);
      sh_put(k, m, 0.85f * on * a, *WHEEL_COL[seg]);
    }
  sh_shape(sd_line(cx, cy - R - 9, cx, cy - R - 3, 4), {255, 255, 255}, a, cx - 14, cy - R - 20, cx + 14, cy - R + 4);
}

void led_rps(const Scene &s, float a) {
  const float lt = s.game_t, T = 1.5f, cx = 120, cy = 126;
  const int mine = s.game_p0 < 0 ? 0 : (s.game_p0 > 2 ? 2 : s.game_p0);
  const Rgb W = {255, 255, 255};
  if (lt < T) {  // one, two, three: the fist bounces
    const float y = cy - fabsf(sinf(lt * PI / 0.5f)) * 14;
    sh_shape(sd_disc(cx, y, 22), W, 0.95f * a, cx - 34, y - 34, cx + 34, y + 34);
    return;
  }
  const float aa = smooth((lt - T) / 0.25f) * a;
  if (mine == 0) {
    sh_shape(sd_disc(cx, cy, 24), W, aa, cx - 36, cy - 36, cx + 36, cy + 36);
  } else if (mine == 1) {
    const auto box = sd_box(cx, cy, 24, 28, 8);
    sh_shape(box, W, 0.25f * aa, cx - 36, cy - 40, cx + 36, cy + 40);
    sh_shape(sd_outline(box, 3), W, aa, cx - 36, cy - 40, cx + 36, cy + 40);
    for (int i = -1; i <= 1; i++) sh_shape(sd_line(cx + i * 11, cy - 12, cx + i * 11, cy + 12, 3), W, 0.7f * aa, cx + i * 11 - 10, cy - 24, cx + i * 11 + 10, cy + 24);
  } else {  // scissors: two blades crossed and two ring handles
    sh_shape(sd_line(cx - 20, cy - 22, cx + 12, cy + 16, 5), W, aa, cx - 34, cy - 36, cx + 26, cy + 30);
    sh_shape(sd_line(cx + 20, cy - 22, cx - 12, cy + 16, 5), W, aa, cx - 26, cy - 36, cx + 34, cy + 30);
    sh_shape(sd_outline(sd_disc(cx - 16, cy + 24, 8), 3.5f), W, aa, cx - 30, cy + 10, cx - 2, cy + 38);
    sh_shape(sd_outline(sd_disc(cx + 16, cy + 24, 8), 3.5f), W, aa, cx + 2, cy + 10, cx + 30, cy + 38);
  }
}

void led_magic8(const Scene &s, float a) {
  const float cx = 120, cy = 124, R = 52;
  sh_shape(sd_outline(sd_disc(cx, cy, R), 3), {255, 255, 255}, 0.85f * a, cx - 66, cy - 66, cx + 66, cy + 66);
  sh_shape(sd_disc(cx, cy, R - 3), L_VIOLET, 0.16f * a, cx - 66, cy - 66, cx + 66, cy + 66);
  (void)s;
}

void led_ttt(const Scene &s, float a) {
  static const int8_t LINES[8][3] = {{0, 1, 2}, {3, 4, 5}, {6, 7, 8}, {0, 3, 6}, {1, 4, 7}, {2, 5, 8}, {0, 4, 8}, {2, 4, 6}};
  const float cx = 120, cy = 126, c = 36, h = c * 1.5f, g = smooth(s.game_t / 0.5f);
  const Rgb W = {255, 255, 255};
  for (int i = 0; i < 2; i++) {
    const float d = i ? c / 2 : -c / 2;
    sh_shape(sd_line(cx + d, cy - h * g, cx + d, cy + h * g, 5), W, 0.8f * a, cx + d - 12, cy - h - 12, cx + d + 12, cy + h + 12);
    sh_shape(sd_line(cx - h * g, cy + d, cx + h * g, cy + d, 5), W, 0.8f * a, cx - h - 12, cy + d - 12, cx + h + 12, cy + d + 12);
  }
  for (int i = 0; i < 9; i++) {
    const char m = s.board[i];
    if (m != 'X' && m != 'O') continue;
    const float x = cx + (i % 3 - 1) * c, y = cy + (i / 3 - 1) * c, ca = s.cell_a[i], r = 11 * (0.4f + 0.6f * ca);
    if (m == 'X') {
      sh_shape(sd_line(x - r, y - r, x + r, y + r, 6), L_CYAN, ca * a, x - 24, y - 24, x + 24, y + 24);
      sh_shape(sd_line(x - r, y + r, x + r, y - r, 6), L_CYAN, ca * a, x - 24, y - 24, x + 24, y + 24);
    } else {
      sh_shape(sd_outline(sd_disc(x, y, r + 1.5f), 6), L_TIMER, ca * a, x - 24, y - 24, x + 24, y + 24);
    }
  }
  for (const auto &l : LINES) {  // the winning line is struck through, a bit beyond the end cells
    const char m = s.board[l[0]];
    if ((m != 'X' && m != 'O') || s.board[l[1]] != m || s.board[l[2]] != m) continue;
    float ax = cx + (l[0] % 3 - 1) * c, ay = cy + (l[0] / 3 - 1) * c, bx = cx + (l[2] % 3 - 1) * c, by = cy + (l[2] / 3 - 1) * c;
    const float dx = bx - ax, dy = by - ay, len = sqrtf(dx * dx + dy * dy), ux = dx / len, uy = dy / len;
    ax -= ux * 10;
    ay -= uy * 10;
    bx += ux * 10;
    by += uy * 10;
    float born = 1;
    for (int j = 0; j < 3; j++) born = fminf(born, s.cell_a[l[j]]);
    born = smooth(born);
    sh_shape(sd_line(ax, ay, ax + (bx - ax) * born, ay + (by - ay) * born, 6), L_GREEN, a, cx - 66, cy - 66, cx + 66, cy + 66);
    break;
  }
}

void led_game(const Scene &s) {
  const float a = s.game_a;
  switch (s.game) {
    case Game::Coin: led_coin(s, a); break;
    case Game::Dice: led_dice(s, a); break;
    case Game::Wheel: led_wheel(s, a); break;
    case Game::Rps: led_rps(s, a); break;
    case Game::Magic8: led_magic8(s, a); break;
    case Game::Ttt: led_ttt(s, a); break;
    case Game::None: break;
  }
}

// Everything the shape layer holds for this frame
void build_shapes(const Scene &s) {
  memset(s_sh, 0, sizeof s_sh);
  const float t = s.t;
  // joy: thick arches instead of the eyes; they grow, wiggle and squash
  if (s.happy > 0.01f) {
    const float a = clamp01(s.happy), sq = 1 + 0.12f * sinf(t * 4.8f), j = sinf(t * 9) * 1.6f;
    const float cy = s.face_y - 3 * fabsf(sinf(t * 4.8f)), R = 20 * sq;
    for (int side = -1; side <= 1; side += 2) {
      const float cx = C + side * 31 + j;
      sh_shape(sd_arc(cx, cy, R, -PI / 2, 1.3f * a, 10), L_WARM, a, cx - R - 12, cy - R - 12, cx + R + 12, cy + 14);
    }
  }
  // reminders: a swaying bell above the eyes; its count is in the digit stencil
  if (s.reminders > 0 && s.rem_a > 0.01f) led_bell(106, 42, sinf(t * 2.4f) * 0.12f, clamp01(s.rem_a));
  // no connection: the red Wi-Fi crossed out through its middle, clear of the eyes
  if (s.offl_a > 0.01f) {
    const float a = clamp01(s.offl_a), k = (0.9f + 0.1f * sinf(t * 2.2f)) * a, cx = C, cy = 66;
    for (const float R : {15.0f, 28.0f, 41.0f}) sh_shape(sd_arc(cx, cy, R, -PI / 2, 0.8f, 6), L_HOT, k, cx - 52, cy - 52, cx + 52, cy + 12);
    sh_shape(sd_disc(cx, cy, 4.6f), L_HOT, k, cx - 12, cy - 12, cx + 12, cy + 12);
    sh_shape(sd_line(cx - 32, cy - 46, cx + 32, cy - 6, 4.5f), {255, 255, 255}, a, cx - 44, cy - 58, cx + 44, cy + 6);
  }
  // quiet mode: a crossed-out speaker below the sleepy eyes
  if (s.quiet_a > 0.01f) {
    const float a = clamp01(s.quiet_a), cx = 116, cy = 168;
    sh_shape(sd_box(cx - 28, cy, 8, 12, 2), L_GREY, 0.95f * a, cx - 44, cy - 28, cx - 12, cy + 28);
    sh_shape([=](float x, float y) { return fmaxf(fmaxf(fabsf(y - cy) - (12 + 16 * (x - (cx - 20)) / 24), (cx - 20) - x), x - (cx + 4)); }, L_GREY,
             0.95f * a, cx - 28, cy - 36, cx + 14, cy + 36);
    for (const float R : {22.0f, 34.0f}) sh_shape(sd_arc(cx + 4, cy, R, 0, 0.7f, 4), L_GREY, 0.6f * a, cx, cy - 44, cx + 52, cy + 44);
    sh_shape(sd_line(cx - 40, cy - 30, cx + 44, cy + 30, 7), L_RED, a, cx - 52, cy - 42, cx + 56, cy + 42);
  }
  if (s.game != Game::None && s.game_a > 0.01f) led_game(s);
}

void init_led() {
  for (int k = 0; k < LN; k++) {
    s_rain_on[k] = hash3(k - LK, 2, 5) < 0.5f ? 1.0f : 0.0f;
    s_rain_ph[k] = hash3(k - LK, 0, 9);
  }
  memset(s_dots, 0, sizeof s_dots);
}

void build_led(const Scene &s) {
  const float t = s.t, dt = s_led_t < 0 ? 0.016f : fminf(0.05f, fmaxf(0.001f, t - s_led_t));
  s_led_t = t;
  if (s_boot_t0 < 0) s_boot_t0 = t;  // power-on: a top-down sweep over 1.5 s
  const float boot = clamp01((t - s_boot_t0) / 1.5f), front = boot * 290 - 14;
  s_boot = boot;
  const float lis = s.led_lis, thk = s.led_thk, spk = s.led_spk, mus = s.led_mus, amp = s.led_amp;
  const float act = fmaxf(lis, spk), bt = fmodf(t * 2.1f, 1), nb = 1 - bt, beat = mus * nb * nb * nb;
  // edge wave: each bar rises fast and settles slowly (soft dynamics)
  const float ka = 1 - expf(-16 * dt), kr = 1 - expf(-4.5f * dt);
  for (int i = 0; i < NB; i++) {
    const float mirror = fabsf((float)i / NB - 0.5f) * 2;
    const float band = 0.35f + 0.65f * fabsf(sinf(i * 0.83f + t * 6.1f) * cosf(i * 0.31f - t * 3.7f + mirror * 2));
    const float flow = 0.3f + 0.7f * (0.5f + 0.5f * sinf(i * 0.35f - t * 2.4f)) * (0.5f + 0.5f * sinf(i * 0.11f + t * 0.9f));
    const float tgt = fmaxf(act * amp * band, mus * (0.26f + 0.62f * beat) * flow) * (0.5f + 0.5f * mirror);
    s_spec[i] += (tgt - s_spec[i]) * (tgt > s_spec[i] ? ka : kr);
  }
  // eyes: the same shapes as the old face, a bit narrower and shifted to fit the grid
  Eye le[2];
  EyeGeo eg[2];
  for (int e = 0; e < 2; e++) {
    le[e] = s.eye[e];
    le[e].cx = C + (s.eye[e].cx - C) * 0.74f;
    le[e].cy = s.eye[e].cy - 4;
    le[e].w = fmaxf(1, s.eye[e].w * 0.76f);
    le[e].h = fmaxf(3.5f, s.eye[e].h * 0.87f);
    le[e].r = s.eye[e].r * 0.85f;
    eg[e] = eye_geo(le[e]);
  }
  // eye tint by state: violet (thinking), cyan (speaking), amber (timer); listening and music keep white eyes
  const float tw = s.alert_mode ? fminf(1, s.timer_a) : 0;  // the eyes turn amber only when the timer has fired
  const float wsum = thk + spk + tw;
  Rgb eye_col = rgb(s.eye_col);
  if (wsum > 0.01f) {
    const Rgb ac = {(L_THINK.r * thk + L_SPEAK.r * spk + L_TIMER.r * tw) / wsum, (L_THINK.g * thk + L_SPEAK.g * spk + L_TIMER.g * tw) / wsum,
                    (L_THINK.b * thk + L_SPEAK.b * spk + L_TIMER.b * tw) / wsum};
    eye_col = lerp_rgb(eye_col, ac, 0.3f * clamp01(wsum));
  }
  // stencil: time, timer, "Z"
  memset(s_txt, 0, sizeof s_txt);
  const Rgb ink = rgb(s.ink);
  if (s.have_time && s.clock_a > 0.01f) {
    char tm[8];
    snprintf(tm, sizeof tm, "%02d:%02d", s.hour % 100, s.minute % 100);
    stamp_time(tm, 1, 6, (uint8_t)(255 * clamp01(s.clock_a)), 0, sinf(t * PI) > 0 || s.clock_a < 0.6f);
  }
  // reminders badge: the count in dots next to the bell (the bell itself is a shape, see build_shapes)
  if (s.reminders > 0 && s.rem_a > 0.01f) {
    const uint8_t ra = (uint8_t)(255 * clamp01(s.rem_a));
    char nb[8];
    snprintf(nb, sizeof nb, "%d", s.reminders > 99 ? 99 : s.reminders);
    const int nd = (int)strlen(nb);
    int k = -((3 * nd + nd - 1) / 2) + 5;
    for (const char *q = nb; *q; q++, k += 4) stamp_glyph(G3[*q - '0'], k, -16, 1, ra, 0);
  }
  if (s.timer_a > 0.01f) {
    const uint32_t secs = (s.timer_rem + 999) / 1000;
    char tm[12];
    if (secs >= 3600) snprintf(tm, sizeof tm, "%u:%02u", (unsigned)(secs / 3600), (unsigned)(secs / 60 % 60));
    else snprintf(tm, sizeof tm, "%02u:%02u", (unsigned)(secs / 60), (unsigned)(secs % 60));
    stamp_time(tm, 1, 4, (uint8_t)(255 * clamp01(s.timer_a) * (s.timer_blink ? 1.0f : 0.3333f)), 1, true);
  }
  // phone cards: the battery gauge, the list icons and the player progress are dots too
  if (s.card_a > 0.01f && s.card == Card::Battery && !s.player) {
    const int lvl = s.card_level < 0 ? 0 : (s.card_level > 100 ? 100 : s.card_level), nseg = (lvl + 9) / 10;
    const uint8_t ci = lvl <= 10 ? 5 : (lvl <= 30 ? 6 : 4);
    for (int i = 0; i < 10; i++) {
      static const char *const SEG[2] = {"##", "##"};
      stamp_sprite(SEG, 2, -14 + i * 3, 2, (uint8_t)(255 * clamp01(s.card_a) * (i < nseg ? 1.0f : 0.24f)), i < nseg ? ci : 0);
    }
  }
  if (s.card_a > 0.01f && s.card == Card::Check && !s.player) {
    static const char *const BELL5[5] = {"..#..", ".###.", ".###.", "#####", "..#.."};
    static const char *const PHONE5[5] = {"#####", "#...#", "#...#", "#...#", "#####"};
    static const char *const LAPTOP5[5] = {".###.", "#...#", "#...#", "#####", "#####"};
    static const char *const BUDS5[5] = {"##.##", "##.##", ".#.#.", ".#.#.", ".#.#."};
    char tmp[4][96];
    const int n = check_split(s.card_text, tmp);
    const float step = 36, y0 = n > 3 ? 92 : 114;
    for (int i = 0; i < n; i++) {
      // four rows do not fit with 5-dot icons and a gap: they get compact 4-dot icons
      static const char *const BELL4[4] = {"..#..", ".###.", "#####", "..#.."};
      static const char *const PHONE4[4] = {"#####", "#...#", "#...#", "#####"};
      static const char *const LAPTOP4[4] = {".###.", "#...#", "#####", "#####"};
      static const char *const BUDS4[4] = {"##.##", "##.##", ".#.#.", ".#.#."};
      const bool compact = n > 3;
      const char *const *ic = compact ? BUDS4 : BUDS5;
      switch (check_kind(tmp[i])) {
        case 0: ic = compact ? BELL4 : BELL5; break;
        case 1: ic = compact ? PHONE4 : PHONE5; break;
        case 2: ic = compact ? LAPTOP4 : LAPTOP5; break;
        default: break;
      }
      const float ra = clamp01((s.card_t - 0.2f - i * 0.18f) / 0.4f);
      const int rows = compact ? 4 : 5;
      stamp_sprite(ic, rows, -14, (int)lroundf((y0 + i * step - 120) / LP) - 2, (uint8_t)(255 * clamp01(s.card_a) * ra), 3);
    }
  }
  if (s.card_a > 0.01f && s.player) {
    for (int k = -9; k <= 9; k++) {
      static const char *const DOT[1] = {"#"};
      stamp_sprite(DOT, 1, k, 3, (uint8_t)(255 * clamp01(s.card_a) * ((k + 9) / 18.0f < s.player_frac ? 1.0f : 0.3f)), 4);
    }
  }
  // sleep: two "Z" drift up and to the right, a small one first and a large one later
  if (s.led_slp > 0.01f) {
    static const char *const Z_S[5] = {"###", "..#", ".#.", "#..", "###"};
    static const char *const Z_L[5] = {"#####", "...#.", "..#..", ".#...", "#####"};
    for (int i = 0; i < 2; i++) {
      const float u = fmodf(t * 0.17f + i * 0.5f, 1.0f);
      stamp_sprite(u < 0.45f ? Z_S : Z_L, 5, 7 + (int)lroundf(u * 6), -3 - (int)lroundf(u * 10), (uint8_t)(255 * (0.7f / 0.9f) * sinf(u * PI) * clamp01(s.led_slp)), 7);
    }
  }
  build_shapes(s);
  const float base = 0.075f * s.bg;  // the grid under text stays as it is: the text carries its own thin outline
  const float tint = s.night_tint, happy_k = 1 - clamp01(s.happy);
  const int wkey = mus > fmaxf(lis, spk) ? 2 : (lis > spk ? 1 : 0);
  const float rate = s.timer_total ? (float)s.timer_rem / s.timer_total : 0;
  for (int m = -LK; m <= LK; m++)
    for (int k = -LK; k <= LK; k++) {
      Dot &D = s_dots[m + LK][k + LK];
      const float x = C + k * LP, y = C + m * LP, dx = x - C, dy = y - C, r2 = dx * dx + dy * dy;
      if (r2 > 118.0f * 118.0f || (boot < 1 && y > front)) {
        D.a = 0;
        continue;
      }
      const float r = sqrtf(r2);
      float v = 0;
      Rgb col = eye_col;
      for (int e = 0; e < 2; e++) {
        if (fabsf(x - le[e].cx) > le[e].w + 9 || fabsf(y - le[e].cy) > le[e].h + fabsf(le[e].bend) + 9) continue;
        float py;
        const float d = eye_sdf(eg[e], x - le[e].cx, y - le[e].cy, py);
        const float q = clamp01(0.5f - d / (LP * 1.05f)) * le[e].bright * happy_k;
        if (q > v) v = q;
      }
      // time, timer, "Z"
      const uint8_t ta = s_txt[m + LK][k + LK];
      if (ta) {
        const float tv = ta * (1 / 255.0f) * 0.9f;
        if (tv > v) {
          v = tv;
          const uint8_t ci = s_txt_c[m + LK][k + LK];
          col = ci == 0 ? ink : *TXT_COL[ci];
        }
      }
      const Shp &shp = s_sh[m + LK][k + LK];  // shapes: joy, bell, Wi-Fi, speaker, games
      if (shp.v) {
        const float sv = shp.v * (1 / 255.0f);
        if (sv > v) {
          v = sv;
          col = {(float)shp.r, (float)shp.g, (float)shp.b};
        }
      }
      float ang = -1;  // angle from the top, clockwise, 0..1 (only when needed)
      auto angle = [&]() {
        if (ang < 0) {
          ang = atan2f(dx, -dy) * (1 / TAU);
          if (ang < 0) ang += 1;
        }
        return ang;
      };
      const bool ring_zone = r > 86;
      float a = 0;
      if (ring_zone && (act > 0.02f || mus > 0.02f)) {
        a = atan2f(dx, -dy) * (1 / TAU);
        if (a < 0) a += 1;
        const float sp = s_spec[(int)(a * NB) % NB], len = sp * 28, edge = 118 - r;
        if (sp > 0.03f && edge < len) {
          const float w = edge / fmaxf(len, 1);
          const float wv = 0.35f + 0.65f * (1 - w);
          if (wv > v) {
            v = wv;
            if (wkey == 2) col = lerp_rgb(L_MUS_A, L_MUS_B, w);               // music: two colours along the bar
            else if (wkey == 1) col = hsl(a * 360 + t * 45, 0.95f, 0.56f + 0.1f * (1 - w));  // listening: full rainbow
            else col = lerp_rgb(L_WHITE, L_SPEAK, clamp01(w * 1.5f));          // speaking: white to cyan
          }
        }
      }
      if (s.card == Card::Call && s.card_a > 0.01f && r > 80) {  // incoming call: a green-cyan wave around the edge
        float ca = atan2f(dx, -dy) * (1 / TAU);
        if (ca < 0) ca += 1;
        const float len = (0.35f + 0.65f * fabsf(sinf(ca * TAU * 3 - t * 4))) * (0.55f + 0.45f * sinf(t * 2.2f)) * s.card_a * 28, edge = 118 - r;
        if (len > 3 && edge < len) {
          const float w = edge / len, cv = 0.35f + 0.65f * (1 - w);
          if (cv > v) v = cv, col = lerp_rgb(L_GREEN, L_SPEAK, w * 0.7f);
        }
      }
      if (s.card == Card::Wake && s.card_a > 0.01f && r > 109.5f && r < 117) {  // alarm off, you are asleep: an amber ring pulse
        const float pv = s.card_a * (0.4f + 0.6f * (0.5f + 0.5f * sinf(t * TAU * 1.1f)));
        if (pv > v) v = pv, col = L_TIMER;
      }
      if (r > 109.5f) {
        if (!(act > 0.02f || mus > 0.02f) && (thk > 0.02f || s.timer_a > 0.05f)) {
          a = atan2f(dx, -dy) * (1 / TAU);
          if (a < 0) a += 1;
        }
        if (thk > 0.02f) {  // the comet runs clockwise, head in front, tail behind
          float da = fmodf(t * 0.3f - a, 1);
          if (da < 0) da += 1;
          const float cm = thk * powf(clamp01(1 - da * 3.2f), 1.6f);
          if (cm > 0.04f && cm > v) {
            v = cm;
            col = cm > 0.7f ? lerp_rgb(L_THINK, L_WHITE, 0.4f) : L_THINK;
          }
        }
      }
      if (s.timer_a > 0.05f && !s.alert_mode && r > 105.5f && r < 117) {  // timer: a rich ring that drains clockwise
        // brightest and lightest at the head (the leading edge), warmer and dimmer toward the tail; a bright spot rides the head
        const float behind = rate - angle(), q = clamp01(behind * (1 / 0.009f) + 0.5f), g = clamp01(1 - behind / 0.45f);
        float tv = q > 0.02f ? s.timer_a * (0.45f + 0.55f * g) * q : 0;
        Rgb tc = lerp_rgb({255, 120, 40}, L_TIMER, g);
        if (behind > -0.004f && behind < 0.03f) tv = s.timer_a, tc = {255, 226, 150};
        if (q < 1 && tv < 0.12f) tv = 0.12f, tc = L_TIMER;  // the empty track
        if (tv > v) v = tv, col = tc;
      }
      if (s.timer_a > 0.05f && s.alert_mode && r > 109.5f && r < 117) {  // fired: the whole ring pulses
        const float pv = s.timer_pulse * fminf(1, s.timer_a);
        if (pv > v) v = pv, col = L_TIMER;
      }
      if (s.alert_mode && s.timer_a > 0.05f && r > 80) {  // fired: an amber wave along the edge breathes with the pulse
        const float len = 0.55f * s.timer_pulse * 16, edge = 118 - r;
        if (len > 3 && edge < len) {
          const float w = edge / len, rv = 0.35f + 0.65f * (1 - w);
          if (rv > v) v = rv, col = lerp_rgb(L_TIMER, {255, 120, 40}, w);
        }
      }
      if (s.task != Task::Idle && s.task_a > 0.01f && r > 109.5f && r < 117) {  // task: a ring of dots by state
        float tv = 0;
        Rgb tc = L_BLUE;
        switch (s.task) {
          case Task::Working:
            if (s.task_spin < 0) {  // known progress: the ring fills clockwise, a white spark runs along it
              const float fr = s.task_frac, q = clamp01((fr - angle()) * (1 / 0.009f) + 0.5f);
              tv = q > 0.02f ? 0.9f * s.task_a * q : 0;
              if (q < 1 && tv < 0.1f) tv = 0.1f;
              const float d = fmodf(angle() - fmodf(t * 0.6f, 1.0f) + 1, 1.0f);
              if (d < 0.05f && angle() < fr) tv = 1, tc = L_WHITE;
            } else {  // unknown progress: a comet runs around the ring
              float d = fmodf(s.task_spin / TAU - angle(), 1.0f);
              if (d < 0) d += 1;
              const float f = d < 0.32f ? 1 - d / 0.32f : 0;
              tv = fmaxf(0.1f, 0.9f * s.task_a * f * f * (3 - 2 * f));
            }
            break;
          case Task::NeedsInput:
          case Task::NeedsPermission:
            tc = L_TIMER;
            tv = s.task_a * (0.4f + 0.6f * (0.5f + 0.5f * sinf(t * TAU * 1.1f)));
            break;
          case Task::Done:
            tc = L_GREEN;
            tv = 0.85f * s.task_a;
            break;
          case Task::Failed:
            tc = L_RED;
            tv = 0.85f * s.task_a;
            break;
          case Task::Idle:
            break;
        }
        if (tv > v) v = tv, col = tc;
      }
      // thinking: a quiet, muted rain over the background; eyes and time are not touched by it
      if (thk > 0.02f && r < 112 && s_rain_on[k + LK] > 0) {
        const float ph = fmodf(t * 0.5f + s_rain_ph[k + LK], 1), ey = ph * (240 + 6 * LP) - 3 * LP, dd = (y - ey) / LP;
        const float drop = (dd <= 0.5f && dd > -5) ? 1 - fmaxf(0, -dd) / 5 : 0, rv = 0.3f * drop * thk;
        if (rv > v) v = rv, col = L_THINK;
      }
      float ba = base;
      if (boot < 1) {  // sweep: a bright edge runs top to bottom
        if (y > front - 12) v = fmaxf(v, 0.95f), col = L_WHITE;
      }
      if (v < 0.001f) col = tint > 0.01f ? lerp_rgb(L_WHITE, L_GRID_RED, tint) : L_WHITE;  // sleep / night: the grid turns red
      const float alpha = ba + (1 - ba) * v;
      D.r = u8f(col.r);
      D.g = u8f(col.g);
      D.b = u8f(col.b);
      D.a = u8f(alpha * 255);
      D.v = u8f(v * 255);
      if (!D.a) D.a = 1;  // the dot exists even if barely visible
    }
}

// Per-axis coverage of a square of half-size h by the pixel |j| steps away from its centre (centres sit on pixel
// borders, so the geometry is symmetric); 64 brightness levels, values 0..255
uint8_t s_cov_core[64][4], s_cov_bloom[64][6];

void init_led_tables() {
  for (int l = 0; l < 64; l++) {
    const float v = (l + 0.5f) / 64, h = LP * (0.64f + 0.16f * v) * 0.5f;
    for (int j = 0; j < 4; j++) s_cov_core[l][j] = u8f(255 * clamp01(h - j));
    for (int j = 0; j < 6; j++) s_cov_bloom[l][j] = u8f(255 * clamp01(2 * h - j));
  }
}

// Blend a colour into one pixel, alpha 0..256
inline void blend(uint8_t *p, int r, int g, int b, int a) {
  p[0] = (uint8_t)(p[0] + (((r - p[0]) * a) >> 8));
  p[1] = (uint8_t)(p[1] + (((g - p[1]) * a) >> 8));
  p[2] = (uint8_t)(p[2] + (((b - p[2]) * a) >> 8));
}

// One square of the LED grid: optional glow spilling onto the neighbours, then the bright core
IRAM_ATTR inline void led_dot(int cx, int cy, const Dot &D) {
  const int lvl = D.v >> 2;
  const int glow = D.v > 115 ? (int)(0.16f * D.v) : 0;  // glow opacity 0..40 of 255
  const uint8_t *cc = s_cov_core[lvl], *cb = s_cov_bloom[lvl];
  const int reach = glow ? 5 : 3;
  for (int ty = -reach; ty < reach; ty++) {
    const int y = cy + ty;
    if (y < s_y0 || y >= s_y1) continue;
    const int jy = ty >= 0 ? ty : -ty - 1;
    const int gy = glow && jy < 6 ? cb[jy] : 0, ky = jy < 4 ? cc[jy] : 0;
    if (!gy && !ky) continue;
    for (int tx = -reach; tx < reach; tx++) {
      const int x = cx + tx;
      if (x < 0 || x >= W) continue;
      const int jx = tx >= 0 ? tx : -tx - 1;
      uint8_t *p = px(x, y);
      if (gy && jx < 6) {
        const int ga = (cb[jx] * gy >> 8) * glow >> 8;  // 0..255 * glow/256
        if (ga) blend(p, D.r, D.g, D.b, ga);
      }
      if (ky && jx < 4) {
        const int ka = (cc[jx] * ky >> 8) * D.a >> 8;
        if (ka) blend(p, D.r, D.g, D.b, ka);
      }
    }
  }
}

IRAM_ATTR void draw_led() {
  memset(s_work, 0, (size_t)(s_y1 - s_y0) * W * 3);
  int mlo = (int)ceilf((s_y0 - 5 - C) / LP), mhi = (int)floorf((s_y1 + 5 - C) / LP);
  if (mlo < -LK) mlo = -LK;
  if (mhi > LK) mhi = LK;
  for (int m = mlo; m <= mhi; m++) {
    const int cy = C + m * 6;
    for (int k = -LK; k <= LK; k++) {
      const Dot &D = s_dots[m + LK][k + LK];
      if (D.a) led_dot(C + k * 6, cy, D);
    }
  }
}

const char *const DAYS_L[7] = {"вс", "пн", "вт", "ср", "чт", "пт", "сб"};
const char *const MON_S[12] = {"янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"};

// Under the time: "ср 21 окт · 12°"; when there is an upcoming item the two lines cross-fade
void draw_led_date(const Scene &s) {
  if (!s.have_time || s.clock_a < 0.01f || s_y1 < 190 || s_y0 > 216) return;
  const float boot_a = s_boot < 1 ? clamp01((s_boot * 290 - 14 - 198) / 14) : 1;
  char line[96];
  int n = snprintf(line, sizeof line, "%s %d %s", DAYS_L[s.wday % 7], s.mday, MON_S[s.month % 12]);
  if (s.wcond >= 0) {
    if (s.wtemp < 0) snprintf(line + n, sizeof line - n, "  \xC2\xB7  \xE2\x88\x92%d\xC2\xB0", -s.wtemp);
    else snprintf(line + n, sizeof line - n, "  \xC2\xB7  %d\xC2\xB0", s.wtemp);
  }
  float date_a = 1, agenda_a = 0;
  char agenda[96] = "";
  if (s.agenda_accent[0] || s.agenda_text[0]) {
    snprintf(agenda, sizeof agenda, "%s%s%s", s.agenda_accent, s.agenda_accent[0] && s.agenda_text[0] ? " " : "", s.agenda_text);
    const float q = fmodf(s.t, 12.0f);  // 5.6 s date, cross-fade, 5.2 s agenda
    agenda_a = smooth(q < 5.6f ? 0 : (q < 6.2f ? (q - 5.6f) / 0.6f : (q < 11.4f ? 1 : (12 - q) / 0.6f)));
    date_a = 1 - agenda_a;
  }
  const Rgb ink = rgb(s.ink);
  char fitted[96];
  const float a = s.clock_a * boot_a * 0.55f * (1 - clamp01(s.led_slp));  // the sleep screen has no date line
  if (date_a > 0.01f) draw_text(text::kUi, line, C, 207, 1, true, ink, a * date_a);
  if (agenda_a > 0.01f) {
    fit(text::kUi, agenda, 170, fitted, sizeof fitted);
    draw_text(text::kUi, fitted, C, 207, 1, true, ink, a * agenda_a * 1.3f);
  }
}

// Timer label under the digits
void draw_led_timer_label(const Scene &s) {
  if (s.timer_a < 0.01f || s_y1 < 184 || s_y0 > 212) return;
  // mockup: 13 px semibold under the digits, "Готово" 16 px when fired
  if (s.alert_mode) draw_text(text::kUiBold, "Готово", C, 200, 16.0f / 15, true, {255, 179, 64}, s.timer_a);
  else draw_text(text::kUiBold, s.timer_label, C, 200, 13.0f / 15, true, {255, 179, 64}, s.timer_a * 0.9f);
}

// Text under the eyes. There is a lot of free room below them (from y 145 to the edge), so the text gets up to
// three lines. What was heard / said keeps the calm regular look; the reply is the first sentence in a big
// font when it fits in two lines, otherwise in bold at three.
void draw_led_caption(const Scene &s) {
  const float a = s.cap_a;
  if (a < 0.02f || s.caption == Caption::None || !s.caption_text[0] || s_y1 < 140 || s_y0 > 210) return;
  const float lift = (1 - a) * 6;
  char lines[4][128];
  if (s.caption == Caption::Heard || s.caption == Caption::Said) {
    static const float W3[3] = {188, 176, 164};
    const int n = wrap_n(text::kUi, s.caption_text, W3, 3, lines);
    const float k = s.caption == Caption::Heard ? 0.9f : 0.55f, y0 = n == 1 ? 190 : (n == 2 ? 176 : 158);
    for (int i = 0; i < n; i++) draw_text(text::kUi, lines[i], C, y0 + i * 20 + lift, 1, true, rgb(s.ink), a * k);
    return;
  }
  if (s.led_spk < 0.02f) return;
  char one[128];
  size_t len = 0;
  for (const char *p = s.caption_text; *p && len + 1 < sizeof one; p++) {
    if (*p == '.' || *p == '!' || *p == '?' || *p == '\n') break;
    one[len++] = *p;
  }
  one[len] = 0;
  const float al = a * s.led_spk * 0.95f;
  bool cut = false;
  static const float WT[2] = {176, 164}, WB[3] = {188, 176, 164};
  int n = wrap_n(text::kTitle, one, WT, 2, lines, &cut);
  if (!cut) {
    const float y0 = n == 1 ? 184 : 168;
    for (int i = 0; i < n; i++) draw_text(text::kTitle, lines[i], C, y0 + i * 27 + lift, 1, true, rgb(s.ink), al);
    return;
  }
  n = wrap_n(text::kUiBold, one, WB, 3, lines);
  const float y0 = n == 1 ? 190 : (n == 2 ? 176 : 158);
  for (int i = 0; i < n; i++) draw_text(text::kUiBold, lines[i], C, y0 + i * 20 + lift, 1, true, rgb(s.ink), al);
}

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
    draw_led();
  }
  {
    Tick t(pf.over);
    draw_led_date(s);
    draw_led_timer_label(s);
    draw_led_caption(s);
    draw_card(s);
    draw_task(s);
    draw_game(s);
  }
  pf.strips++;
}

// Debug screenshot: the sender copies the strips of the next whole frame (exactly what goes to the panel)
// into a PSRAM buffer. 0 idle, 1 armed, 2 capturing, 3 ready.
std::atomic<int> s_shot_state{0};
uint8_t *s_shot = nullptr;

void shot_capture(int k, const uint8_t *strip) {
  int st = s_shot_state.load();
  if (st == 1 && k == 0) s_shot_state.store(st = 2);
  if (st != 2 || !s_shot) return;
  memcpy(s_shot + (size_t)k * W * SH * 2, strip, (size_t)W * SH * 2);
  if (k == NSTRIP - 1) s_shot_state.store(3);
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
      shot_capture((int)(j % NSTRIP), s_out[j % NOUT]);
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
  init_led();
  init_led_tables();
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

bool shot_arm() {
  if (!s_shot) s_shot = (uint8_t *)heap_caps_malloc((size_t)W * H * 2, MALLOC_CAP_SPIRAM);
  if (!s_shot) return false;
  s_shot_state.store(1);
  return true;
}

const uint8_t *shot_ready() { return s_shot_state.load() == 3 ? s_shot : nullptr; }

void shot_release() { s_shot_state.store(0); }

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
    build_led(s);
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
