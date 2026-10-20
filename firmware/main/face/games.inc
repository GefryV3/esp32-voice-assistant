// Задача, игры и счётчик напоминаний. Включается в render.cpp внутри безымянного namespace, потому что
// пользуется его примитивами (px, mix, capsule, arc, disc, draw_text…). Стиль тот же: тёмный фон,
// цвета iOS, ничего лишнего, плавное появление через s.game_a / s.task_a.

// ── мелочи ──

const Rgb C_BLUE = {10, 132, 255}, C_GREEN = {48, 209, 88}, C_RED = {255, 69, 58}, C_ORANGE = {255, 159, 10};
const Rgb C_PURPLE = {191, 90, 242}, C_TEAL = {64, 200, 224}, C_PINK = {255, 55, 95}, C_YELLOW = {255, 214, 10};
const Rgb C_WHITE = {244, 244, 250};

inline float ease_out(float p) {
  p = clamp01(p);
  return 1 - (1 - p) * (1 - p) * (1 - p);
}
inline float ease_out_back(float p) {
  p = clamp01(p);
  const float c = 1.9f, q = p - 1;
  return 1 + (c + 1) * q * q * q + c * q * q;
}
inline Rgb shade(const Rgb &c, float k) { return {fminf(255, c.r * k), fminf(255, c.g * k), fminf(255, c.b * k)}; }

// Арктангенс без таблиц: ошибка около 0.0001 рад, на порядок быстрее atan2f
inline float fast_atan2(float y, float x) {
  const float ax = fabsf(x), ay = fabsf(y), mx = fmaxf(ax, ay), mn = fminf(ax, ay);
  if (mx < 1e-9f) return 0;
  const float a = mn / mx, s = a * a;
  float r = ((-0.0464964749f * s + 0.15931422f) * s - 0.327622764f) * s * a + a;
  if (ay > ax) r = PI / 2 - r;
  if (x < 0) r = PI - r;
  return y < 0 ? -r : r;
}

// Эллипс с мягким краем
void ellipse(float cx, float cy, float rx, float ry, const Rgb &c, float alpha) {
  if (rx < 0.5f || ry < 0.5f || alpha < 0.004f) return;
  int x0, y0, x1, y1;
  if (!clip(cx - rx - 1, cy - ry - 1, cx + rx + 1, cy + ry + 1, x0, y0, x1, y1)) return;
  const float m = fminf(rx, ry), irx = 1 / rx, iry = 1 / ry;
  for (int y = y0; y <= y1; y++) {
    const float dy = (y + 0.5f - cy) * iry;
    for (int x = x0; x <= x1; x++) {
      const float dx = (x + 0.5f - cx) * irx;
      const float a = clamp01(0.5f - (sqrtf(dx * dx + dy * dy) - 1) * m) * alpha;
      if (a > 0.003f) mix(px(x, y), c, a);
    }
  }
}

// Три вещи для всех сцен: где центр и во сколько раз сцена «всплывает»
constexpr float SX = 120, SY = 142;

// ── Кольцо задачи ──

// Бегущая дуга с хвостом: head — доля круга от верха по часовой, len — длина хвоста (доля)
void ring_comet(float R, float hw, float head, float len, const Rgb &o, float alpha, float track) {
  if (alpha < 0.004f) return;
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
        const float dr = fabsf(s_qr[i] * (1.0f / 64) - R);
        if (dr > hw + 1) continue;
        uint8_t *p = px(x, y);
        const float cover = clamp01(0.5f - (dr - hw));
        if (track > 0) mix(p, o, cover * track * alpha);
        const float u = ((angle8(x, y, s_qa[i]) + 64) & 255) * (1.0f / 256);
        float d = head - u;
        d -= floorf(d);  // сколько позади головы
        if (d >= len) continue;
        const float f = 1 - d / len;
        const float k = f * f * (3 - 2 * f) * cover * alpha;
        if (k > 0.003f) mix(p, o, k);
      }
    }
  }
}

