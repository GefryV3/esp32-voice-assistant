// Анимация лица: из того, что известно колонке (Input), получается кадр (Scene).
// Поведение — как в макете design/screen-mockup.html: все величины едут пружинами, поэтому
// любое движение можно прервать на полпути без рывка; глаза моргают и блуждают взглядом,
// после тишины засыпают, а часы переезжают и остаются на экране. Поверх — карточки с
// телефона и мини-плеер.
#include <cmath>
#include <cstring>

#include "esp_random.h"
#include "face/face.h"

namespace face {
namespace {

constexpr float TAU = 6.28318530718f, PI = 3.14159265359f;
constexpr int C = 120;
constexpr uint32_t DROWSY_MS = 3400;   // засыпание: веки тяжелеют
constexpr uint32_t ALERT_MS = 9000;    // сработавший таймер пульсирует, пока не ответят

inline float clamp01(float v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
inline float smooth(float t) {
  t = clamp01(t);
  return t * t * (3 - 2 * t);
}
inline float frand() { return (esp_random() >> 8) * (1.0f / 16777216.0f); }

// Пружина, как в iOS: response — время реакции, damping — 1 без отскока.
struct Spring {
  float v = 0, target = 0, vel = 0, r = 0.3f, d = 1, k = 1;
  void init(float v0, float response, float damping) {
    v = target = v0;
    vel = 0;
    r = response;
    d = damping;
    k = (TAU / r) * (TAU / r);
  }
  void step(float dt) {
    const float a = -k * (v - target) - (2 * TAU * d / r) * vel;
    vel += a * dt;
    v += vel * dt;
  }
};

enum EyeParam { EX, EY, EW, EH, ERAD, ELIDT, ELIDA, ELIDB, EBEND, EBRIGHT, EYE_N };
enum Param {
  GXL, GYL, GXR, GYR,  // взгляд у глаз чуть разный по скорости — живее синхронного робота
  FACE_Y, SCALE, CLOCK_Y, CLOCK_SIZE, CLOCK_A, WX_A, CAP_A, TIMER_A,
  RING, RING_W, THINK, ALERT, GLOW, BG, ARC_A, AURA, CARD_A, REVEAL, AURA_SPEED,
  CR, CG, CB, GR, GG, GB, A0R, A0G, A0B, A1R, A1G, A1B, A2R, A2G, A2B, ACR, ACG, ACB,
  INKR, INKG, INKB,
  PARAM_N
};

Spring s_eye[2][EYE_N];
Spring s_p[PARAM_N];

const float ICE[3] = {226, 236, 255}, ICE_GLOW[3] = {64, 128, 255};
const float AMBER[3] = {255, 196, 110}, AMBER_GLOW[3] = {255, 128, 20};
const float RED[3] = {255, 96, 72}, RED_GLOW[3] = {190, 24, 12};  // ночь: красный, как на Apple Watch
const float CALL_GLOW[3] = {48, 209, 120};
const float INK[3] = {236, 240, 247}, INK_RED[3] = {255, 80, 60};

// Живой фон: три мягких пятна, цвет по времени суток; accent — для подписей и колец
struct Palette {
  float blobs[3][3];
  float accent[3];
};
const Palette PAL_MORNING = {{{255, 150, 70}, {215, 80, 130}, {110, 80, 200}}, {255, 196, 130}};
const Palette PAL_DAY = {{{30, 140, 230}, {70, 100, 240}, {20, 170, 170}}, {130, 205, 255}};
const Palette PAL_EVENING = {{{170, 70, 230}, {255, 100, 60}, {80, 60, 210}}, {225, 165, 255}};
const Palette PAL_NIGHT = {{{30, 50, 170}, {90, 40, 170}, {10, 90, 150}}, {140, 165, 255}};
const Palette PAL_ALERT = {{{255, 120, 20}, {255, 70, 40}, {200, 100, 20}}, {255, 180, 80}};
const Palette PAL_RED = {{{120, 12, 6}, {80, 6, 24}, {110, 30, 0}}, {255, 96, 72}};
const Palette PAL_CALL = {{{20, 160, 80}, {10, 120, 110}, {40, 180, 60}}, {48, 209, 88}};

const Palette &palette_for(int hour) {
  if (hour >= 6 && hour < 10) return PAL_MORNING;
  if (hour >= 10 && hour < 17) return PAL_DAY;
  if (hour >= 17 && hour < 22) return PAL_EVENING;
  return PAL_NIGHT;
}

struct Blob {
  float x, y, ax, ay, px, py, f;
};
const Blob BLOBS[3] = {
    {78, 80, 34, 26, 23, 29, 0.3f},
    {168, 110, 30, 38, 31, 19, 2.1f},
    {112, 176, 40, 22, 37, 27, 4.2f},
};

enum class Mode : uint8_t { Idle, Drowsy, Sleep, Listen, Think, Speak, Timer, Alert };

struct Blink {
  uint32_t at;
  uint16_t c, h, o;  // закрыть, подержать, открыть — мс
};

struct State {
  bool inited = false;
  Mode mode = Mode::Idle;
  uint32_t mode_at = 0, idle_since = 0, last_activity = 0;
  bool night_sleep = false;
  float gaze_x = 0, gaze_y = 0;
  uint32_t next_saccade = 0, next_blink = 0;
  Blink blinks[4] = {};
  int nblinks = 0;
  uint32_t glance_down = 0, glance_up = 0;
  int last_minute = -1, last_wcond = -2;
  float level = 0, amp = 0, aura_t = 0;
  struct Zz {
    uint32_t at;
    float s;
  } zs[3] = {};
  int nz = 0;
  uint32_t next_z = 0;
};

State st;
Scene sc;

void blink(uint32_t at, bool slow) {
  if (st.nblinks == 4) {
    memmove(st.blinks, st.blinks + 1, sizeof(Blink) * 3);
    st.nblinks = 3;
  }
  st.blinks[st.nblinks++] = slow ? Blink{at, 220, 160, 380} : Blink{at, 70, 40, 130};
}

float blink_amount(uint32_t now) {
  float m = 0;
  for (int i = 0; i < st.nblinks; i++) {
    const Blink &b = st.blinks[i];
    if (now < b.at) continue;
    const uint32_t t = now - b.at;
    float v = 0;
    if (t < b.c) v = smooth((float)t / b.c);
    else if (t < (uint32_t)(b.c + b.h)) v = 1;
    else if (t < (uint32_t)(b.c + b.h + b.o)) v = 1 - smooth((float)(t - b.c - b.h) / b.o);
    if (v > m) m = v;
  }
  return m;
}

void prune_blinks(uint32_t now) {
  int n = 0;
  for (int i = 0; i < st.nblinks; i++)
    if (now < st.blinks[i].at || now - st.blinks[i].at < 1500) st.blinks[n++] = st.blinks[i];
  st.nblinks = n;
}

void set_mode(Mode m, uint32_t now) {
  const Mode prev = st.mode;
  st.mode = m;
  st.mode_at = now;
  if (m == Mode::Idle) st.idle_since = now;
  if (m == Mode::Sleep) st.next_z = now + 900;
  else st.nz = 0;
  const bool was_asleep = prev == Mode::Sleep || prev == Mode::Drowsy;
  if (was_asleep && m != Mode::Sleep && m != Mode::Drowsy) {  // просыпается: медленно, потом быстро
    blink(now + 140, true);
    blink(now + 760, false);
    st.next_blink = now + 3200;
  }
  if (m == Mode::Listen && prev != Mode::Listen) {  // мгновенная реакция; сияние «наливается» снизу
    blink(now, false);
    s_p[REVEAL].v = 0;
    s_p[REVEAL].vel = 0;
  }
}

void init_springs() {
  for (int e = 0; e < 2; e++) {
    Spring *s = s_eye[e];
    s[EX].init(0, .32f, .82f);
    s[EY].init(0, .32f, .82f);
    s[EW].init(25, .3f, .74f);
    s[EH].init(31, .3f, .74f);
    s[ERAD].init(15, .35f, .9f);
    s[ELIDT].init(0, .26f, .9f);
    s[ELIDA].init(0, .3f, .9f);
    s[ELIDB].init(0, .3f, .82f);
    s[EBEND].init(0, .5f, .95f);
    s[EBRIGHT].init(1, .5f, 1);
  }
  s_p[GXL].init(0, .15f, .8f);
  s_p[GYL].init(0, .15f, .8f);
  s_p[GXR].init(0, .19f, .8f);
  s_p[GYR].init(0, .19f, .8f);
  s_p[FACE_Y].init(98, .55f, .88f);
  s_p[SCALE].init(1, .5f, .86f);
  s_p[CLOCK_Y].init(172, .75f, .92f);
  s_p[CLOCK_SIZE].init(40, .75f, .95f);
  s_p[CLOCK_A].init(.92f, .45f, 1);
  s_p[WX_A].init(1, .45f, 1);
  s_p[CAP_A].init(0, .3f, 1);
  s_p[TIMER_A].init(0, .4f, 1);
  s_p[RING].init(0, .12f, 1);
  s_p[RING_W].init(10, .2f, 1);
  s_p[THINK].init(0, .35f, 1);
  s_p[ALERT].init(0, .3f, 1);
  s_p[GLOW].init(.3f, .5f, 1);
  s_p[BG].init(1, .8f, 1);
  s_p[ARC_A].init(1, .45f, 1);
  s_p[AURA].init(.16f, .6f, 1);
  s_p[CARD_A].init(0, .38f, .82f);
  s_p[REVEAL].init(1, .45f, 1);
  s_p[AURA_SPEED].init(1, .8f, 1);
  for (int c = 0; c < 3; c++) {
    s_p[CR + c].init(ICE[c], .5f, 1);
    s_p[GR + c].init(ICE_GLOW[c], .5f, 1);
    s_p[ACR + c].init(PAL_NIGHT.accent[c], .9f, 1);
    s_p[INKR + c].init(INK[c], .6f, 1);
    for (int b = 0; b < 3; b++) s_p[A0R + b * 3 + c].init(PAL_NIGHT.blobs[b][c], .9f, 1);
  }
}

// Голос: слоги с паузами между словами. Пока нет микрофона (этап 2), уровень изображаем.
float speech(float t, float rate, float seed) {
  const float syl = 0.5f + 0.5f * sinf(t * TAU * rate + 2 * sinf(t * 1.7f + seed));
  const float word = 0.5f + 0.5f * sinf(t * TAU * 0.8f + seed * 3);
  return powf(syl, 1.6f) * (0.3f + 0.7f * word);
}

// Какой режим требует сервер прямо сейчас; Idle — ничего активного.
Mode wanted(const Input &in, uint32_t now) {
  if (in.timer_on && in.timer_ms) {
    const uint32_t end = in.timer_start + in.timer_ms;
    if (now >= end && now - end < ALERT_MS && in.activity_at <= end) return Mode::Alert;
  }
  switch (in.voice) {
    case Voice::Listening: return Mode::Listen;
    case Voice::Thinking: return Mode::Think;
    case Voice::Speaking: return Mode::Speak;
    case Voice::Idle: break;
  }
  if (in.timer_on && in.timer_ms && now < in.timer_start + in.timer_ms) return Mode::Timer;
  return Mode::Idle;
}

bool expr_on(const Input &in, uint32_t now) {
  return in.expr != Expr::None && now - in.expr_at < in.expr_ms;
}

bool card_on(const Input &in, uint32_t now) {
  return in.card != Card::None && now - in.card_at < in.card_ms;
}

void update(const Input &in, float dt) {
  const uint32_t now = in.now;
  const bool activity = in.activity_at != st.last_activity;
  st.last_activity = in.activity_at;

  const Mode want = wanted(in, now);
  if (want != Mode::Idle) {
    if (st.mode != want) set_mode(want, now);
    st.night_sleep = false;
  } else {
    const bool expr = expr_on(in, now);
    if (st.mode >= Mode::Listen) set_mode(Mode::Idle, now);  // разговор или таймер закончились
    if (in.night && !expr) {
      if (st.mode != Mode::Sleep) set_mode(Mode::Sleep, now);
      st.night_sleep = true;
    } else {
      if (st.night_sleep) {
        st.night_sleep = false;
        set_mode(Mode::Idle, now);
      }
      if (activity || expr) {
        if (st.mode == Mode::Sleep || st.mode == Mode::Drowsy) set_mode(Mode::Idle, now);
        else st.idle_since = now;
      }
      // пока на экране карточка или играет музыка — не засыпаем, но и не будим
      if (st.mode == Mode::Idle && (card_on(in, now) || in.player_on)) st.idle_since = now;
      if (st.mode == Mode::Idle && now - st.idle_since > in.sleep_after_ms) set_mode(Mode::Drowsy, now);
      else if (st.mode == Mode::Drowsy && now - st.mode_at > DROWSY_MS) set_mode(Mode::Sleep, now);
    }
  }

  if (st.mode != Mode::Sleep && now >= st.next_blink) {
    const bool slow = st.mode == Mode::Drowsy;
    blink(now, slow);
    if (!slow && frand() < 0.22f) blink(now + 260, false);
    st.next_blink = now + (slow ? 1300 + (uint32_t)(frand() * 600) : 2400 + (uint32_t)(frand() * 3800));
  }
  prune_blinks(now);

  if (st.mode == Mode::Idle && now >= st.next_saccade) {
    if (frand() < 0.35f) {
      st.gaze_x = st.gaze_y = 0;
    } else {
      st.gaze_x = (frand() * 2 - 1) * 14;
      st.gaze_y = frand() * 14 - 8;
    }
    st.next_saccade = now + 1200 + (uint32_t)(frand() * 3000);
  }
  // Сменилась минута — глаза на миг опускаются к часам; новая погода — поднимаются к ней
  if (in.have_time && in.minute != st.last_minute) {
    if (st.last_minute >= 0 && st.mode == Mode::Idle && !expr_on(in, now)) st.glance_down = now + 1100;
    st.last_minute = in.minute;
  }
  if (in.wcond != st.last_wcond) {
    if (st.last_wcond != -2 && in.wcond >= 0 && st.mode == Mode::Idle) st.glance_up = now + 900;
    st.last_wcond = in.wcond;
  }

  const float t = now / 1000.0f, kA = 1 - expf(-dt / 0.03f), kR = 1 - expf(-dt / 0.16f);
  float lv = 0;
  if (st.mode == Mode::Listen) {
    const uint32_t p = (now - st.mode_at) % 2600;
    lv = (p > 200 && p < 2000) ? speech(t, 4.2f, 1.3f) : 0.03f;
  }
  st.level += (lv - st.level) * (lv > st.level ? kA : kR);
  float am = 0;
  if (st.mode == Mode::Speak) {
    const uint32_t p = (now - st.mode_at) % 1700;
    am = p < 1450 ? speech(t, 5, 0.4f) : 0.02f;
  }
  st.amp += (am - st.amp) * (am > st.amp ? kA : kR);
  st.aura_t += dt * s_p[AURA_SPEED].v;

  if (st.mode == Mode::Sleep && now >= st.next_z) {
    if (st.nz == 3) {
      st.zs[0] = st.zs[1];
      st.zs[1] = st.zs[2];
      st.nz = 2;
    }
    st.zs[st.nz++] = {now, frand()};
    st.next_z = now + 2600;
  }
  int n = 0;
  for (int i = 0; i < st.nz; i++)
    if (now - st.zs[i].at < 3400) st.zs[n++] = st.zs[i];
  st.nz = n;
}

struct EyeT {
  float x = 0, y = 0, w = 25, h = 31, rad = 15, lidT = 0, lidA = 0, lidB = 0, bend = 0, bright = 1;
};

// Цели для пружин: что должно быть на экране в этом состоянии
void targets(const Input &in) {
  const uint32_t now = in.now;
  const float t = now / 1000.0f;
  EyeT E[2];
  float T[PARAM_N] = {};
  T[FACE_Y] = 98;
  T[SCALE] = 1;
  T[CLOCK_Y] = 172;
  T[CLOCK_SIZE] = 40;
  T[CLOCK_A] = in.have_time ? .92f : 0;
  T[WX_A] = in.wcond >= 0 ? 1 : 0;
  T[CAP_A] = 0;
  T[TIMER_A] = 0;
  T[RING] = 0;
  T[RING_W] = 10;
  T[THINK] = 0;
  T[ALERT] = 0;
  T[GLOW] = .3f;
  T[BG] = 1;
  T[ARC_A] = in.have_time ? 1 : 0;
  T[AURA] = .16f;
  T[CARD_A] = 0;
  T[REVEAL] = 1;
  T[AURA_SPEED] = 1;
  float gx = 0, gy = 0;
  const float *col = ICE, *gcol = ICE_GLOW, *ink = INK;
  const Palette *pal = nullptr;
  auto quiet = [&] {
    T[CLOCK_A] = 0;
    T[CLOCK_Y] = 196;
    T[WX_A] = 0;
    T[ARC_A] = 0;
  };
  auto red = [&] {
    col = RED;
    gcol = RED_GLOW;
    ink = INK_RED;
    pal = &PAL_RED;
  };

  switch (st.mode) {
    case Mode::Idle:
      gx = st.gaze_x;
      gy = st.gaze_y;
      if (now < st.glance_down) gx = 0, gy = 10;
      if (now < st.glance_up) gx = 0, gy = -11;
      for (EyeT &e : E) e.h *= 1 + 0.012f * sinf(t * TAU / 4.2f);
      break;
    case Mode::Drowsy: {
      const float p = smooth((float)(now - st.mode_at) / DROWSY_MS);
      for (EyeT &e : E) e.lidT = 0.12f + 0.48f * p;
      T[FACE_Y] = 98 + 8 * p;
      gy = 3 * p;
      T[ARC_A] *= 1 - 0.4f * p;
      T[AURA] = .16f - .06f * p;
      T[AURA_SPEED] = .6f;
      break;
    }
    case Mode::Sleep: {
      const bool top = in.sleep_clock_top;
      const float b = 0.5f + 0.5f * sinf(t * TAU / 6.5f);  // дыхание
      for (EyeT &e : E) {
        e.w = 23;
        e.h = 3;
        e.rad = 3;
        e.bend = 6;
        e.bright = .38f + .2f * b;
      }
      T[FACE_Y] = top ? 136 : 104;
      T[CLOCK_Y] = top ? 66 : 190;
      T[CLOCK_SIZE] = 30;
      T[CLOCK_A] = in.have_time ? .42f : 0;
      T[WX_A] = 0;
      T[GLOW] = .18f;
      T[BG] = .35f;
      T[ARC_A] = 0;
      T[AURA] = .06f;
      T[AURA_SPEED] = .35f;
      red();
      break;
    }
    case Mode::Listen:
      for (EyeT &e : E) {
        e.w = 26;
        e.h = 33 + 3 * st.level;
      }
      T[FACE_Y] = 110;
      quiet();
      T[CAP_A] = 1;
      T[RING] = .55f + .7f * st.level;
      T[RING_W] = 9 + 10 * st.level;
      T[GLOW] = .36f;
      T[AURA] = .24f + .12f * st.level;
      T[AURA_SPEED] = 3.2f;
      break;
    case Mode::Think:
      for (EyeT &e : E) {
        e.lidT = .3f;
        e.h = 30;
      }
      gx = 10;
      gy = -9;
      T[FACE_Y] = 110;
      quiet();
      T[CAP_A] = 1;
      T[RING] = .7f;
      T[RING_W] = 11;
      T[THINK] = 1;
      T[AURA] = .2f;
      T[AURA_SPEED] = 3.5f;
      break;
    case Mode::Speak:
      for (EyeT &e : E) {
        e.h = 31 * (1 - .12f * st.amp);
        e.lidB = .1f * st.amp;
      }
      T[FACE_Y] = 98 - 2 * st.amp;
      quiet();
      T[CAP_A] = 1;
      T[RING] = .3f + .45f * st.amp;
      T[RING_W] = 8 + 6 * st.amp;
      T[AURA] = .2f + .06f * st.amp;
      T[AURA_SPEED] = 2;
      break;
    case Mode::Timer:
      T[SCALE] = .52f;
      T[FACE_Y] = 62;
      gy = 4;  // смотрит на цифры
      quiet();
      T[TIMER_A] = 1;
      T[AURA] = .1f;
      T[AURA_SPEED] = .6f;
      break;
    case Mode::Alert:
      T[SCALE] = .52f;
      T[FACE_Y] = 62;
      quiet();
      T[TIMER_A] = 1;
      T[ALERT] = 1;
      for (EyeT &e : E) {
        e.w = 27;
        e.h = 35;
      }
      col = AMBER;
      gcol = AMBER_GLOW;
      pal = &PAL_ALERT;
      T[AURA] = .14f;
      T[AURA_SPEED] = 1.5f;
      break;
  }

  // Мини-плеер: в покое глаза уходят выше и покачиваются в такт, по краю — кольцо прогресса
  const bool card = card_on(in, now);
  if (in.player_on && st.mode == Mode::Idle && !card) {
    quiet();
    T[CARD_A] = 1;
    T[SCALE] = .72f;
    T[FACE_Y] = 78;
    if (in.player_playing) {
      gx = 5 * sinf(t * TAU * 0.55f);
      gy = 2 * sinf(t * TAU * 1.1f);
      for (EyeT &e : E) e.lidB = .22f;
    }
  }

  // Карточки с телефона поверх состояния
  if (card) {
    quiet();
    T[CARD_A] = 1;
    T[CAP_A] = 0;
    switch (in.card) {
      case Card::Msg:
        T[SCALE] = .55f;
        T[FACE_Y] = 62;
        gx = 0;
        gy = 7;
        break;
      case Card::Check:
        T[SCALE] = .5f;
        T[FACE_Y] = 56;
        gx = 0;
        gy = 6;
        break;
      case Card::Battery:
        T[SCALE] = .55f;
        T[FACE_Y] = 62;
        gx = 0;
        gy = 7;
        for (EyeT &e : E) {
          e.lidT = .3f;
          e.lidA = -.3f;
        }
        break;
      case Card::Info:
        T[SCALE] = .6f;
        T[FACE_Y] = 70;
        gx = 0;
        gy = 4;
        break;
      case Card::Call:
        T[SCALE] = .8f;
        T[FACE_Y] = 84;
        gx = gy = 0;
        for (EyeT &e : E) {
          e.w = 27;
          e.h = 36;
        }
        pal = &PAL_CALL;
        gcol = CALL_GLOW;
        T[AURA] = .26f;
        T[AURA_SPEED] = 2.5f;
        break;
      case Card::Remark:  // ночью в телефоне: красный, косится — глаза открываются даже из сна
        for (EyeT &e : E) e = EyeT{0, 0, 25, 31, 15, .46f, 0, 0, 0, .85f};
        T[FACE_Y] = 92;
        T[SCALE] = 1;
        gx = 17;
        gy = 1;
        T[BG] = .4f;
        T[AURA] = .07f;
        red();
        break;
      case Card::Wake:
        for (EyeT &e : E) e = EyeT{0, 0, 28, 37, 15, 0, 0, 0, 0, 1};
        T[FACE_Y] = 82;
        T[SCALE] = .9f;
        T[ALERT] = 1;
        col = AMBER;
        gcol = AMBER_GLOW;
        ink = INK;
        pal = &PAL_ALERT;
        T[AURA] = .2f;
        break;
      case Card::None:
        break;
    }
  }

  // Выражения характера поверх состояния
  if (expr_on(in, now)) {
    const uint32_t et = now - in.expr_at;
    EyeT &L = E[0], &R = E[1];
    switch (in.expr) {
      case Expr::Skeptic:  // один глаз прищурен, другой приподнят
        L.lidT = .48f;
        L.lidA = .15f;
        R.h *= 1.1f;
        R.y = -3;
        gx = 6;
        gy = -1;
        break;
      case Expr::Roll:  // закатывает глаза
        for (EyeT &e : E) e.lidT = .3f;
        if (et < 300) {
          gx = -14;
          gy = -8;
        } else if (et < 1300) {
          const float a = PI * (1 - (et - 300) / 1000.0f);
          gx = 15 * cosf(a);
          gy = -13 * sinf(a);
        } else {
          gx = 0;
          gy = 2;
          for (EyeT &e : E) e.lidT = .42f;
        }
        break;
      case Expr::Side:  // косится
        for (EyeT &e : E) e.lidT = .46f;
        gx = 17;
        gy = 1;
        break;
      case Expr::Happy:
        for (EyeT &e : E) e.lidB = .62f;
        T[FACE_Y] -= 4;
        break;
      case Expr::Surprise:
        for (EyeT &e : E) {
          e.w *= 1.1f;
          e.h *= 1.22f;
        }
        gy = -2;
        break;
      case Expr::Sorry:
        for (EyeT &e : E) {
          e.lidT = .34f;
          e.lidA = -.38f;
          e.bright = .85f;
        }
        gy = 7;
        break;
      case Expr::None:
        break;
    }
  }

  if (T[CARD_A] > 0 && pal != &PAL_CALL) T[AURA] *= 0.7f;  // под текстом карточки фон тише

  for (int e = 0; e < 2; e++) {
    Spring *s = s_eye[e];
    s[EX].target = E[e].x;
    s[EY].target = E[e].y;
    s[EW].target = E[e].w;
    s[EH].target = E[e].h;
    s[ERAD].target = E[e].rad;
    s[ELIDT].target = E[e].lidT;
    s[ELIDA].target = E[e].lidA;
    s[ELIDB].target = E[e].lidB;
    s[EBEND].target = E[e].bend;
    s[EBRIGHT].target = E[e].bright;
  }
  for (int k = FACE_Y; k <= AURA_SPEED; k++) s_p[k].target = T[k];
  s_p[GXL].target = s_p[GXR].target = gx;
  s_p[GYL].target = s_p[GYR].target = gy;
  if (!pal) pal = &palette_for(in.have_time ? in.hour : 23);
  for (int c = 0; c < 3; c++) {
    s_p[CR + c].target = col[c];
    s_p[GR + c].target = gcol[c];
    s_p[INKR + c].target = ink[c];
    s_p[ACR + c].target = pal->accent[c];
    for (int b = 0; b < 3; b++) s_p[A0R + b * 3 + c].target = pal->blobs[b][c];
  }
}

void build_scene(const Input &in) {
  const uint32_t now = in.now;
  const float t = now / 1000.0f, scale = s_p[SCALE].v;
  sc.t = t;
  sc.scale = scale;
  const float bl = st.mode == Mode::Sleep ? 0 : blink_amount(now);
  for (int e = 0; e < 2; e++) {
    const Spring *s = s_eye[e];
    const float side = e == 0 ? 1 : -1;
    const float gx = s_p[e == 0 ? GXL : GXR].v, gy = s_p[e == 0 ? GYL : GYR].v;
    Eye &E = sc.eye[e];
    E.side = side;
    E.cx = C - side * 42 * scale + (s[EX].v + gx) * scale;
    E.cy = s_p[FACE_Y].v + (s[EY].v + gy) * scale;
    E.w = fmaxf(1, s[EW].v * scale * (1 + 0.08f * bl));  // моргая, глаз чуть расплющивается
    float h = s[EH].v * scale;
    if (bl > 0) h = fmaxf(1.4f * scale, h * (1 - 0.92f * bl));
    E.h = h;
    E.r = fmaxf(0, fminf(fminf(s[ERAD].v * scale, E.w), h));
    E.lidT = clamp01(s[ELIDT].v);
    E.lidA = s[ELIDA].v;
    E.lidB = clamp01(s[ELIDB].v);
    E.bend = s[EBEND].v * scale;
    E.bright = clamp01(s[EBRIGHT].v);
    E.gpx = -gx * 0.18f * scale;
    E.gpy = -gy * 0.18f * scale;
  }
  for (int c = 0; c < 3; c++) {
    sc.eye_col[c] = s_p[CR + c].v;
    sc.glow_col[c] = s_p[GR + c].v;
    sc.accent[c] = s_p[ACR + c].v;
    sc.ink[c] = s_p[INKR + c].v;
    for (int b = 0; b < 3; b++) sc.aura_col[b][c] = s_p[A0R + b * 3 + c].v;
  }
  // свечение в два слоя: плотное у края и широкое мягкое
  sc.glow = s_p[GLOW].v;
  sc.glow_r = 5 * scale + 2;
  sc.glow2 = sc.glow * 0.55f;
  sc.glow_r2 = 14 * scale + 4;
  sc.bg = s_p[BG].v;
  sc.aura = s_p[AURA].v;
  for (int b = 0; b < 3; b++) {
    sc.blob_x[b] = BLOBS[b].x + BLOBS[b].ax * sinf(TAU * st.aura_t / BLOBS[b].px + BLOBS[b].f);
    sc.blob_y[b] = BLOBS[b].y + BLOBS[b].ay * cosf(TAU * st.aura_t / BLOBS[b].py + BLOBS[b].f * 1.3f);
  }
  sc.ring = s_p[RING].v;
  sc.ring_w = s_p[RING_W].v;
  sc.think = s_p[THINK].v;
  sc.alert = s_p[ALERT].v;
  sc.reveal = s_p[REVEAL].v;
  sc.ring_rot = t * 0.35f;
  sc.comet = t * 4.4f - PI / 2;
  sc.pulse = 0.55f + 0.45f * sinf(t * TAU * 1.1f);

  sc.have_time = in.have_time;
  sc.hour = in.hour;
  sc.minute = in.minute;
  sc.wday = in.wday;
  sc.mday = in.mday;
  sc.month = in.month;
  sc.clock_y = s_p[CLOCK_Y].v;
  sc.clock_size = s_p[CLOCK_SIZE].v;
  sc.clock_a = s_p[CLOCK_A].v;
  sc.wx_a = s_p[WX_A].v;
  sc.wcond = in.wcond;
  sc.wtemp = in.wtemp;
  sc.arc_a = s_p[ARC_A].v;
  sc.agenda_accent = in.agenda_accent;
  sc.agenda_text = in.agenda_text;
  sc.cap_a = s_p[CAP_A].v;
  sc.caption = in.caption;
  sc.icon = in.icon;
  sc.caption_text = in.caption_text;

  sc.timer_a = s_p[TIMER_A].v;
  sc.alert_mode = st.mode == Mode::Alert;
  sc.timer_total = in.timer_ms ? in.timer_ms : 1;
  const uint32_t end = in.timer_start + in.timer_ms;
  sc.timer_rem = (in.timer_on && now < end) ? end - now : 0;
  sc.timer_label = in.timer_label;
  sc.timer_pulse = sc.alert_mode ? 0.6f + 0.4f * cosf((now - st.mode_at) / 1000.0f * TAU * 1.1f) : 1;

  // карточка видна, пока не угасла; плеер — когда нет карточки и колонка в покое
  const bool card = card_on(in, now);
  sc.card_a = s_p[CARD_A].v;
  sc.player = !card && in.player_on && st.mode == Mode::Idle;
  sc.card = sc.player ? Card::None : in.card;
  sc.card_t = (now - in.card_at) / 1000.0f;
  sc.card_level = in.card_level;
  sc.card_app = in.card_app;
  sc.card_title = in.card_title;
  sc.card_text = in.card_text;
  sc.player_playing = in.player_playing;
  sc.player_title = in.player_title;
  sc.player_artist = in.player_artist;
  float pos = (float)in.player_pos;
  if (in.player_playing) pos += (now - in.player_at) / 1000.0f;
  sc.player_frac = in.player_dur > 0 ? clamp01(pos / in.player_dur) : 0;

  const float zx = C + 42 * scale + 18, zy = s_p[FACE_Y].v - 10;
  sc.nz = 0;
  for (int i = 0; i < st.nz; i++) {
    const float p = (now - st.zs[i].at) / 3400.0f;
    if (p < 0 || p > 1) continue;
    sc.z[sc.nz++] = {zx + 7 * sinf(p * PI * 1.2f + st.zs[i].s * 3), zy - 34 * p, 9 + 5 * p,
                     sinf(PI * p) * 0.45f};
  }
  sc.offline = !in.online;
  sc.bright = in.bright;
  sc.sleeping = st.mode == Mode::Sleep && sc.card_a < 0.01f;
}

}  // namespace

const Scene &animate(const Input &in, float dt) {
  if (!st.inited) {
    init_springs();
    st.inited = true;
    st.idle_since = st.mode_at = in.now;
    st.next_blink = in.now + 1500;
    st.next_saccade = in.now + 1800;
  }
  update(in, dt);
  targets(in);
  const int steps = dt > 0 ? (int)ceilf(dt * 240) : 0;
  const float h = steps ? dt / steps : 0;
  for (int i = 0; i < steps; i++) {
    for (auto &eye : s_eye)
      for (Spring &s : eye) s.step(h);
    for (Spring &s : s_p) s.step(h);
  }
  build_scene(in);
  return sc;
}

}  // namespace face
