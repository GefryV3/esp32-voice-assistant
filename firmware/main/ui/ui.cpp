#include "ui/ui.h"

#include <algorithm>
#include <cstring>
#include <ctime>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

namespace ui {
namespace {

const char *TAG = "ui";

face::Input s_in;  // модель: сеть меняет поля под мьютексом, задача ui берёт снимок на кадр
SemaphoreHandle_t s_lock = nullptr;
volatile uint32_t s_fps = 0;

uint32_t now_ms() { return (uint32_t)(esp_timer_get_time() / 1000); }

struct Lock {
  Lock() { xSemaphoreTake(s_lock, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(s_lock); }
};

// Копия строки с обрезкой по границе символа UTF-8
void copy_text(char *dst, size_t size, const char *src) {
  if (!src) src = "";
  size_t n = strlen(src);
  if (n >= size) {
    n = size - 1;
    while (n > 0 && ((uint8_t)src[n] & 0xC0) == 0x80) n--;
  }
  memcpy(dst, src, n);
  dst[n] = 0;
}

// Пауза между кадрами: живые состояния ~60 к/с, сон — ~30.
constexpr uint32_t FRAME_MS = 16, SLEEP_FRAME_MS = 33;

// Раз в 5 с пишем в лог фактическую частоту и время кадра — по нему видно, где тормозит.
struct Stats {
  uint32_t from = 0, frames = 0, draw_us = 0, wait_us = 0, worst_us = 0;

  void add(const face::Timing &t) {
    frames++;
    draw_us += t.draw_us;
    wait_us += t.wait_us;
    worst_us = std::max(worst_us, t.draw_us + t.wait_us);
  }

  void maybe_log(uint32_t now) {
    if (now - from < 5000) return;
    if (frames) {
      s_fps = frames * 1000 / (now - from);
      ESP_LOGI(TAG, "кадры: %lu к/с, счёт %.1f мс, ожидание второго ядра %.1f мс, худший кадр %.1f мс",
               (unsigned long)s_fps, draw_us / 1000.0f / frames, wait_us / 1000.0f / frames, worst_us / 1000.0f);
      face::log_profile();
    }
    *this = Stats{};
    from = now;
  }
};

void render_task(void *) {
  Stats stats;
  stats.from = now_ms();
  uint32_t last = now_ms();
  bool sleeping = false;
  face::Input in;
  for (;;) {
    vTaskDelay(1);
    const uint32_t now = now_ms();
    if (now - last < (sleeping ? SLEEP_FRAME_MS : FRAME_MS)) continue;
    const float dt = std::min((now - last) / 1000.0f, 0.05f);
    last = now;
    {
      Lock lock;
      in = s_in;
    }
    in.now = now;
    const time_t t = time(nullptr);
    struct tm lt;
    localtime_r(&t, &lt);
    in.have_time = lt.tm_year + 1900 >= 2024;  // время уже пришло от сервера
    in.hour = lt.tm_hour;
    in.minute = lt.tm_min;
    in.wday = lt.tm_wday;
    in.mday = lt.tm_mday;
    in.month = lt.tm_mon;
    const face::Scene &scene = face::animate(in, dt);
    sleeping = scene.sleeping;
    stats.add(face::render(scene));
    stats.maybe_log(now);
  }
}

}  // namespace

void start() {
  s_lock = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(render_task, "ui", 8192, nullptr, 5, nullptr, 1);
  ESP_LOGI(TAG, "отрисовка запущена");
}

void set_voice(face::Voice v) {
  Lock lock;
  if (s_in.voice == v) return;
  s_in.voice = v;
  s_in.activity_at = now_ms();
}

void show_expr(face::Expr e, uint32_t ms) {
  Lock lock;
  s_in.expr = e;
  s_in.expr_at = s_in.activity_at = now_ms();
  s_in.expr_ms = std::clamp<uint32_t>(ms, 300, 60000);
}

void set_weather(int cond, int temp) {
  Lock lock;
  s_in.wcond = std::clamp(cond, -1, 7);
  s_in.wtemp = std::clamp(temp, -60, 60);
}

void set_night(bool on) {
  Lock lock;
  s_in.night = on;
}

void set_brightness(int level) {
  Lock lock;
  s_in.bright = std::clamp(level, 0, 255);
}

void set_online(bool online) {
  Lock lock;
  s_in.online = online;
}

void set_timer(uint32_t sec, const char *label) {
  Lock lock;
  const uint32_t now = now_ms();
  s_in.activity_at = now;
  if (sec == 0) {
    s_in.timer_on = false;
    return;
  }
  s_in.timer_on = true;
  s_in.timer_start = now;
  s_in.timer_ms = std::min<uint32_t>(sec, 24 * 3600) * 1000;
  copy_text(s_in.timer_label, sizeof s_in.timer_label, label);
}

void set_caption(face::Caption kind, const char *text, face::Icon icon) {
  Lock lock;
  s_in.caption = kind;
  s_in.icon = icon;
  copy_text(s_in.caption_text, sizeof s_in.caption_text, text);
}

void set_agenda(const char *accent, const char *text) {
  Lock lock;
  copy_text(s_in.agenda_accent, sizeof s_in.agenda_accent, accent);
  copy_text(s_in.agenda_text, sizeof s_in.agenda_text, text);
}

void set_sleep(uint32_t after_s, bool clock_top) {
  Lock lock;
  s_in.sleep_after_ms = std::clamp<uint32_t>(after_s, 10, 24 * 3600) * 1000;
  s_in.sleep_clock_top = clock_top;
}

void set_card(face::Card kind, const char *app, const char *title, const char *text, int level, uint32_t ms) {
  Lock lock;
  const uint32_t now = now_ms();
  s_in.card = kind;
  s_in.card_at = now;
  s_in.card_ms = std::clamp<uint32_t>(ms, 1000, 120000);
  s_in.card_level = level;
  copy_text(s_in.card_app, sizeof s_in.card_app, app);
  copy_text(s_in.card_title, sizeof s_in.card_title, title);
  copy_text(s_in.card_text, sizeof s_in.card_text, text);
  // ночная колкость не будит — она сама показывается поверх сна
  if (kind != face::Card::Remark) s_in.activity_at = now;
}

void hide_card() {
  Lock lock;
  s_in.card_ms = 0;  // угаснет плавно: содержимое остаётся, пока не исчезнет
}

void set_player(bool on, bool playing, const char *title, const char *artist, int pos_s, int dur_s) {
  Lock lock;
  if (on && !s_in.player_on) s_in.activity_at = now_ms();
  s_in.player_on = on;
  s_in.player_playing = playing;
  s_in.player_at = now_ms();
  s_in.player_pos = std::max(0, pos_s);
  s_in.player_dur = std::max(0, dur_s);
  copy_text(s_in.player_title, sizeof s_in.player_title, title);
  copy_text(s_in.player_artist, sizeof s_in.player_artist, artist);
}

uint32_t fps() { return s_fps; }

}  // namespace ui