void draw_task(const Scene &s) {
  const float a = s.task_a;
  if (a < 0.01f || s.task == Task::Idle) return;
  Rgb c = rgb(s.accent);
  const char *status = "";
  char pct[16];
  switch (s.task) {
    case Task::Working:
      c = {10, 132, 255};
      if (s.task_spin < 0) {
        snprintf(pct, sizeof pct, "%d %%", (int)(s.task_frac * 100 + 0.5f));
        status = pct;
      } else {
        status = "Работаю";
      }
      break;
    case Task::NeedsInput:
    case Task::NeedsPermission:
      c = C_ORANGE;
      status = "Ждёт тебя";
      break;
    case Task::Done:
      c = C_GREEN;
      status = "Готово";
      break;
    case Task::Failed:
      c = C_RED;
      status = "Не вышло";
      break;
    case Task::Idle:
      break;
  }
  const float R = 115, hw = 2.5f;
  if (s.task == Task::Working) {
    if (s.task_spin >= 0) ring_comet(R, hw, s.task_spin / TAU, 0.32f, c, a, 0.16f);
    else ring_band(R, hw, s.task_frac, c, a, 0.16f);
  } else if (s.task == Task::NeedsInput || s.task == Task::NeedsPermission) {
    ring_band(R, hw + 0.8f, 1, c, a * (0.55f + 0.45f * s.pulse), 0);
  } else {
    ring_band(R, hw, 1, c, a, 0);
  }
  if (!s.task_big || s_y1 < 150 || s_y0 > 205) return;
  char line[96];
  fit(text::kUiBold, s.task_label, 168, line, sizeof line);
  if (line[0]) draw_text(text::kUiBold, line, C, 170, 1, true, rgb(s.ink), a);
  draw_text(text::kUi, status, C, 191, 1, true, s.task == Task::Working ? rgb(s.ink) : c, a * (s.task == Task::Working ? 0.55f : 0.95f));
}

// ── Счётчик напоминаний: пилюля с колокольчиком под часами ──

void draw_reminders(const Scene &s) {
  const float a = s.rem_a;
  if (a < 0.01f || s.reminders <= 0 || s_y1 < 194 || s_y0 > 222) return;
  char n[16];
  snprintf(n, sizeof n, "%d", s.reminders);
  const float tw = text::width(text::kUiBold, n), total = 13 + 5 + tw, x0 = C - total / 2, y = 208;
  const Rgb acc = rgb(s.accent);
  rrect(C, y, total / 2 + 8, 10, 10, acc, 0.5f * a, 1.1f);
  bell_icon(x0 + 6, y - 1, acc, a);
  draw_text(text::kUiBold, n, x0 + 18, y + 5, 1, false, rgb(s.ink), a);
}

// ── Тихий режим: точка и подпись сверху ──

void draw_quiet(const Scene &s) {
  const float a = s.quiet_a;
  if (a < 0.01f || s_y0 > 62 || s_y1 < 30) return;
  const float tw = text::width(text::kUi, "Тишина"), total = 8 + 7 + tw, x0 = C - total / 2;
  disc(x0 + 4, 44, 3.6f, rgb(s.accent), a * 0.9f);
  draw_text(text::kUi, "Тишина", x0 + 15, 49, 1, false, rgb(s.ink), a * 0.7f);
}

// ── Монетка ──

void game_coin(const Scene &s, float a) {
  const float t = s.game_t, T = 2.2f;
  const int result = s.game_p0 ? 1 : 0, n = 8 + result;  // чётное число полуоборотов — орёл
  const float p = clamp01(t / T), theta = PI * n * (1 - (1 - p) * (1 - p));
  float lift = 56 * 4 * p * (1 - p);
  if (t > T) lift = 7 * expf(-(t - T) * 7) * fabsf(sinf((t - T) * 17));  // отскок после посадки
  const float cs = cosf(theta), sx = fabsf(cs), cy = SY + 8 - lift, R = 46;
  // тень на «полу» сжимается, пока монета высоко
  ellipse(SX, SY + 62, R * (1 - lift / 110), 7 * (1 - lift / 110), {0, 0, 0}, 0.5f * a);
  const Rgb edge = {150, 104, 8}, rim = {255, 223, 110}, face = {255, 196, 20};
  const float th = 5 * (1 - sx);  // толщина ребра видна, когда монета стоит на ребре
  ellipse(SX, cy + th, R, R * sx, edge, a);
  ellipse(SX, cy, R, R * sx, cs >= 0 ? rim : shade(rim, 0.94f), a);
  ellipse(SX, cy, R - 5, (R - 5) * sx, cs >= 0 ? face : shade(face, 0.95f), a);
  const float ea = smooth((sx - 0.4f) / 0.4f) * a;
  if (ea > 0.01f) {
    const bool heads = cs >= 0;
    draw_text(text::kTitle, heads ? "О" : "Р", SX, cy + 8, 1, true, {138, 92, 0}, ea);
    arc(SX, cy, R - 12, 1.6f, 0, TAU, {190, 130, 8}, ea * 0.7f);
  }
  const float ra = smooth((t - T - 0.1f) / 0.4f) * a;
  if (ra > 0.01f && s_y1 > 190)
    draw_text(text::kTitle, result ? "Решка" : "Орёл", SX, 214, 1, true, rgb(s.ink), ra);
}

