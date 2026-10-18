#pragma once
// Быстрый рендер-движок лица для GC9A01 240x240, приближенный к превью v6.
// Скорость: фон считается 1 раз на смену состояния (BGBUF, туда запечены виньетка,
// аврора, отблеск и пылинки), каждый кадр — memcpy полосы глаз + построчная
// отрисовка глаз + частичная выгрузка (шлём только полосу глаз; фон/часы статичны).
// Глаз: свечение-ореол + вертикальный градиент + мягкий блик + стеклянный кант +
// сглаживание боковых краёв. "Говорю" — отдельная дуга-улыбка (по столбцам).
#include "esphome.h"
#include "esp_heap_caps.h"
#include <cmath>
#include <cstdlib>
#include <cstring>

namespace face {

using esphome::display::Display;

static const int W = 240, H = 240;
static const float TAUF = 6.28318530718f;
static uint8_t *FB = nullptr;
static uint8_t *BGBUF = nullptr;
static bool s_init = false;

static inline float clampf(float x, float a, float b) { return x < a ? a : (x > b ? b : x); }
static inline float mixf(float a, float b, float t) { return a + (b - a) * t; }
static inline uint8_t cl8(float v) { return (uint8_t)(v < 0 ? 0 : (v > 255 ? 255 : v + 0.5f)); }
static inline float sstep(float e0, float e1, float x) { float t = clampf((x - e0) / (e1 - e0), 0.f, 1.f); return t * t * (3.f - 2.f * t); }

struct C3 { float r, g, b; };
static inline C3 mix3(C3 a, C3 b, float t) { return { mixf(a.r, b.r, t), mixf(a.g, b.g, t), mixf(a.b, b.b, t) }; }
static inline void tw(C3 &c, float k) { c.r = mixf(c.r, 255.f, k); c.g = mixf(c.g, 255.f, k); c.b = mixf(c.b, 255.f, k); }
static inline uint16_t pack565(float r, float g, float b) {
  uint8_t R = cl8(r), G = cl8(g), B = cl8(b);
  return ((R & 0xF8) << 8) | ((G & 0xFC) << 3) | (B >> 3);
}
static inline void putFB(int x, int y, uint16_t c) {
  if ((unsigned)x >= (unsigned)W || (unsigned)y >= (unsigned)H) return;
  int i = (y * W + x) * 2; FB[i] = c >> 8; FB[i + 1] = (uint8_t)c;
}
// смешать пиксель FB с цветом col по k (0..1) — мягкость поверх фона/глаза
static inline void blendFB(int x, int y, C3 col, float k) {
  if ((unsigned)x >= (unsigned)W || (unsigned)y >= (unsigned)H || k <= 0.003f) return;
  int i = (y * W + x) * 2;
  uint16_t bc = (FB[i] << 8) | FB[i + 1];
  float br = ((bc >> 11) & 0x1F) * 8.226f, bg = ((bc >> 5) & 0x3F) * 4.048f, bb = (bc & 0x1F) * 8.226f;
  uint16_t c = pack565(mixf(br, col.r, k), mixf(bg, col.g, k), mixf(bb, col.b, k));
  FB[i] = c >> 8; FB[i + 1] = (uint8_t)c;
}
static inline void fillRect(int x, int y, int w, int h, uint16_t c) {
  for (int yy = y; yy < y + h; yy++) if ((unsigned)yy < (unsigned)H) { uint8_t *r = FB + yy * W * 2; for (int xx = x; xx < x + w; xx++) if ((unsigned)xx < (unsigned)W) { r[xx * 2] = c >> 8; r[xx * 2 + 1] = (uint8_t)c; } }
}
static inline void fillCircle(int cx, int cy, int rad, uint16_t c) {
  for (int yy = -rad; yy <= rad; yy++) for (int xx = -rad; xx <= rad; xx++) if (xx * xx + yy * yy <= rad * rad) putFB(cx + xx, cy + yy, c);
}
// мягкий кружок: центр ярче, край растворяется (для бликов и пылинок)
static void softDot(int cx, int cy, float rad, C3 col, float amax) {
  int ri = (int)ceilf(rad);
  float inv = 1.0f / (rad * rad);
  for (int yy = -ri; yy <= ri; yy++) for (int xx = -ri; xx <= ri; xx++) {
    float d2 = (float)(xx * xx + yy * yy) * inv; if (d2 > 1) continue;
    blendFB(cx + xx, cy + yy, col, amax * (1 - d2));
  }
}

static const uint8_t SEG[10] = {0x3F,0x06,0x5B,0x4F,0x66,0x6D,0x7D,0x07,0x7F,0x6F};
static void seg7(int x, int y, int w, int h, int t, int d, uint16_t c) {
  if (d < 0 || d > 9) return;
  uint8_t s = SEG[d]; int half = (h - 3 * t) / 2;
  if (s & 0x01) fillRect(x + t, y, w - 2 * t, t, c);
  if (s & 0x20) fillRect(x, y + t, t, half, c);
  if (s & 0x02) fillRect(x + w - t, y + t, t, half, c);
  if (s & 0x40) fillRect(x + t, y + t + half, w - 2 * t, t, c);
  if (s & 0x10) fillRect(x, y + 2 * t + half, t, half, c);
  if (s & 0x04) fillRect(x + w - t, y + 2 * t + half, t, half, c);
  if (s & 0x08) fillRect(x + t, y + h - t, w - 2 * t, t, c);
}

struct EyeP { float hw, hh, bend; C3 ct, cb; };
static inline EyeP paramFor(int st) {
  switch (st) {
    case 1: return { 45, 54, 0,  {135,208,255}, {30,112,236} };
    case 2: return { 41, 31, 0,  {255,208,96},  {230,132,24} };
    case 3: return { 44, 29, 26, {152,242,152}, {54,190,100} };
    case 4: return { 40, 46, 0,  {150,245,190}, {36,185,130} };  // игривое (весёлый мятный)
    case 5: return { 40, 46, 0,  {255,220,120}, {120,80,20} };   // монетка (тёплое золото фона)
    case 6: return { 40, 46, 0,  {120,150,200}, {16,26,52} };    // крестики-нолики (тёмно-синий фон)
    case 7: return { 40, 50, 0,  {40,70,110},   {3,7,20} };      // сон (ночь, глубокий синий)
    case 8: return { 40, 46, 0,  {170,250,200}, {40,175,150} };  // радость (тёплый мятный)
    case 9: return { 52, 52, 0,  {255,190,120}, {175,44,30} };   // тревога (тревожный янтарь/алый)
    case 10:return { 40, 46, 0,  {210,220,235}, {40,55,85} };    // кубик
    case 11:return { 40, 46, 0,  {230,215,255}, {45,35,85} };    // колесо фортуны
    case 12:return { 40, 46, 0,  {200,225,255}, {35,55,95} };    // камень-ножницы-бумага
    case 13:return { 40, 46, 0,  {110,130,180}, {6,10,34} };     // magic-8-ball
    default:return { 40, 50, 0,  {96,242,214},  {0,146,156} };
  }
}
static inline uint32_t hash32(uint32_t a, uint32_t s) {
  uint32_t h = (a * 2654435761u) ^ (s * 40503u + 0x9E3779B9u);
  h ^= h >> 13; h *= 0x85EBCA6Bu; h ^= h >> 16; return h;
}

// Фон: виньетка + аврора (2 пятна) + отблеск под глазами + пылинки. Тон bgc/tp.
static void buildBG(C3 bgc, C3 tp) {
  for (int y = 0; y < H; y++) {
    float dyv = (y - 120) * (1.0f / 118.0f); float dyv2 = dyv * dyv;
    uint8_t *row = BGBUF + y * W * 2;
    for (int x = 0; x < W; x++) {
      float dxv = (x - 120) * (1.0f / 118.0f);
      float vig = 1.0f - (dxv * dxv + dyv2); if (vig < 0) vig = 0; vig *= vig * 0.30f;
      float ax = x - 80, ay = y - 92;  float a1 = 1.0f - (ax * ax + ay * ay) * (1.0f / 13000.f); if (a1 < 0) a1 = 0; a1 *= a1 * 0.13f;
      float bx = x - 168, by = y - 150; float a2 = 1.0f - (bx * bx + by * by) * (1.0f / 13000.f); if (a2 < 0) a2 = 0; a2 *= a2 * 0.10f;
      float pxx = x - 120, pyy = y - 182; float pd = 1.0f - (pxx * pxx * (1.0f / 8000.f) + pyy * pyy * (1.0f / 700.f)); if (pd < 0) pd = 0; pd *= pd * 0.11f;
      float kd = vig + pd, ka = a1 + a2;   // виньетка/отблеск — тоном cb; аврора — тоном ct
      uint16_t c = pack565(bgc.r * kd + tp.r * ka, bgc.g * kd + tp.g * ka, bgc.b * kd + tp.b * ka);
      row[x * 2] = c >> 8; row[x * 2 + 1] = (uint8_t)c;
    }
  }
  // пылинки — фиксированные мягкие точки (амбиент)
  static const uint8_t PX[22] = {30,54,88,150,200,215,42,120,190,66,110,175,225,18,72,138,205,95,160,48,132,182};
  static const uint8_t PY[22] = {40,150,26,20,60,120,200,90,170,55,210,140,44,110,185,66,100,30,220,175,155,205};
  uint8_t *save = FB; FB = BGBUF;   // softDot пишет в FB — временно направим в BGBUF
  C3 spark = { mixf(tp.r,255,0.5f), mixf(tp.g,255,0.5f), mixf(tp.b,255,0.5f) };
  for (int i = 0; i < 22; i++) softDot(PX[i], PY[i], 1.7f + (i % 3), spark, 0.28f);
  FB = save;
}

// SDF скруглённого прямоугольника (для 2D-свечения). sqrt только вне тела.
static inline float sdRound(float px_, float py_, float bx, float by, float r) {
  float qx = fabsf(px_) - (bx - r), qy = fabsf(py_) - (by - r);
  if (qx < 0 && qy < 0) return (qx > qy ? qx : qy) - r;
  float mx = qx > 0 ? qx : 0, my = qy > 0 ? qy : 0;
  return sqrtf(mx * mx + my * my) + fminf(fmaxf(qx, qy), 0.f) - r;
}

// Скруглённый глаз построчно: 2D-ореол + градиент + чёткий блик + кант + AA краёв.
static void drawEye(float ex, float cy, float hw, float hh, C3 ct, C3 cb, float gx) {
  if (hh < 3) { fillRect((int)(ex - hw), (int)(cy - 1), (int)(hw * 2), 3, pack565(cb.r * 0.6f, cb.g * 0.6f, cb.b * 0.6f)); return; }
  // мягкое свечение: 2 полупрозрачных кольца с РОВНОЙ альфой (построчно, дёшево).
  // Ровная альфа на кольцо -> на верхушках нет центральной вертикальной линии.
  // Градиент даёт наложение колец: у самого глаза ярче (оба кольца), дальше тусклее.
  {
    C3 gcol = { mixf(ct.r, 255, 0.10f), mixf(ct.g, 255, 0.10f), mixf(ct.b, 255, 0.10f) };
    const float ENL[2] = {7.0f, 3.0f}; const float ALP[2] = {0.17f, 0.21f};
    float rBody = fminf(hw, hh) * 0.62f;
    for (int L = 0; L < 2; L++) {
      float ehw = hw + ENL[L], ehh = hh + ENL[L], er = rBody + ENL[L], a = ALP[L];
      int y0 = (int)ceilf(cy - ehh), y1 = (int)floorf(cy + ehh);
      if (y0 < 0) y0 = 0; if (y1 > 239) y1 = 239;
      for (int y = y0; y <= y1; y++) {
        float dy = y - cy, ady = dy < 0 ? -dy : dy, bhw = 0;
        if (ady <= hh - rBody) bhw = hw;
        else if (ady < hh) { float o = ady - (hh - rBody); float ins = rBody * rBody - o * o; bhw = ins > 0 ? hw - (rBody - sqrtf(ins)) : 0; }
        float ehwr;
        if (ady <= ehh - er) ehwr = ehw;
        else { float o = ady - (ehh - er); float ins = er * er - o * o; if (ins <= 0) continue; ehwr = ehw - (er - sqrtf(ins)); }
        for (int off = (int)bhw; off <= (int)ehwr; off++) { blendFB((int)(ex + off), y, gcol, a); blendFB((int)(ex - off), y, gcol, a); }
      }
    }
  }
  // тело глаза
  float r = fminf(hw, hh) * 0.62f;
  float inv2hh = 1.0f / (2 * hh), invGlow = 1.0f / (hh * 1.1f);
  int ytop = (int)ceilf(cy - hh), ybot = (int)floorf(cy + hh);
  if (ytop < 0) ytop = 0; if (ybot > 239) ybot = 239;
  for (int y = ytop; y <= ybot; y++) {
    float dy = y - cy, ady = dy < 0 ? -dy : dy, hwr;
    if (ady <= hh - r) hwr = hw; else { float o = ady - (hh - r); float ins = r * r - o * o; if (ins <= 0) continue; hwr = hw - (r - sqrtf(ins)); }
    float tg = clampf((dy + hh) * inv2hh, 0.f, 1.f);
    C3 rc = mix3(ct, cb, tg);
    float glow = clampf(0.16f - dy * invGlow, 0.f, 1.f); tw(rc, glow * 0.14f);
    float edgeL = ex - hwr, edgeR = ex + hwr;
    int x0 = (int)ceilf(edgeL), x1 = (int)floorf(edgeR);
    uint16_t c = pack565(rc.r, rc.g, rc.b); uint8_t hi = c >> 8, lo = (uint8_t)c;
    int xa = x0 < 0 ? 0 : x0, xb = x1 > 239 ? 239 : x1;
    uint8_t *prow = FB + y * W * 2;
    for (int x = xa; x <= xb; x++) { prow[x * 2] = hi; prow[x * 2 + 1] = lo; }
    // стеклянный кант: чуть светлее у самых краёв строки
    C3 rim = rc; tw(rim, 0.22f); uint16_t rc2 = pack565(rim.r, rim.g, rim.b);
    if (xa >= 0 && xa < W) { prow[xa * 2] = rc2 >> 8; prow[xa * 2 + 1] = (uint8_t)rc2; }
    if (xb >= 0 && xb < W) { prow[xb * 2] = rc2 >> 8; prow[xb * 2 + 1] = (uint8_t)rc2; }
    // сглаживание боковых краёв
    blendFB(x0 - 1, y, rc, (float)x0 - edgeL);
    blendFB(x1 + 1, y, rc, edgeR - (float)x1);
  }
  // 3) блик: мягкий ореол + чёткое белое ядро (глянцевая искра) + вторая искорка
  if (hh > 16) {
    float chx = ex - hw * 0.28f - gx * 0.10f, chy = cy - hh * 0.30f;
    C3 wcol = {255, 255, 255};
    softDot((int)chx, (int)chy, hw * 0.20f, wcol, 0.42f);
    fillCircle((int)chx, (int)chy, (int)(hw * 0.075f), pack565(255, 255, 255));
    softDot((int)(ex + hw * 0.20f), (int)(cy + hh * 0.18f), hw * 0.06f, wcol, 0.55f);
  }
}

// Улыбка ‿ по столбцам (для speaking): дуга вверх с вертикальным градиентом.
static void drawSmile(float ex, float cy, float hw, float thick, C3 ct, C3 cb, float sag) {
  int xa = (int)(ex - hw), xb = (int)(ex + hw);
  for (int x = xa; x <= xb; x++) {
    float dx = (x - ex) / hw; if (dx < -1 || dx > 1) continue;
    float yc = cy - sag * 0.5f + sag * dx * dx;      // ‿ : середина ниже, концы выше
    float th = thick * (0.45f + 0.55f * sqrtf(1 - dx * dx));  // тоньше к концам
    int y0 = (int)(yc - th), y1 = (int)(yc + th); float inv = 1.0f / (2 * th);
    for (int y = y0; y <= y1; y++) {
      float tg = clampf((y - (yc - th)) * inv, 0.f, 1.f);
      C3 c = mix3(ct, cb, tg);
      putFB(x, y, pack565(c.r, c.g, c.b));
    }
  }
}

// Монета: золотой эллипс (вращается через вертикальный масштаб vs 0..1).
// face: -1 крутится, 0 = орёл (звезда-солнце), 1 = решка (кольцо).
static void drawCoin(int cx, int cy, int R, float vs, int face) {
  int Rh = (int)(R * vs); if (Rh < 1) Rh = 1;
  for (int y = -Rh; y <= Rh; y++) {
    float ny = (float)y / Rh;
    int hw = (int)(R * sqrtf(1 - ny * ny));
    float t = (ny + 1) * 0.5f;
    C3 c = mix3((C3){255,236,150}, (C3){188,128,28}, t);
    uint16_t cc = pack565(c.r, c.g, c.b);
    uint16_t rim = pack565(120, 78, 12);
    int yy = cy + y; if ((unsigned)yy >= (unsigned)H) continue;
    uint8_t *row = FB + yy * W * 2;
    for (int x = -hw; x <= hw; x++) {
      int xx = cx + x; if ((unsigned)xx >= (unsigned)W) continue;
      uint16_t v = (x <= -hw + 2 || x >= hw - 2 || y <= -Rh + 1 || y >= Rh - 1) ? rim : cc;
      row[xx * 2] = v >> 8; row[xx * 2 + 1] = (uint8_t)v;
    }
  }
  softDot(cx - (int)(R * 0.30f), cy - (int)(Rh * 0.42f), R * 0.44f, (C3){255, 255, 255}, 0.34f * vs);  // глянцевый блик
  if (face >= 0 && vs > 0.9f) {
    uint16_t fc = pack565(110, 72, 10);
    if (face == 0) {                 // орёл — лучистая звезда
      for (int i = 0; i < 8; i++) {
        float a = i * 0.7854f;
        for (float k = 0.35f; k <= 1.0f; k += 0.06f)
          fillCircle(cx + (int)(30 * k * cosf(a)), cy + (int)(30 * k * sinf(a)), 2, fc);
      }
      fillCircle(cx, cy, 9, fc);
    } else {                          // решка — кольцо с точкой
      for (float a = 0; a < 6.3f; a += 0.04f)
        fillCircle(cx + (int)(27 * cosf(a)), cy + (int)(27 * sinf(a)), 2, fc);
      fillCircle(cx, cy, 7, fc);
    }
  }
}

// Поле крестиков-ноликов из строки 9 символов (. X O).
static void drawBoard(const char *b) {
  const int x0 = 22, y0 = 22, cell = 65;   // сетка 3*65=195 -> 22..217
  uint16_t grid = pack565(90, 120, 170);
  for (int i = 1; i < 3; i++) {
    fillRect(x0 + i * cell - 1, y0, 3, cell * 3, grid);
    fillRect(x0, y0 + i * cell - 1, cell * 3, 3, grid);
  }
  uint16_t xc = pack565(80, 205, 255);     // крестики — голубой
  uint16_t oc = pack565(255, 140, 90);     // нолики — оранжевый
  if (!b) return;
  for (int k = 0; k < 9 && b[k]; k++) {
    char ch = b[k]; if (ch != 'X' && ch != 'O') continue;
    int r = k / 3, c = k % 3;
    int cx = x0 + c * cell + cell / 2, cy = y0 + r * cell + cell / 2;
    int rr = cell / 2 - 13;
    if (ch == 'X') {
      for (int t = -rr; t <= rr; t++) { fillCircle(cx + t, cy + t, 2, xc); fillCircle(cx + t, cy - t, 2, xc); }
    } else {
      for (float a = 0; a < 6.3f; a += 0.04f) fillCircle(cx + (int)(rr * cosf(a)), cy + (int)(rr * sinf(a)), 2, oc);
    }
  }
}

// ── Кисть-линия по Брезенхэму (для иконок/стрелок/жестов) ──
static void brushLine(int x0, int y0, int x1, int y1, int rad, uint16_t c) {
  int dx = abs(x1 - x0), dy = abs(y1 - y0), sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1, err = dx - dy;
  for (;;) { fillCircle(x0, y0, rad, c); if (x0 == x1 && y0 == y1) break; int e2 = 2 * err; if (e2 > -dy) { err -= dy; x0 += sx; } if (e2 < dx) { err += dx; y0 += sy; } }
}
// ── Программное затемнение полосы FB (b:0..255) — ночь/сон ──
static void dimFB(int y0, int h, int b) {
  if (b >= 245) return; int y1 = y0 + h; if (y0 < 0) y0 = 0; if (y1 > H) y1 = H;
  for (int y = y0; y < y1; y++) { uint8_t *row = FB + y * W * 2;
    for (int x = 0; x < W; x++) { int i = x * 2; uint16_t c = (row[i] << 8) | row[i + 1];
      int R = (((c >> 11) & 0x1F) * b) >> 8, G = (((c >> 5) & 0x3F) * b) >> 8, Bl = ((c & 0x1F) * b) >> 8;
      uint16_t v = (R << 11) | (G << 5) | Bl; row[i] = v >> 8; row[i + 1] = (uint8_t)v; } }
}

// ── ПРЕМИУМ-ПРИМИТИВЫ (объём/тени/сглаживание — как у глаз) ──
// 3D-шар: диффуз (свет сверху-слева) + мягкий блик + сглаженный край. Даёт объём
// монетке, кубику-пипсам, magic-8, «камню» и т.п.
static void shadedBall(int cx, int cy, float R, C3 base, float spec) {
  int Ri = (int)ceilf(R); float invR = 1.0f / R;
  for (int y = -Ri; y <= Ri; y++) { int yy = cy + y; if ((unsigned)yy >= (unsigned)H) continue;
    float ny = y * invR;
    for (int x = -Ri; x <= Ri; x++) { float nx = x * invR; float d2 = nx * nx + ny * ny; if (d2 > 1.0f) continue;
      int xx = cx + x; if ((unsigned)xx >= (unsigned)W) continue;
      float nz = sqrtf(1.0f - d2);
      float diff = nx * (-0.42f) + ny * (-0.5f) + nz * 0.76f; if (diff < 0) diff = 0;
      float shade = 0.24f + 0.86f * diff;
      float hx = nx + 0.4f, hy = ny + 0.46f, hd = hx * hx + hy * hy;      // блик верх-лево
      float sp = hd < 0.13f ? (1.0f - hd / 0.13f) : 0.0f; sp = sp * sp * spec;
      C3 c = { base.r * shade + 255 * sp, base.g * shade + 255 * sp, base.b * shade + 255 * sp };
      float edge = (1.0f - d2) * R; float a = edge < 1.5f ? edge / 1.5f : 1.0f;   // сглаживание края
      if (a >= 0.996f) putFB(xx, yy, pack565(c.r, c.g, c.b)); else blendFB(xx, yy, c, a);
    }
  }
}
// Мягкая эллиптическая тень (под объектами — «отрыв» от фона)
static void softShadowEllipse(int cx, int cy, float rx, float ry, float amax) {
  int rxi = (int)ceilf(rx), ryi = (int)ceilf(ry);
  for (int y = -ryi; y <= ryi; y++) { int yy = cy + y; if ((unsigned)yy >= (unsigned)H) continue;
    float ry2 = (float)y * y / (ry * ry);
    for (int x = -rxi; x <= rxi; x++) { float d = (float)x * x / (rx * rx) + ry2; if (d > 1) continue;
      int xx = cx + x; if ((unsigned)xx >= (unsigned)W) continue;
      float k = 1 - d; blendFB(xx, yy, (C3){0, 0, 0}, amax * k * k); } }
}
// Сглаженный диск (мягкий край в 1.3px)
static void aaDisc(int cx, int cy, float R, C3 col) {
  int Ri = (int)ceilf(R + 1);
  for (int y = -Ri; y <= Ri; y++) { int yy = cy + y; if ((unsigned)yy >= (unsigned)H) continue;
    for (int x = -Ri; x <= Ri; x++) { float d = sqrtf((float)x * x + (float)y * y); if (d > R + 1) continue;
      int xx = cx + x; if ((unsigned)xx >= (unsigned)W) continue;
      float a = R - d; a = a > 1 ? 1 : (a < 0 ? 0 : a);
      if (a >= 0.996f) putFB(xx, yy, pack565(col.r, col.g, col.b)); else if (a > 0) blendFB(xx, yy, col, a); } }
}
// Скруглённый прямоугольник с вертикальным градиентом и сглаженным краем (кубик/бумага)
static void roundRectGrad(int cx, int cy, int hw, int hh, int rad, C3 top, C3 bot) {
  for (int y = -hh; y <= hh; y++) { int yy = cy + y; if ((unsigned)yy >= (unsigned)H) continue;
    float tg = (float)(y + hh) / (2 * hh); C3 rc = mix3(top, bot, tg);
    float ady = (float)(y < 0 ? -y : y) - (hh - rad);
    for (int x = -hw; x <= hw; x++) { int xx = cx + x; if ((unsigned)xx >= (unsigned)W) continue;
      float adx = (float)(x < 0 ? -x : x) - (hw - rad);
      float outside;
      if (adx > 0 && ady > 0) outside = sqrtf(adx * adx + ady * ady) - rad;
      else outside = (adx > ady ? adx : ady) - rad;
      if (outside > 1) continue;
      float a = outside < 0 ? 1 : (1 - outside);
      if (a >= 0.996f) putFB(xx, yy, pack565(rc.r, rc.g, rc.b)); else blendFB(xx, yy, rc, a); } }
}

// ── Иконка погоды (лаконичная). cond: 0 ясно,1 перем.обл,2 облачно,3 дождь,
//    4 снег,5 гроза,6 туман,7 ясная ночь ──
static void drawWeatherIcon(int cx, int cy, int cond) {
  C3 sunC = {255, 205, 80}, cloudC = {214, 222, 236}, rainC = {120, 180, 255},
     snowC = {235, 245, 255}, boltC = {255, 220, 110}, moonC = {224, 232, 255};
  // объёмное облако: тёмное «дно» + светлый «верх», сглаженные комки
  auto drawCloud = [&](int ox, int oy, float s) {
    C3 bot = mix3(cloudC, (C3){120, 132, 158}, 0.55f);
    softShadowEllipse(ox, oy + (int)(9 * s), 18 * s, 5 * s, 0.25f);
    aaDisc(ox - (int)(10 * s), oy + (int)(2 * s), 8 * s, bot);
    aaDisc(ox + (int)(11 * s), oy + (int)(2 * s), 9 * s, bot);
    roundRectGrad(ox, oy + (int)(4 * s), (int)(17 * s), (int)(5 * s), (int)(4 * s), cloudC, bot);
    aaDisc(ox - (int)(9 * s), oy - (int)(1 * s), 8 * s, cloudC);
    aaDisc(ox + (int)(10 * s), oy - (int)(1 * s), 9 * s, cloudC);
    aaDisc(ox, oy - (int)(7 * s), 10 * s, cloudC);
  };
  if (cond == 0 || cond == 1) {
    softDot(cx, cy, 22, mix3(sunC, (C3){255,255,255}, 0.3f), 0.5f);   // мягкое свечение
    for (int i = 0; i < 8; i++) { float a = i * 0.7854f; brushLine(cx + (int)(13 * cosf(a)), cy + (int)(13 * sinf(a)), cx + (int)(19 * cosf(a)), cy + (int)(19 * sinf(a)), 1, pack565(sunC.r, sunC.g, sunC.b)); }
    shadedBall(cx, cy, 10, sunC, 0.6f);
    if (cond == 1) drawCloud(cx + 9, cy + 9, 0.78f);
  } else if (cond == 7) {
    softDot(cx, cy, 18, mix3(moonC, (C3){255,255,255}, 0.2f), 0.4f);
    shadedBall(cx, cy, 12, moonC, 0.35f);
    aaDisc(cx + 7, cy - 4, 11, (C3){8, 12, 32});                       // «полумесяц»
    softDot(cx + 16, cy - 12, 2.5f, snowC, 0.9f); softDot(cx + 13, cy + 9, 1.8f, snowC, 0.8f);
  } else if (cond == 6) {
    for (int i = 0; i < 4; i++) { C3 f = {180, 195, 215}; for (int x = -16; x <= 16; x++) blendFB(cx + x, cy - 9 + i * 7, f, 0.85f * (1 - fabsf(x) / 20.0f)); }
  } else {
    drawCloud(cx, cy - 4, 1.0f);
    if (cond == 3) for (int i = -1; i <= 1; i++) { brushLine(cx + i * 9, cy + 11, cx + i * 9 - 3, cy + 21, 1, pack565(rainC.r, rainC.g, rainC.b)); softDot(cx + i * 9 - 3, cy + 21, 2, rainC, 0.7f); }
    else if (cond == 4) for (int i = -1; i <= 1; i++) softDot(cx + i * 9, cy + 17, 3, snowC, 0.9f);
    else if (cond == 5) { C3 gl = {255, 230, 120}; softDot(cx - 3, cy + 16, 7, gl, 0.5f); brushLine(cx - 1, cy + 8, cx - 7, cy + 17, 2, pack565(boltC.r, boltC.g, boltC.b)); brushLine(cx - 7, cy + 17, cx + 3, cy + 15, 2, pack565(boltC.r, boltC.g, boltC.b)); brushLine(cx + 3, cy + 15, cx - 3, cy + 25, 2, pack565(boltC.r, boltC.g, boltC.b)); }
  }
}

// ── Спящий режим: мягкие закрытые веки + реснички + светящиеся Zzz ──
static void drawZ(int cx, int cy, float s, C3 c, float a) {
  // мягкое свечение под буквой + чёткий штрих
  softDot(cx, cy, s * 1.15f, c, a * 0.28f);
  int xa = cx - (int)s, xb = cx + (int)s, yt = cy - (int)s, yb = cy + (int)s;
  auto ln = [&](int x0, int y0, int x1, int y1) {
    int dx = abs(x1 - x0), dy = abs(y1 - y0), sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1, err = dx - dy;
    for (;;) { blendFB(x0, y0, c, a); blendFB(x0 + 1, y0, c, a * 0.6f); if (x0 == x1 && y0 == y1) break; int e2 = 2 * err; if (e2 > -dy) { err -= dy; x0 += sx; } if (e2 < dx) { err += dx; y0 += sy; } }
  };
  ln(xa, yt, xb, yt); ln(xb, yt, xa, yb); ln(xa, yb, xb, yb);
}
static void drawSleep(uint32_t now) {
  float breathe = 0.5f + 0.5f * sinf(now / 2400.0f * TAUF);   // медленное дыхание
  C3 lid = {95, 175, 225};
  for (int e = 0; e < 2; e++) {
    int ex = e ? 174 : 66;
    float depth = 9.0f + 2.0f * breathe;
    for (int x = -46; x <= 46; x++) {
      float dx = x / 46.0f;
      float yc = 104 + depth * (1 - dx * dx);      // пологая закрытая веко-дуга ⌣
      float th = 2.4f * (1 - 0.35f * dx * dx);      // толще к центру
      for (int t = -3; t <= 3; t++) {
        float k = 1 - fabsf(t) / (th + 1.2f); if (k < 0) continue;
        blendFB(ex + x, (int)yc + t, lid, 0.9f * k);
      }
    }
    // реснички у внешнего края
    int lx = ex + (e ? 40 : -40);
    for (int r = 0; r < 3; r++) blendFB(lx + (e ? r : -r), 112 + r, lid, 0.7f - r * 0.15f);
  }
  C3 zc = {160, 205, 250};
  for (int i = 0; i < 3; i++) {
    float ph = fmodf(now / 2800.0f + i * 0.33f, 1.0f);
    int zx = 150 + i * 16 + (int)(6 * sinf(now / 850.0f + i));
    int zy = (int)(80 - ph * 46);
    float a = (ph < 0.8f ? 1.0f : (1 - (ph - 0.8f) / 0.2f)) * 0.95f;
    drawZ(zx, zy, 9 - i * 2.0f, zc, a);
  }
}

// 4-лучевая искорка (мягкое ядро + тонкий крест)
static void sparkle(int cx, int cy, float s, C3 c, float a) {
  softDot(cx, cy, s, c, a);
  for (int i = 1; i <= (int)(s * 1.6f); i++) {
    float k = a * (1 - i / (s * 1.7f)); if (k <= 0) continue;
    blendFB(cx + i, cy, c, k); blendFB(cx - i, cy, c, k);
    blendFB(cx, cy + i, c, k); blendFB(cx, cy - i, c, k);
  }
}
// ── Радость (^_^): светящиеся дуги-глаза пиком вверх + щёчки + искорки ──
static void drawHappy(uint32_t now, C3 ct) {
  float bob = 4 * sinf(now / 250.0f);
  C3 glow = { mixf(ct.r, 255, 0.12f), mixf(ct.g, 255, 0.12f), mixf(ct.b, 255, 0.12f) };
  for (int e = 0; e < 2; e++) {
    int ex = e ? 174 : 66, cy = 104 + (int)bob;
    softDot(ex, cy - 6, 30, glow, 0.22f);                    // тёплое свечение под глазом
    for (int x = -42; x <= 42; x++) {
      float dx = x / 42.0f; if (fabsf(dx) > 1) continue;
      float yc = cy - 20 * (1 - dx * dx);                    // ⌢ пик вверх — довольный прищур
      float th = 4.5f * (1 - 0.45f * dx * dx);               // толще в центре, тоньше к краям
      for (int t = -1; t <= (int)(th + 2); t++) {
        float k = 1 - fabsf(t - th * 0.5f) / (th * 0.6f + 1.2f); if (k < 0) continue;
        C3 c = t < 2 ? mix3(glow, ct, 0.3f) : ct;            // светлее сверху
        blendFB(ex + x, (int)yc + t, c, 0.96f * k);
      }
    }
    // румяные щёчки с градиентом
    softDot(ex - (e ? -6 : 6), cy + 26, 12, (C3){255, 150, 170}, 0.34f);
  }
  for (int i = 0; i < 4; i++) {
    float ph = fmodf(now / 850.0f + i * 0.25f, 1.0f);
    int sx = 58 + i * 42 + (int)(9 * sinf(now / 300.0f + i * 2)), sy = (int)(72 - ph * 56);
    sparkle(sx, sy, 3.6f - ph * 1.6f, (C3){255, 238, 190}, (1 - ph) * 0.95f);
  }
}

// ── Тревога (!): объёмные распахнутые глаза + брови-домиком + светящийся «!» ──
static void drawAlarm(uint32_t now, C3 ct) {
  int shake = (int)(5 * sinf(now / 52.0f));
  float pulse = 0.5f + 0.5f * sinf(now / 170.0f);
  C3 warn = {255, 90, 70};
  for (int e = 0; e < 2; e++) {
    int ex = (e ? 174 : 66) + shake, cy = 110;
    softDot(ex, cy, 40, mix3(warn, (C3){255,180,120}, 0.4f), 0.14f * pulse);   // тревожное свечение
    shadedBall(ex, cy, 31, (C3){250, 250, 255}, 0.15f);                        // белок с объёмом
    // цветной ободок-радужка
    for (float a = 0; a < TAUF; a += 0.02f) blendFB(ex + (int)(30 * cosf(a)), cy + (int)(30 * sinf(a)), ct, 0.5f);
    int px = (int)(8 * sinf(now / 115.0f + e)), py = (int)(6 * cosf(now / 95.0f + e));  // бегающий зрачок
    shadedBall(ex + px, cy + py, 12, (C3){28, 30, 42}, 0.0f);
    aaDisc(ex + px - 4, cy + py - 4, 3, (C3){235, 240, 255});                  // блик-искорка
    // бровь-домиком (тревога): линия, поднятая к внешнему краю
    uint16_t brow = pack565(ct.r * 0.8f, ct.g * 0.8f, ct.b * 0.8f);
    if (e) brushLine(ex + 20, cy - 40, ex - 20, cy - 47, 2, brow);
    else brushLine(ex - 20, cy - 40, ex + 20, cy - 47, 2, brow);
  }
  // «!» по центру сверху — свечение + тень + тело
  int mx = 120 + shake;
  C3 exc = {255, cl8(120 + 110 * pulse), 70};
  softDot(mx, 46, 20, exc, 0.35f * pulse);
  softShadowEllipse(mx + 2, 74, 7, 4, 0.3f);
  roundRectGrad(mx, 44, 5, 15, 3, mix3(exc, (C3){255,255,255}, 0.3f), exc);
  aaDisc(mx, 68, 5, exc);
}

// ── Кубик: объём (градиент+тень+блик), утопленные пипсы, пружинка ──
static void drawDie(int cx, int cy, int R, int face, float sq) {
  int hw = R, hh = (int)(R * sq); if (hh < 6) hh = 6;
  int rad = R / 4;
  softShadowEllipse(cx, cy + hh + 6, hw * 0.98f, hh * 0.30f, 0.40f);
  roundRectGrad(cx, cy, hw, hh, rad, (C3){252, 252, 255}, (C3){204, 210, 228});
  for (int x = -hw + rad; x <= hw - rad; x++) blendFB(cx + x, cy - hh + 3, (C3){255, 255, 255}, 0.55f);  // верхний блик
  if (face < 1) return;
  int d = (int)(R * 0.44f), pr = (int)(R * 0.15f); if (pr < 3) pr = 3;
  C3 pip = {60, 66, 92};
  auto P = [&](int gx, int gy) {
    int py = cy + (int)(gy * sq);
    softDot(cx + gx, py + 1, pr + 2.0f, (C3){18, 20, 32}, 0.45f);   // тень-«ямка»
    shadedBall(cx + gx, py, pr, pip, 0.22f);
  };
  if (face & 1) P(0, 0);
  if (face >= 2) { P(-d, -d); P(d, d); }
  if (face >= 4) { P(-d, d); P(d, -d); }
  if (face == 6) { P(-d, 0); P(d, 0); }
}
static void drawDice(uint32_t now, uint32_t until, int result) {
  float rem = (until > now) ? (float)(until - now) : 0.f;
  float p = 1.0f - rem / 2200.0f; if (p < 0) p = 0; if (p > 1) p = 1;
  bool landed = p > 0.82f;
  int face = landed ? (result < 1 ? 1 : result) : (int)(hash32((uint32_t)(now / 100), 5) % 6) + 1;
  float sq = 1.0f; int bob = 0;
  if (!landed) { bob = (int)(34 * (1 - p) * fabsf(sinf(p * 16.0f))); sq = 1.0f + 0.10f * sinf(now / 70.0f); }
  else { float lp = (p - 0.82f) / 0.18f; sq = 1.0f - 0.16f * sinf(lp * 3.14159f) * (1 - lp * 0.6f); }  // приземлился — пружинит
  drawDie(120, 116 - bob, 46, face, sq);
}

// ── Колесо фортуны: залитый диск с секторами, крутится и садится на result ──
static void drawWheel(uint32_t now, uint32_t until, int count, int result) {
  if (count < 2) count = 2; if (count > 8) count = 8;
  if (result < 0) result = 0; if (result >= count) result = count - 1;
  int cx = 120, cy = 112, R = 86, hub = 17;
  const C3 HUE[8] = {{240,96,104},{250,168,72},{246,214,86},{126,206,118},{96,200,214},{116,146,240},{184,124,236},{246,132,190}};
  float rem = (until > now) ? (float)(until - now) : 0.f;
  float p = 1.0f - rem / 3000.0f; if (p < 0) p = 0; if (p > 1) p = 1;
  float ease = 1 - (1 - p) * (1 - p);
  float segA = TAUF / count;
  float rotFinal = -1.5708f - (result + 0.5f) * segA;       // winner под верхним указателем
  float rot = rotFinal - (1 - ease) * (TAUF * 6);
  bool landed = p > 0.985f; float pulse = 0.5f + 0.5f * sinf(now / 130.0f);
  softShadowEllipse(cx, cy + R - 4, R * 0.92f, R * 0.30f, 0.40f);
  int steps = 1000; float dA = TAUF / steps, invSeg = 1.0f / segA;
  for (int i = 0; i < steps; i++) {
    float a = i * dA, ca = cosf(a), sa = sinf(a);
    float local = a - rot; local -= TAUF * floorf(local / TAUF);
    int seg = (int)(local * invSeg); if (seg >= count) seg = count - 1;
    float edge = local - seg * segA;                          // к границе сектора
    bool divider = edge < 0.028f || (segA - edge) < 0.028f;
    C3 col = HUE[seg % 8];
    if (landed && seg == result) { tw(col, 0.28f * pulse); }
    for (int rr = hub - 1; rr <= R; rr++) {
      float k = 0.82f + 0.20f * (rr / (float)R);              // к ободу ярче
      C3 c = divider ? mix3(col, (C3){30, 30, 44}, 0.55f) : col;
      int px = cx + (int)(rr * ca), py = cy + (int)(rr * sa);
      if (rr >= R - 2) c = mix3(c, (C3){255, 255, 255}, 0.35f);  // светлый ободок
      putFB(px, py, pack565(c.r * k, c.g * k, c.b * k));
    }
  }
  shadedBall(cx, cy, hub, (C3){70, 76, 104}, 0.5f);            // ступица
  aaDisc(cx, cy, hub * 0.4f, (C3){210, 216, 236});
  // указатель сверху с тенью
  int py0 = cy - R - 2;
  softShadowEllipse(cx, py0 + 12, 9, 4, 0.3f);
  C3 pt = {250, 250, 255};
  for (int y = 0; y <= 16; y++) { int hwp = 9 - y * 9 / 16; for (int x = -hwp; x <= hwp; x++) blendFB(cx + x, py0 + y, y < 3 ? (C3){255,255,255} : pt, 1.0f); }
}

// ── Камень-ножницы-бумага: объёмный жест + светящийся обод по исходу ──
static void drawRPS(uint32_t now, uint32_t until, int mine, int result) {
  int cx = 120;
  float rem = (until > now) ? (float)(until - now) : 0.f;
  float p = 1.0f - rem / 3200.0f; if (p < 0) p = 0; if (p > 1) p = 1;
  float bob = (p < 0.55f) ? 14 * fabsf(sinf(p * TAUF * 3)) : 0;   // «камень-ножницы-бумага — раз!»
  int y = 112 - (int)bob;
  bool show = p >= 0.5f;
  float pulse = 0.5f + 0.5f * sinf(now / 150.0f);
  C3 rc = result == 1 ? (C3){110, 225, 140} : result == 2 ? (C3){255, 110, 105} : (C3){172, 186, 206};
  softDot(cx, y, 76, rc, 0.13f + 0.06f * pulse);                  // свечение исхода
  for (float a = 0; a < TAUF; a += 0.012f) { float ca = cosf(a), sa = sinf(a); for (int t = 0; t < 4; t++) blendFB(cx + (int)((66 + t) * ca), y + (int)((66 + t) * sa), rc, 0.7f - t * 0.15f); }
  softShadowEllipse(cx, y + 42, 40, 12, 0.34f);
  C3 skin = {234, 216, 194};
  if (!show) { shadedBall(cx, y, 34, (C3){224, 206, 182}, 0.18f); return; }
  if (mine == 0) {                    // камень — кулак с костяшками
    shadedBall(cx, y + 4, 34, skin, 0.18f);
    for (int k = -1; k <= 2; k++) shadedBall(cx + k * 15 - 8, y - 14, 8, mix3(skin, (C3){255, 242, 224}, 0.25f), 0.3f);
  } else if (mine == 1) {             // бумага — лист
    roundRectGrad(cx, y, 32, 40, 7, (C3){250, 248, 244}, (C3){214, 210, 204});
    for (int i = 0; i < 3; i++) for (int x = -20; x <= 20; x++) blendFB(cx + x, y - 16 + i * 15, (C3){196, 192, 186}, 0.8f);
  } else {                            // ножницы — два лезвия + кольца
    for (int s = -1; s <= 1; s += 2) {
      float bx0 = cx + s * 8, by0 = y - 30, bx1 = cx - s * 22, by1 = y + 18;
      for (int t = 0; t < 44; t++) { float tt = t / 43.0f; float wth = 5.0f * (1 - tt) + 1.6f; aaDisc((int)mixf(bx0, bx1, tt), (int)mixf(by0, by1, tt), wth, mix3((C3){214, 220, 234}, (C3){150, 158, 182}, tt)); }
    }
    for (int s = -1; s <= 1; s += 2) { int hx = cx + s * 20, hy = y + 30; shadedBall(hx, hy, 10, (C3){124, 132, 156}, 0.3f); aaDisc(hx, hy, 5, (C3){24, 28, 40}); }
  }
}

// ── Magic-8-ball: глянцевый объёмный шар, окно с символом ответа (текст — голосом) ──
//    ans 1..5 → да (✓), 6..7 → неясно (~), 8..10 → нет (✗)
static void drawMagic8(uint32_t now, uint32_t until, int ans) {
  int cx = 120, cy = 114;
  float rem = (until > now) ? (float)(until - now) : 0.f;
  float p = 1.0f - rem / 3200.0f; if (p < 0) p = 0; if (p > 1) p = 1;
  if (p < 0.6f) cx += (int)(8 * sinf(now / 42.0f));           // трясётся
  cy += (int)(3 * sinf(now / 700.0f));                        // лёгкое парение
  softShadowEllipse(cx, cy + 94, 78, 20, 0.42f);
  shadedBall(cx, cy, 90, (C3){15, 17, 27}, 0.95f);            // глянцевый чёрный
  shadedBall(cx - 30, cy - 30, 19, (C3){248, 248, 252}, 0.4f);  // белый кружок «8»
  seg7(cx - 36, cy - 41, 13, 22, 3, 8, pack565(28, 28, 42));
  if (p < 0.6f) return;
  float rp = (p - 0.6f) / 0.4f; if (rp > 1) rp = 1;            // проявление окна
  int th = (int)(34 * rp);
  C3 blu = {60, 120, 255};
  for (int r = 0; r < th; r++) { float k = 0.35f + 0.55f * (r / (float)(th + 1)); int hwt = (int)(r * 0.78f); for (int x = -hwt; x <= hwt; x++) blendFB(cx + x, cy + 14 + r, (C3){blu.r * k, blu.g * k, blu.b * k}, 0.95f); }
  softDot(cx, cy + 30, 22 * rp, blu, 0.4f);
  if (rp < 0.55f) return;
  int cat = ans <= 5 ? 0 : (ans <= 7 ? 1 : 2);
  C3 sc = cat == 0 ? (C3){150, 255, 190} : cat == 1 ? (C3){255, 222, 130} : (C3){255, 140, 140};
  uint16_t sol = pack565(sc.r, sc.g, sc.b);
  int by = cy + 32;
  if (cat == 0) { brushLine(cx - 9, by, cx - 2, by + 8, 2, sol); brushLine(cx - 2, by + 8, cx + 11, by - 9, 2, sol); }      // ✓
  else if (cat == 2) { brushLine(cx - 8, by - 8, cx + 8, by + 8, 2, sol); brushLine(cx + 8, by - 8, cx - 8, by + 8, 2, sol); } // ✗
  else { for (int x = -9; x <= 9; x++) blendFB(cx + x, by + (int)(3 * sinf(x * 0.5f)), sc, 0.95f); }                          // ~
}

static void render(Display &it, int state, uint32_t now, int hour, int minute, int reminders,
                   uint32_t coinUntil, int coinRes, const char *board,
                   uint32_t diceUntil, int diceRes,
                   uint32_t wheelUntil, int wheelCount, int wheelRes,
                   uint32_t rpsUntil, int rpsMine, int rpsRes,
                   uint32_t magic8Until, int magic8Ans,
                   int wcond, int wtemp, int bright) {
  if (!s_init) {
    FB = (uint8_t *)heap_caps_malloc(W * H * 2, MALLOC_CAP_DMA);
    if (!FB) FB = (uint8_t *)heap_caps_malloc(W * H * 2, MALLOC_CAP_SPIRAM);
    BGBUF = (uint8_t *)heap_caps_malloc(W * H * 2, MALLOC_CAP_SPIRAM);
    if (!BGBUF) BGBUF = (uint8_t *)malloc(W * H * 2);
    if (!FB || !BGBUF) return;
    s_init = true;
  }
  uint32_t t_start = micros();
  const float TAU = 6.28318f; const int rb = W * 2, by0 = 12, bh = 162;

  static int prev = -1, from = 0; static uint32_t tsw = 0;
  if (state != prev) { from = (prev < 0 ? state : prev); prev = state; tsw = now; }
  float bt = sstep(0.f, 1.f, (now - tsw) / 440.0f);
  EyeP A = paramFor(from), B = paramFor(state);
  float hw = mixf(A.hw, B.hw, bt), hh0 = mixf(A.hh, B.hh, bt), bend = mixf(A.bend, B.bend, bt);
  C3 ct = mix3(A.ct, B.ct, bt), cb = mix3(A.cb, B.cb, bt);

  static int sState = -999, sW = -999999;
  int wkey = (state == 0 && wcond >= 0) ? (wcond * 1000 + (wtemp + 100)) : -1;
  bool full = (sState != state) || (state == 0 && wkey != sW);
  if (full) {
    sState = state; sW = wkey;
    EyeP p = paramFor(state);
    C3 bgc = p.cb, tp = p.ct;
    if (state == 0 && wcond >= 0) {   // лёгкий погодный тон фона (лаконично)
      if (wcond == 3 || wcond == 5) bgc = mix3(bgc, (C3){20, 40, 90}, 0.32f);       // дождь/гроза — прохладнее
      else if (wcond == 4) { bgc = mix3(bgc, (C3){70, 90, 130}, 0.30f); tp = mix3(tp, (C3){200, 220, 255}, 0.30f); }  // снег — светлее
      else if (wcond == 0) tp = mix3(tp, (C3){255, 210, 120}, 0.22f);               // ясно — теплее
      else if (wcond == 7) bgc = mix3(bgc, (C3){6, 10, 30}, 0.40f);                 // ночь — темнее
    }
    buildBG(bgc, tp);
  }
  bool fullBlit = full || state >= 6;   // игры/эмоции/сон рисуют весь экран
  if (fullBlit) memcpy(FB, BGBUF, W * H * 2);
  else memcpy(FB + by0 * rb, BGBUF + by0 * rb, bh * rb);

  float breathe = 1.0f + 0.026f * sinf(now / 1500.0f * TAU);
  float gx = 0, gy = 0, hhL = hh0 * breathe, hhR = hh0 * breathe, openL = 1, openR = 1;
  float crossL = 0, crossR = 0;   // индивидуальный сдвиг глаз (косоглазие)

  if (state == 1) {
    float k = sinf(now / 850.0f * TAU); gy = -2 - 1.5f * k; hhL *= 1 + 0.03f * k; hhR = hhL;
  } else if (state == 2) {
    float p = 0.5f - 0.5f * cosf(now / 2600.0f * TAU); gx = 11 * p; gy = -12 * p;
  } else if (state == 3) {
    float talk = fabsf(sinf(now / 300.0f)) * fabsf(sinf(now / 470.0f));
    gy = 2.5f * sinf(now / 700.0f * TAU); bend *= (1 - 0.13f * talk); hhL *= 1 + 0.09f * talk; hhR = hhL;
  } else if (state == 4) {  // игривое: напевает/танцует — покачивается в ритм
    float beat = now / 340.0f * TAU;
    gy = -1 + 6 * sinf(beat);
    gx = 5 * sinf(now / 680.0f * TAU);
    float sq = 0.78f + 0.14f * fabsf(sinf(beat));   // подпрыгивает высотой (довольный прищур)
    hhL = hh0 * breathe * sq; hhR = hhL;
  } else if (state == 5) {
    // монетка — глаза не рисуем (обрабатывается в блоке отрисовки ниже)
  } else if (state == 6) {
    // поле крестиков-ноликов — глаза не рисуем
  } else if (state == 0) {
    const uint32_t SP = 3800; uint32_t seg = now / SP; float pin = (now % SP) / (float)SP;
    float e; if (pin < 0.14f) { float u = pin / 0.14f - 1; e = 1 + (2.2f * u * u * u + 1.2f * u * u); } else e = 1;
    e = clampf(e, 0, 1);
    float gx0 = (int)(hash32(seg, 1) % 20) - 10, gx1 = (int)(hash32(seg + 1, 1) % 20) - 10;
    float gy0 = (int)(hash32(seg, 2) % 14) - 7,  gy1 = (int)(hash32(seg + 1, 2) % 14) - 7;
    gx = mixf(gx0, gx1, e) + 0.3f * sinf(now / 190.0f); gy = mixf(gy0, gy1, e);
    // моргание (иногда двойное)
    uint32_t bc = now % 3800;
    float o = (bc < 120) ? (1 - sstep(0, 1, bc / 120.0f)) : (bc < 250 ? sstep(0, 1, (bc - 120) / 130.0f) : 1);
    if (hash32(seg, 9) % 3 == 0) { uint32_t t = (bc + 340) % 3800; float o2 = (t < 120) ? (1 - sstep(0, 1, t / 120.0f)) : (t < 250 ? sstep(0, 1, (t - 120) / 130.0f) : 1); if (o2 < o) o = o2; }
    openL = openR = o;
    // характерные события скуки (без подмигивания) — раз в сегмент случайное
    uint32_t ev = hash32(seg, 7) % 10;
    if (ev == 0) { gy -= 12; }                                       // задумчиво вверх
    else if (ev == 1) { hhL *= 0.58f; hhR *= 0.58f; }                // прищур со скуки
    else if (ev == 2) { hhL *= 1.16f; hhR *= 1.16f; gy -= 2; }       // распахнул — интерес
    else if (ev == 3) { crossL = 9; crossR = -9; gy += 2; }          // косоглазие — балуется
    else if (ev == 4) { float ph = pin * TAU; gx = 12 * cosf(ph); gy = 9 * sinf(ph); }  // закатывает глаза (круг)
    else if (ev == 5) { float d = pin < 0.78f ? (0.42f + 0.1f * sinf(now / 260.0f)) : 1.0f; if (d < openL) { openL = d; openR = d; } gy += 4; }  // клюёт носом и вскидывается
    else if (ev == 6) { if (pin < 0.45f) { gx = (hash32(seg, 3) & 1 ? 16 : -16); gy = (hash32(seg, 4) & 1 ? 11 : -11); } }  // резкий дёрг взгляда в угол
    else if (ev == 7) { gy += 5 * sinf(now / 220.0f); hhL *= 0.9f; hhR *= 0.9f; }  // "напевает" — покачивается, довольный прищур
    // ev 8,9 — просто спокойно блуждает
  }
  hhL *= openL; hhR *= openR;
  float cy = 100 + gy;

  if (state == 6) {  // крестики-нолики — рисуем поле
    drawBoard(board);
  } else if (state == 5) {  // монетка — подброс и приземление
    float rem = (coinUntil > now) ? (float)(coinUntil - now) : 0.f;
    float p = 1.0f - rem / 3200.0f; if (p < 0) p = 0; if (p > 1) p = 1;
    float spin = (1 - (1 - p) * (1 - p)) * 20.0f;    // ease-out: много оборотов, замедляется
    float vs = fabsf(cosf(spin)); if (vs < 0.05f) vs = 0.05f;
    bool landed = p > 0.85f; if (landed) vs = 1.0f;
    int bob = landed ? 0 : (int)(28 * (1 - p) * fabsf(sinf(spin * 0.5f)));
    drawCoin(120, 116 - bob, 54, vs, landed ? coinRes : -1);
  } else if (state == 10) {  // кубик
    drawDice(now, diceUntil, diceRes);
  } else if (state == 11) {  // колесо фортуны
    drawWheel(now, wheelUntil, wheelCount, wheelRes);
  } else if (state == 12) {  // камень-ножницы-бумага
    drawRPS(now, rpsUntil, rpsMine, rpsRes);
  } else if (state == 13) {  // magic-8-ball
    drawMagic8(now, magic8Until, magic8Ans);
  } else if (state == 7) {   // сон
    drawSleep(now);
  } else if (state == 8) {   // радость
    drawHappy(now, ct);
  } else if (state == 9) {   // тревога
    drawAlarm(now, ct);
  } else if (bend > 13) {  // говорю — дуга-улыбка
    float th = 15.0f * (hhL / 29.0f);
    drawSmile(66 + gx, cy, hw, th, ct, cb, 24);
    drawSmile(174 + gx, cy, hw, th, ct, cb, 24);
  } else {
    drawEye(66 + gx + crossL, cy, hw, hhL, ct, cb, gx);
    drawEye(174 + gx + crossR, cy, hw, hhR, ct, cb, gx);
  }

  if (state == 4) {
    // порхающие искорки-нотки над глазами
    for (int i = 0; i < 3; i++) {
      float ph = fmodf(now / 950.0f + i * 0.34f, 1.0f);
      int sx = 68 + i * 52 + (int)(7 * sinf(now / 300.0f + i * 2));
      int sy = (int)(64 - ph * 48);
      C3 sc = {255, 240, 180};
      softDot(sx, sy, 3.5f - ph * 1.5f, sc, (1 - ph) * 0.85f);
    }
  } else if (state == 2) {
    for (int i = 0; i < 3; i++) {
      float ph = fmodf(now / 320.0f - i * 0.6f, 3.0f);
      fillCircle(96 + i * 24, 152, (ph < 1.0f) ? 6 : 4, pack565(255, 208, 96));
    }
  } else if (state == 0 && hour >= 0) {
    uint16_t cc = pack565(150, 170, 205); const int dw = 22, dh = 40, dt = 5, y0 = 178;
    seg7(56, y0, dw, dh, dt, hour / 10, cc); seg7(85, y0, dw, dh, dt, hour % 10, cc);
    fillRect(117, y0 + 11, 5, 5, cc); fillRect(117, y0 + 26, 5, 5, cc);
    seg7(133, y0, dw, dh, dt, minute / 10, cc); seg7(162, y0, dw, dh, dt, minute % 10, cc);
    if (reminders > 0) {
      fillCircle(120, 226, 13, pack565(255, 176, 54));
      seg7(114, 217, 12, 18, 3, reminders > 9 ? 9 : reminders, pack565(40, 28, 8));
    }
    if (wcond >= 0) {                    // погода: иконка + температура (лаконично, сверху)
      drawWeatherIcon(100, 34, wcond);
      int tx = 128, ty = 24; uint16_t tc = pack565(190, 205, 230);
      int tv = wtemp, av = tv < 0 ? -tv : tv;
      if (tv < 0) { fillRect(tx, ty + 9, 8, 3, tc); tx += 12; }
      if (av >= 10) { seg7(tx, ty, 12, 20, 3, (av / 10) % 10, tc); tx += 16; }
      seg7(tx, ty, 12, 20, 3, av % 10, tc); tx += 15;
      fillCircle(tx + 2, ty + 2, 2, tc);
    }
  }

  int b = bright < 0 ? 255 : (bright > 255 ? 255 : bright);
  if (state == 7) b = b * 45 / 100;    // сон — заметно темнее

  uint32_t t_blit = micros();
  static int lastMin = -1, lastRem = -1; int dh2;
  if (fullBlit) {   // весь экран -> целиком
    lastMin = minute; lastRem = reminders;
    dimFB(0, H, b);
    it.draw_pixels_at(0, 0, W, H, FB, esphome::display::COLOR_ORDER_RGB, esphome::display::COLOR_BITNESS_565, true); dh2 = H;
  } else {
    dimFB(by0, bh, b);
    it.draw_pixels_at(0, by0, W, bh, FB + by0 * rb, esphome::display::COLOR_ORDER_RGB, esphome::display::COLOR_BITNESS_565, true); dh2 = bh;
    if (state == 0 && (minute != lastMin || reminders != lastRem)) {
      dimFB(174, 66, b);
      it.draw_pixels_at(0, 174, W, 66, FB + 174 * rb, esphome::display::COLOR_ORDER_RGB, esphome::display::COLOR_BITNESS_565, true);
      lastMin = minute; lastRem = reminders; dh2 += 66;
    }
  }
  uint32_t t_end = micros();
  static uint32_t la = 0;
  if (now - la > 2000) { ESP_LOGI("face", "render=%u us blit=%u us строк=%d", (unsigned)(t_blit - t_start), (unsigned)(t_end - t_blit), dh2); la = now; }
}

}  // namespace face
