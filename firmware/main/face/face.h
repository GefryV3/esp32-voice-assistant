#pragma once
// Лицо колонки по макету design/screen-mockup.html. animate() решает, что показать
// (состояние, пружины, моргание, засыпание), render() рисует кадр полосами прямо в экран.
#include <cstdint>

namespace face {

enum class Voice : uint8_t { Idle, Listening, Thinking, Speaking };
enum class Expr : uint8_t { None, Happy, Skeptic, Roll, Side, Surprise, Sorry, Tired, Bored };
enum class Caption : uint8_t { None, Heard, Said, Reply };
enum class Icon : uint8_t { None, Light };
// Карточки с телефона: уведомление, звонок, батарея, сводка, ночная колкость, «вставай»,
// проверка перед сном (её строки разделены « · »).
enum class Card : uint8_t { None, Msg, Call, Battery, Info, Remark, Wake, Check };

// Ход длинной задачи (сообщение task_status) и игры на экране (сообщение game).
enum class Task : uint8_t { Idle, Working, NeedsInput, NeedsPermission, Done, Failed };
enum class Game : uint8_t { None, Coin, Dice, Wheel, Rps, Magic8, Ttt };

// Что известно колонке на момент кадра (снимок модели ui).
struct Input {
  uint32_t now = 0;  // мс от включения
  Voice voice = Voice::Idle;
  Expr expr = Expr::None;
  uint32_t expr_at = 0, expr_ms = 0;
  uint32_t activity_at = 0;  // последнее событие от сервера: от него считается засыпание
  bool night = false, online = false;
  bool have_time = false;  // время пришло от сервера
  int hour = 0, minute = 0, wday = 0, mday = 1, month = 0;
  int wcond = -1, wtemp = 0;
  int bright = 255;
  bool timer_on = false;
  uint32_t timer_start = 0, timer_ms = 0;
  char timer_label[32] = "";
  Caption caption = Caption::None;
  Icon icon = Icon::None;
  char caption_text[128] = "";
  char agenda_accent[48] = "", agenda_text[80] = "";
  uint32_t sleep_after_ms = 180000;
  bool sleep_clock_top = true;
  Card card = Card::None;
  uint32_t card_at = 0, card_ms = 0;
  int card_level = -1;  // заряд для карточки батареи
  char card_app[32] = "", card_title[64] = "", card_text[192] = "";
  bool player_on = false, player_playing = false;
  uint32_t player_at = 0;  // когда пришли pos/dur
  int player_pos = 0, player_dur = 0;  // секунды
  char player_title[64] = "", player_artist[48] = "";
  // задача: кольцо по краю и подпись снизу
  Task task = Task::Idle;
  uint32_t task_at = 0;
  int task_pct = -1;
  char task_label[32] = "";
  // игра: p0…p2 — параметры по виду (см. ui::set_game), board — поле крестиков-ноликов
  Game game = Game::None;
  uint32_t game_at = 0, game_ms = 0;
  int game_p0 = 0, game_p1 = 0;
  char game_board[10] = ".........";
  int reminders = 0;
  bool quiet = false;  // тихий режим: точка и «Тишина»
  // микрофон (считается на колонке, звук никуда не уходит): уровень 0…1, счётчики событий
  float mic_level = 0;
  uint32_t bang_n = 0, beat_n = 0;  // резкий громкий звук (хлопок, стук) / удар ритма
};

struct Eye {
  float cx, cy;     // центр, px
  float w, h, r;    // полуширина, полувысота, скругление
  float lidT, lidA; // верхнее веко: насколько опущено (0…1) и наклон
  float lidB;       // нижнее веко «улыбкой» (0…1)
  float bend;       // изгиб дугой — закрытый глаз во сне
  float bright;     // 0…1
  float side;       // +1 левый глаз, −1 правый: в какую сторону «к носу»
  float gpx, gpy;   // сдвиг блика против взгляда — ощущение глубины
};

// Готовый к отрисовке кадр.
struct Scene {
  float t = 0;  // секунды от включения
  Eye eye[2] = {};
  float eye_col[3] = {}, glow_col[3] = {};
  float glow = 0, glow_r = 12, scale = 1;
  float glow2 = 0, glow_r2 = 18;  // второй, широкий слой свечения
  float ink[3] = {236, 240, 247};  // цвет текста: ночью — красный
  float reveal = 1;                // сияние «наливается» снизу при начале разговора
  bool glass = true, calm = false;
  float bg = 1;
  float aura = 0, aura_col[3][3] = {}, blob_x[3] = {}, blob_y[3] = {};
  float ring = 0, ring_w = 10, think = 0, alert = 0, ring_rot = 0, comet = 0, pulse = 1;
  bool have_time = false;
  int hour = 0, minute = 0, wday = 0, mday = 1, month = 0;
  float clock_y = 172, clock_size = 40, clock_a = 1;
  float wx_a = 1;
  int wcond = -1, wtemp = 0;
  float arc_a = 1, accent[3] = {};
  const char *agenda_accent = "", *agenda_text = "";
  float cap_a = 0;
  Caption caption = Caption::None;
  Icon icon = Icon::None;
  const char *caption_text = "";
  float timer_a = 0, timer_pulse = 1;
  bool alert_mode = false;
  uint32_t timer_rem = 0, timer_total = 1;
  const char *timer_label = "";
  struct Z {
    float x, y, size, a;
  } z[3] = {};
  int nz = 0;
  bool offline = false;
  int bright = 255;
  bool sleeping = false;  // можно реже обновлять кадр
  // карточка с телефона и мини-плеер
  float card_a = 0, card_t = 0;  // проявление 0…1, секунды с начала
  Card card = Card::None;
  int card_level = -1;
  const char *card_app = "", *card_title = "", *card_text = "";
  bool player = false, player_playing = false;
  float player_frac = 0;
  const char *player_title = "", *player_artist = "";
  // задача
  float task_a = 0, task_frac = 0, task_spin = 0, task_flash = 0;  // проявление, доля, угол бегущей дуги, вспышка
  Task task = Task::Idle;
  bool task_big = false;  // есть место для подписи (глаза уменьшены, часы убраны)
  const char *task_label = "";
  float alert_col[3] = {255, 140, 20};  // цвет пульса по краю (янтарь, зелёный, красный)
  // игра
  float game_a = 0, game_t = 0;
  Game game = Game::None;
  int game_p0 = 0, game_p1 = 0;
  char board[10] = ".........";
  float cell_a[9] = {};
  int reminders = 0;
  float rem_a = 0, quiet_a = 0;
  // LED panel: how much it is listening / thinking / speaking / playing music right now (0..1, smoothed) and the loudness
  float led_lis = 0, led_thk = 0, led_spk = 0, led_mus = 0, led_amp = 0;
  // LED panel extras (web mockup design/screen-rest.html): arch eyes of joy, the sleep screen, night grid tint,
  // the no-connection icon, where the face sits and the phase of the fired timer
  float happy = 0;       // 0..1, "joy" arches instead of eyes
  float led_slp = 0;     // 0..1, the sleep screen
  float night_tint = 0;  // 0..1, grid dots turn red (sleep, night remark)
  float offl_a = 0;      // 0..1, the crossed-out Wi-Fi icon
  float face_y = 98;     // face centre line (px), arches follow it
  bool timer_blink = true;  // the fired timer: digits lit / dim
};

bool init();

// Тестовая заставка: красная, зелёная, синяя полосы сверху вниз и «123» слева направо.
// По ней видно, правильно ли настроены цвет и зеркало экрана.
void splash();

const Scene &animate(const Input &in, float dt);

struct Timing {
  uint32_t draw_us = 0;  // счёт кадра
  uint32_t wait_us = 0;  // ожидание экрана (SPI)
};

Timing render(const Scene &s);

// Записать в лог, сколько в среднем занял каждый проход кадра, и начать счёт заново.
void log_profile();

}  // namespace face