// ── Кубик ──

const int8_t PIPS[7][6][2] = {{}, {{0, 0}}, {{-1, -1}, {1, 1}}, {{-1, -1}, {0, 0}, {1, 1}},
                              {{-1, -1}, {1, -1}, {-1, 1}, {1, 1}},
                              {{-1, -1}, {1, -1}, {0, 0}, {-1, 1}, {1, 1}},
                              {{-1, -1}, {1, -1}, {-1, 0}, {1, 0}, {-1, 1}, {1, 1}}};
const int PIP_N[7] = {0, 1, 2, 3, 4, 5, 6};

void dice_cube(float cx, float cy, float half, float rot, int face, float alpha) {
  const float cs = cosf(rot), sn = sinf(rot), ext = half * 1.42f + 2, rr = half * 0.3f, sp = half * 0.5f;
  const float pr = half * 0.14f;
  int x0, y0, x1, y1;
  if (!clip(cx - ext, cy - ext, cx + ext, cy + ext, x0, y0, x1, y1)) return;
  for (int y = y0; y <= y1; y++) {
    for (int x = x0; x <= x1; x++) {
      const float dx = x + 0.5f - cx, dy = y + 0.5f - cy;
      const float u = dx * cs + dy * sn, v = -dx * sn + dy * cs;
      const float qx = fabsf(u) - half + rr, qy = fabsf(v) - half + rr;
      const float ox = qx > 0 ? qx : 0, oy = qy > 0 ? qy : 0;
      const float d = sqrtf(ox * ox + oy * oy) + fminf(fmaxf(qx, qy), 0) - rr;
      const float cov = clamp01(0.5f - d);
      if (cov <= 0.003f) continue;
      const float k = 1.0f - 0.1f * (v / half + 1) * 0.5f - 0.1f * clamp01(1 + d * 0.22f);  // сверху светлее, к краю чуть темнее
      Rgb c = shade(C_WHITE, k);
      float pa = 0;
      for (int i = 0; i < PIP_N[face]; i++) {
        const float px_ = PIPS[face][i][0] * sp, py_ = PIPS[face][i][1] * sp;
        const float ddx = u - px_, ddy = v - py_;
        pa = fmaxf(pa, clamp01(pr + 0.5f - sqrtf(ddx * ddx + ddy * ddy)));
      }
      if (pa > 0) c = {c.r + (28 - c.r) * pa, c.g + (28 - c.g) * pa, c.b + (36 - c.b) * pa};
      mix(px(x, y), c, cov * alpha);
    }
  }
}

void game_dice(const Scene &s, float a) {
  const float t = s.game_t, T = 1.5f, p = clamp01(t / T);
  const int result = s.game_p0 < 1 ? 1 : (s.game_p0 > 6 ? 6 : s.game_p0);
  // грани мелькают, к концу замедляются и садятся на выпавшую
  int face = result;
  if (p < 0.78f) {
    const uint32_t step = (uint32_t)(t * (14 - 9 * p));
    face = 1 + (int)((step * 2654435761u >> 16) % 6);
  }
  const float rot = 2 * TAU * (1 - p) * (1 - p) * 1.0f;
  float lift = 0;
  if (t < T) lift = 46 * (1 - p) * fabsf(sinf(t * PI / 0.46f));
  else lift = 5 * expf(-(t - T) * 8) * fabsf(sinf((t - T) * 20));
  ellipse(SX, SY + 54, 36 * (1 - lift / 90), 6 * (1 - lift / 90), {0, 0, 0}, 0.5f * a);
  dice_cube(SX + 14 * sinf(p * 3) * (1 - p), SY + 8 - lift, 34, rot, face, a);
  const float ra = smooth((t - T) / 0.35f) * a;
  if (ra > 0.01f && s_y1 > 190) {
    char buf[24];
    snprintf(buf, sizeof buf, "Выпало %d", result);
    draw_text(text::kTitle, buf, SX, 214, 1, true, rgb(s.ink), ra);
  }
}

// ── Колесо ──

void game_wheel(const Scene &s, float a) {
  const int count = s.game_p0 < 2 ? 2 : (s.game_p0 > 8 ? 8 : s.game_p0);
  const int result = s.game_p1 < 0 ? 0 : (s.game_p1 >= count ? count - 1 : s.game_p1);
  const float t = s.game_t, T = 2.7f, S = TAU / count, R = 74, cy = SY + 4;
  const float fin = 5 * TAU - (result + 0.5f) * S;
  const float rot = fin * ease_out(t / T);
  const bool stopped = t >= T;
  static const Rgb COLORS[8] = {C_BLUE, C_ORANGE, C_GREEN, C_PINK, C_PURPLE, C_TEAL, C_YELLOW, C_RED};
  int x0, y0, x1, y1;
  if (clip(SX - R - 2, cy - R - 2, SX + R + 2, cy + R + 2, x0, y0, x1, y1)) {
    const float invS = 1 / S;
    for (int y = y0; y <= y1; y++) {
      const float dy = y + 0.5f - cy;
      for (int x = x0; x <= x1; x++) {
        const float dx = x + 0.5f - SX, r2 = dx * dx + dy * dy;
        if (r2 > (R + 1) * (R + 1)) continue;
        const float dist = sqrtf(r2);
        float rel = fast_atan2(dx, -dy) - rot;  // угол от верха по часовой минус поворот колеса
        rel -= TAU * floorf(rel * (1 / TAU));
        int idx = (int)(rel * invS);
        if (idx >= count) idx = count - 1;
        const float frac = rel * invS - idx, m = fminf(frac, 1 - frac) * S;  // угол до границы сектора
        Rgb c = COLORS[idx];
        float k = 0.8f + 0.2f * (1 - dist / R);
        if (stopped && idx == result) k *= 1.2f + 0.12f * sinf((t - T) * 7);
        c = shade(c, k);
        const float cover = clamp01(R + 0.5f - dist);
        const float gap = clamp01(dist * m - 0.7f);  // тонкий зазор между секторами
        const float rimk = clamp01(dist - (R - 2.5f));
        c = {c.r + (240 - c.r) * rimk * 0.8f, c.g + (240 - c.g) * rimk * 0.8f, c.b + (244 - c.b) * rimk * 0.8f};
        mix(px(x, y), c, cover * gap * a);
      }
    }
  }
  // номера секторов едут вместе с колесом
  for (int i = 0; i < count; i++) {
    const float mid = rot + (i + 0.5f) * S, rr = R * (count <= 4 ? 0.55f : 0.66f);
    char n[4];
    snprintf(n, sizeof n, "%d", i + 1);
    draw_text(text::kUiBold, n, SX + rr * sinf(mid), cy - rr * cosf(mid) + 5, 1, true, {255, 255, 255}, a);
  }
  disc(SX, cy, 9, {20, 20, 26}, a);
  // указатель сверху
  const Rgb pin = {250, 250, 255};
  disc(SX, cy - R - 7, 6.5f, pin, a);
  capsule(SX, cy - R - 7, SX, cy - R + 3, 6, pin, a);
  disc(SX, cy - R + 3, 3, pin, a);
}

// ── Камень, ножницы, бумага ──

void rps_icon(int kind, float cx, float cy, float sc, float a) {
  if (kind == 0) {  // камень: гладкий булыжник
    ellipse(cx, cy, 40 * sc, 34 * sc, {96, 112, 136}, a);
    ellipse(cx - 2 * sc, cy - 3 * sc, 36 * sc, 29 * sc, {136, 154, 180}, a);
    soft(cx - 12 * sc, cy - 12 * sc, 22 * sc, {255, 255, 255}, 0.28f * a);
    capsule(cx + 6 * sc, cy + 14 * sc, cx + 20 * sc, cy + 8 * sc, 2 * sc, {96, 112, 136}, 0.6f * a);
  } else if (kind == 1) {  // бумага: лист с загнутым уголком и строчками
    rrect(cx, cy, 30 * sc, 38 * sc, 7 * sc, C_WHITE, a, 0);
    for (int i = 0; i < 4; i++)
      capsule(cx - 17 * sc, cy - 16 * sc + i * 11 * sc, cx + 17 * sc - (i == 3 ? 12 * sc : 0), cy - 16 * sc + i * 11 * sc,
              2.4f * sc, {196, 202, 218}, a);
  } else {  // ножницы: два лезвия крестом и два кольца
    const Rgb steel = {206, 214, 226};
    capsule(cx - 22 * sc, cy - 30 * sc, cx + 10 * sc, cy + 14 * sc, 6 * sc, steel, a);
    capsule(cx + 22 * sc, cy - 30 * sc, cx - 10 * sc, cy + 14 * sc, 6 * sc, shade(steel, 0.86f), a);
    arc(cx - 15 * sc, cy + 26 * sc, 11 * sc, 4.5f * sc, 0, TAU, C_RED, a);
    arc(cx + 15 * sc, cy + 26 * sc, 11 * sc, 4.5f * sc, 0, TAU, C_RED, a);
    disc(cx, cy - 3 * sc, 3.2f * sc, {90, 96, 110}, a);
  }
}

void game_rps(const Scene &s, float a) {
  const float t = s.game_t, T = 1.5f;
  const int mine = s.game_p0 < 0 ? 0 : (s.game_p0 > 2 ? 2 : s.game_p0), res = s.game_p1;
  const float cy = SY + 4;
  if (t < T) {  // раз, два, три — кулак качается
    const float bounce = fabsf(sinf(t * PI / 0.5f));
    rps_icon(0, SX, cy - 26 * bounce, 0.85f, a);
    if (s_y1 > 190) draw_text(text::kUi, "Раз… два… три!", SX, 208, 1, true, rgb(s.ink), a * 0.6f);
    return;
  }
  const float sc = 0.7f + 0.3f * ease_out_back((t - T) / 0.5f);
  rps_icon(mine, SX, cy, sc, a * clamp01((t - T) / 0.12f));
  const float ra = smooth((t - T - 0.35f) / 0.35f) * a;
  if (ra > 0.01f && s_y1 > 190) {
    const char *msg = res == 1 ? "Я выиграла" : (res == 2 ? "Ты выиграл" : "Ничья");
    const Rgb c = res == 1 ? C_GREEN : (res == 2 ? C_ORANGE : rgb(s.ink));
    draw_text(text::kTitle, msg, SX, 214, 1, true, c, ra);
  }
}

// ── Шар предсказаний ──

const char *const BALL[11] = {"", "Бесспорно", "Да", "Без сомнений", "Точно да", "Конечно",
                              "Спроси позже", "Не ясно", "Не думаю", "Нет", "Вряд ли"};

void game_magic8(const Scene &s, float a) {
  const float t = s.game_t;
  const int ans = s.game_p0 < 1 ? 6 : (s.game_p0 > 10 ? 10 : s.game_p0);
  float ox = 0, oy = 0;
  if (t < 1.5f) {  // встряхивают
    const float k = 1 - t / 1.5f;
    ox = 8 * sinf(t * 36) * k;
    oy = 4 * sinf(t * 29 + 1) * k;
  }
  const float cx = SX + ox, cy = SY + 4 + oy, R = 68;
  ellipse(SX, SY + 78, R * 0.8f, 6, {0, 0, 0}, 0.45f * a);
  disc(cx, cy, R, {26, 28, 38}, a);
  disc(cx, cy + 3, R - 6, {10, 11, 16}, a);
  soft(cx - 24, cy - 30, 34, {255, 255, 255}, 0.22f * a);  // глянец
  arc(cx, cy, R - 1, 1.5f, PI * 1.05f, PI * 1.55f, {255, 255, 255}, 0.35f * a);
  // окошко
  disc(cx, cy + 2, 42, {18, 26, 96}, a);
  arc(cx, cy + 2, 42, 1.4f, 0, TAU, {70, 90, 200}, 0.7f * a);
  const float eight = (1 - smooth((t - 1.4f) / 0.3f)) * a, show = smooth((t - 1.7f) / 0.5f) * a;
  if (eight > 0.01f) draw_text(text::kTitle, "8", cx, cy + 12, 1, true, {235, 240, 255}, eight);
  if (show > 0.01f) {
    char lines[2][128];
    const int n = wrap2(text::kUiBold, BALL[ans], 74, lines);
    const Rgb c = ans <= 5 ? Rgb{150, 255, 180} : (ans <= 7 ? Rgb{255, 224, 140} : Rgb{255, 150, 140});
    const float y0 = cy + 2 + (n == 1 ? 5 : -2) + (1 - show / (a > 0 ? a : 1)) * 6;
    for (int i = 0; i < n; i++) draw_text(text::kUiBold, lines[i], cx, y0 + i * 18, 1, true, c, show);
  }
}

// ── Крестики-нолики ──

const int8_t TTT_LINES[8][3] = {{0, 1, 2}, {3, 4, 5}, {6, 7, 8}, {0, 3, 6}, {1, 4, 7}, {2, 5, 8}, {0, 4, 8}, {2, 4, 6}};

void game_ttt(const Scene &s, float a) {
  const float cell = 40, x0 = SX - 1.5f * cell, y0 = SY + 4 - 1.5f * cell;
  const Rgb line = {200, 206, 222};
  for (int i = 1; i <= 2; i++) {
    capsule(x0 + i * cell, y0 + 4, x0 + i * cell, y0 + 3 * cell - 4, 3, line, 0.4f * a);
    capsule(x0 + 4, y0 + i * cell, x0 + 3 * cell - 4, y0 + i * cell, 3, line, 0.4f * a);
  }
  for (int i = 0; i < 9; i++) {
    const char m = s.board[i];
    if (m != 'X' && m != 'O') continue;
    const float cx = x0 + (i % 3 + 0.5f) * cell, cy = y0 + (i / 3 + 0.5f) * cell;
    const float k = ease_out_back(s.cell_a[i]), al = a * clamp01(s.cell_a[i] * 3);
    if (m == 'X') {
      const float r = 11 * k;
      capsule(cx - r, cy - r, cx + r, cy + r, 5, C_TEAL, al);
      capsule(cx + r, cy - r, cx - r, cy + r, 5, C_TEAL, al);
    } else {
      arc(cx, cy, 11.5f * k, 5, 0, TAU, C_ORANGE, al);
    }
  }
  for (const auto &l : TTT_LINES) {  // выигрышная линия перечёркивается
    const char m = s.board[l[0]];
    if ((m != 'X' && m != 'O') || s.board[l[1]] != m || s.board[l[2]] != m) continue;
    const float ax = x0 + (l[0] % 3 + 0.5f) * cell, ay = y0 + (l[0] / 3 + 0.5f) * cell;
    const float bx = x0 + (l[2] % 3 + 0.5f) * cell, by = y0 + (l[2] / 3 + 0.5f) * cell;
    float born = 1;
    for (int j = 0; j < 3; j++) born = fminf(born, s.cell_a[l[j]]);
    born = ease_out(born);
    capsule(ax, ay, ax + (bx - ax) * born, ay + (by - ay) * born, 5, C_GREEN, 0.9f * a * (0.7f + 0.3f * s.pulse));
    break;
  }
}

void draw_game(const Scene &s) {
  const float a = s.game_a;
  if (a < 0.01f || s.game == Game::None || s_y1 < 52 || s_y0 > 232) return;
  switch (s.game) {
    case Game::Coin: game_coin(s, a); break;
    case Game::Dice: game_dice(s, a); break;
    case Game::Wheel: game_wheel(s, a); break;
    case Game::Rps: game_rps(s, a); break;
    case Game::Magic8: game_magic8(s, a); break;
    case Game::Ttt: game_ttt(s, a); break;
    case Game::None: break;
  }
}
