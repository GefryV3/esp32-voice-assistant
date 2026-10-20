#include "net/uplink.h"

#include <sys/time.h>

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include "audio/audio.h"
#include "board.h"
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net/ota.h"
#include "net/plan.h"
#include "net/room.h"
#include "net/wifi.h"
#include "secrets.h"
#include "ui/ui.h"

namespace uplink {
namespace {

const char *TAG = "uplink";
constexpr int PROTO_VERSION = 1;
constexpr int STATUS_PERIOD_MS = 30000;
constexpr size_t MAX_TEXT = 4096;

esp_websocket_client_handle_t s_client = nullptr;
TaskHandle_t s_status_task = nullptr;
volatile bool s_online = false;
std::atomic<int> s_clap_pending{0};   // хлопки, ждущие отправки (ставит задача микрофона)

char s_rx[MAX_TEXT + 1];
size_t s_rx_len = 0;

// ── мелкие помощники для cJSON ──

const char *str(const cJSON *o, const char *key) {
  const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
  return cJSON_IsString(v) ? v->valuestring : nullptr;
}

int num(const cJSON *o, const char *key, int def) {
  const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
  return cJSON_IsNumber(v) ? (int)v->valuedouble : def;
}

bool eq(const char *a, const char *b) { return a && strcmp(a, b) == 0; }

void send_json(cJSON *msg) {
  char *text = cJSON_PrintUnformatted(msg);
  if (text && s_client && esp_websocket_client_is_connected(s_client)) {
    esp_websocket_client_send_text(s_client, text, strlen(text), pdMS_TO_TICKS(1000));
  }
  cJSON_free(text);
  cJSON_Delete(msg);
}

void send_hello() {
  cJSON *m = cJSON_CreateObject();
  cJSON_AddStringToObject(m, "type", "hello");
  cJSON_AddNumberToObject(m, "proto", PROTO_VERSION);
  cJSON_AddStringToObject(m, "token", DEVICE_TOKEN);
  cJSON_AddStringToObject(m, "device", board::DEVICE_NAME);
  cJSON_AddStringToObject(m, "fw", esp_app_get_description()->version);
  cJSON *caps = cJSON_AddArrayToObject(m, "caps");
  cJSON_AddItemToArray(caps, cJSON_CreateString("display"));
  cJSON_AddItemToArray(caps, cJSON_CreateString("mic"));
  cJSON_AddItemToArray(caps, cJSON_CreateString("speaker"));
  send_json(m);
}

// ── звук: колбэки для audio, зовутся из его задач ──

bool send_mic(const uint8_t *data, size_t len) {
  if (!s_online || !esp_websocket_client_is_connected(s_client)) return false;
  // Ждём до 1.5 с: на слабом Wi-Fi (−75 дБм и хуже) окно TCP иногда стоит дольше 200 мс, а
  // таймаут записи клиент WebSocket считает обрывом и закрывает связь (она тогда рвалась раз
  // в ~5 с). Отправка идёт в своей задаче, кадры пока ждут в буфере микрофона.
  return esp_websocket_client_send_bin(s_client, reinterpret_cast<const char *>(data), len, pdMS_TO_TICKS(1500)) >= 0;
}

void send_played() {
  cJSON *m = cJSON_CreateObject();
  cJSON_AddStringToObject(m, "type", "played");
  send_json(m);
}

void send_barge_in() {
  cJSON *m = cJSON_CreateObject();
  cJSON_AddStringToObject(m, "type", "barge_in");
  send_json(m);
}

// ход обновления: серверу — сообщением, на экран — подписью
void report_ota(const char *state, int pct, const char *text) {
  cJSON *m = cJSON_CreateObject();
  cJSON_AddStringToObject(m, "type", "ota_state");
  cJSON_AddStringToObject(m, "state", state);
  cJSON_AddNumberToObject(m, "pct", pct);
  if (text && *text) cJSON_AddStringToObject(m, "error", text);
  send_json(m);
  char line[96];   // кириллица — по 2 байта на букву
  if (eq(state, "progress")) snprintf(line, sizeof line, "Обновляюсь… %d %%", pct);
  else if (eq(state, "done")) snprintf(line, sizeof line, "Обновился, перезагружаюсь");
  else snprintf(line, sizeof line, "Обновиться не вышло");
  ui::set_caption(face::Caption::Reply, line, face::Icon::None);
}

void send_clap(int count) {
  cJSON *m = cJSON_CreateObject();
  cJSON_AddStringToObject(m, "type", "clap");
  cJSON_AddNumberToObject(m, "count", count);
  send_json(m);
}

// из задачи микрофона: не ждём сеть, отправит status_task
void on_clap(int count) {
  s_clap_pending = count;
  if (s_status_task) xTaskNotifyGive(s_status_task);
}

void send_room_msg(bool here, int rssi, const char *src, const RoomExtra &x) {
  cJSON *m = cJSON_CreateObject();
  cJSON_AddStringToObject(m, "type", "room");
  cJSON_AddBoolToObject(m, "here", here);
  if (rssi != 0) cJSON_AddNumberToObject(m, "rssi", rssi);
  cJSON_AddStringToObject(m, "src", src);
  if (x.heap_free >= 0) cJSON_AddNumberToObject(m, "heap_free", x.heap_free);
  if (x.heap_min >= 0) cJSON_AddNumberToObject(m, "heap_min", x.heap_min);
  if (x.has_raw) {
    cJSON_AddNumberToObject(m, "raw", x.raw);
    cJSON_AddNumberToObject(m, "heard", x.heard);
  }
  send_json(m);
}

void send_status() {
  cJSON *m = cJSON_CreateObject();
  cJSON_AddStringToObject(m, "type", "status");
  cJSON_AddNumberToObject(m, "uptime", (double)(esp_timer_get_time() / 1000000));
  cJSON_AddNumberToObject(m, "heap", heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
  cJSON_AddNumberToObject(m, "psram", heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
  cJSON_AddNumberToObject(m, "rssi", wifi::rssi());
  cJSON_AddNumberToObject(m, "fps", ui::fps());
  send_json(m);
}

void on_welcome(const cJSON *m) {
  long t = (long)num(m, "time", 0);
  if (t > 0) {
    struct timeval tv = {};
    tv.tv_sec = t;
    settimeofday(&tv, nullptr);
  }
  if (const char *tz = str(m, "tz")) {
    setenv("TZ", tz, 1);
    tzset();
  }
  s_online = true;
  ui::set_online(true);
  plan::on_link(true);
  ota::confirm();   // сервер ответил — новая прошивка рабочая, откат не нужен
  time_t now = time(nullptr);
  struct tm lt;
  localtime_r(&now, &lt);
  char clock[24];
  strftime(clock, sizeof clock, "%d.%m %H:%M %Z", &lt);
  ESP_LOGI(TAG, "сервер %s принял, местное время %s", str(m, "server") ? str(m, "server") : "?", clock);
  xTaskNotifyGive(s_status_task);  // первый статус — сразу
}

// Выражения характера. alarm и playful — имена из протокола до нового дизайна.
face::Expr expr_by_name(const char *n) {
  if (eq(n, "happy") || eq(n, "playful")) return face::Expr::Happy;
  if (eq(n, "skeptic")) return face::Expr::Skeptic;
  if (eq(n, "roll")) return face::Expr::Roll;
  if (eq(n, "side")) return face::Expr::Side;
  if (eq(n, "surprise") || eq(n, "alarm")) return face::Expr::Surprise;
  if (eq(n, "sorry")) return face::Expr::Sorry;
  if (eq(n, "tired")) return face::Expr::Tired;
  if (eq(n, "bored")) return face::Expr::Bored;
  return face::Expr::None;
}

void on_caption(const cJSON *m) {
  const char *k = str(m, "kind");
  const face::Caption kind = eq(k, "heard")   ? face::Caption::Heard
                             : eq(k, "said")  ? face::Caption::Said
                             : eq(k, "reply") ? face::Caption::Reply
                                              : face::Caption::None;
  const face::Icon icon = eq(str(m, "icon"), "light") ? face::Icon::Light : face::Icon::None;
  ui::set_caption(kind, str(m, "text"), icon);
}

void on_notify(const cJSON *m) {
  const char *k = str(m, "kind");
  const face::Card kind = eq(k, "call")      ? face::Card::Call
                          : eq(k, "battery") ? face::Card::Battery
                          : eq(k, "info")    ? face::Card::Info
                          : eq(k, "remark")  ? face::Card::Remark
                          : eq(k, "wake")    ? face::Card::Wake
                          : eq(k, "check")   ? face::Card::Check
                                             : face::Card::Msg;
  ui::set_card(kind, str(m, "app"), str(m, "title"), str(m, "text"), num(m, "level", -1),
               (uint32_t)num(m, "ms", 8000));
}

void on_player(const cJSON *m) {
  const char *state = str(m, "state");
  const bool on = eq(state, "playing") || eq(state, "paused");
  ui::set_player(on, eq(state, "playing"), str(m, "title"), str(m, "artist"), num(m, "pos", 0), num(m, "dur", 0));
}

void on_task_status(const cJSON *m) {
  const char *s = str(m, "state");
  const face::Task t = eq(s, "working")            ? face::Task::Working
                       : eq(s, "needs_input")      ? face::Task::NeedsInput
                       : eq(s, "needs_permission") ? face::Task::NeedsPermission
                       : eq(s, "done")             ? face::Task::Done
                       : eq(s, "failed")           ? face::Task::Failed
                                                   : face::Task::Idle;
  ui::set_task(t, str(m, "label"), num(m, "pct", -1));
}

// Игры: время показа берём из таблицы протокола, если сервер не прислал своё
void on_game(const cJSON *m) {
  const char *n = str(m, "name");
  if (eq(n, "coin")) ui::set_game(face::Game::Coin, num(m, "result", 0), 0, nullptr, 3200);
  else if (eq(n, "dice")) ui::set_game(face::Game::Dice, num(m, "result", 1), 0, nullptr, 2400);
  else if (eq(n, "wheel")) ui::set_game(face::Game::Wheel, num(m, "count", 2), num(m, "result", 0), nullptr, 3400);
  else if (eq(n, "rps")) ui::set_game(face::Game::Rps, num(m, "mine", 0), num(m, "result", 0), nullptr, 3600);
  else if (eq(n, "magic8")) ui::set_game(face::Game::Magic8, num(m, "answer", 6), 0, nullptr, 3600);
  else if (eq(n, "ttt")) ui::set_game(face::Game::Ttt, 0, 0, str(m, "board"), 45000);
  else ESP_LOGW(TAG, "неизвестная игра %s", n ? n : "?");
}

// Применяем только присланные поля: сервер шлёт и настройки сна, и хлопков отдельными сообщениями.
void on_config(const cJSON *m) {
  if (cJSON_GetObjectItemCaseSensitive(m, "sleep_after") || cJSON_GetObjectItemCaseSensitive(m, "sleep_clock")) {
    const char *clock = str(m, "sleep_clock");
    ui::set_sleep((uint32_t)num(m, "sleep_after", 180), !eq(clock, "bottom"));
  }
  if (cJSON_GetObjectItemCaseSensitive(m, "clap") || cJSON_GetObjectItemCaseSensitive(m, "clap_sens")) {
    static int sens = 5;
    static bool on = true;
    sens = num(m, "clap_sens", sens);
    on = num(m, "clap", on ? 1 : 0) != 0;
    audio::set_clap(on, sens);
  }
  if (cJSON_GetObjectItemCaseSensitive(m, "ble_near") && cJSON_GetObjectItemCaseSensitive(m, "ble_far")) {
    room::set_thresholds(num(m, "ble_near", -78), num(m, "ble_far", -85));
  }
}

void on_message(const cJSON *m) {
  const char *type = str(m, "type");
  if (!eq(type, "ping")) ESP_LOGI(TAG, "← %.*s", (int)s_rx_len, s_rx);
  if (eq(type, "welcome")) {
    on_welcome(m);
  } else if (eq(type, "voice")) {
    const char *s = str(m, "state");
    ui::set_voice(eq(s, "listening")  ? face::Voice::Listening
                  : eq(s, "thinking") ? face::Voice::Thinking
                  : eq(s, "speaking") ? face::Voice::Speaking
                                      : face::Voice::Idle);
  } else if (eq(type, "emotion")) {
    const face::Expr e = expr_by_name(str(m, "name"));
    if (e != face::Expr::None) ui::show_expr(e, (uint32_t)num(m, "ms", 2600));
    else ESP_LOGW(TAG, "неизвестное выражение %s", str(m, "name") ? str(m, "name") : "?");
  } else if (eq(type, "caption")) {
    on_caption(m);
  } else if (eq(type, "timer")) {
    ui::set_timer((uint32_t)std::max(0, num(m, "sec", 0)), str(m, "label"));
  } else if (eq(type, "agenda")) {
    ui::set_agenda(str(m, "accent"), str(m, "text"));
  } else if (eq(type, "config")) {
    on_config(m);
  } else if (eq(type, "plan")) {
    plan::on_plan(m);
  } else if (eq(type, "notify")) {
    on_notify(m);
  } else if (eq(type, "notify_hide")) {
    ui::hide_card();
  } else if (eq(type, "player")) {
    on_player(m);
  } else if (eq(type, "game")) {
    on_game(m);
  } else if (eq(type, "reminders")) {
    ui::set_reminders(num(m, "count", 0));
  } else if (eq(type, "task_status")) {
    on_task_status(m);
  } else if (eq(type, "weather")) {
    ui::set_weather(num(m, "cond", -1), num(m, "temp", 0));
  } else if (eq(type, "night")) {
    const bool night_on = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(m, "on"));
    ui::set_night(night_on);
    room::set_night(night_on);  // ночью BLE-скан реже
  } else if (eq(type, "brightness")) {
    ui::set_brightness(num(m, "level", 255));
  } else if (eq(type, "quiet")) {
    ui::set_quiet(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(m, "on")));
  } else if (eq(type, "listen_start")) {
    ui::set_quiet(false);
    audio::listen(true, num(m, "gain", 12));
  } else if (eq(type, "listen_stop")) {
    audio::listen(false);
  } else if (eq(type, "audio_start")) {
    audio::play_start(num(m, "rate", 16000));
  } else if (eq(type, "audio_end")) {
    audio::play_end();
  } else if (eq(type, "audio_stop")) {
    audio::play_stop();
  } else if (eq(type, "volume")) {
    audio::set_volume(num(m, "level", 70));
  } else if (eq(type, "barge")) {
    audio::set_barge_in(num(m, "on", 0) != 0, num(m, "ratio_x10", 40) / 10.0f);
  } else if (eq(type, "ble_scan")) {
    room::ScanParams p;  // не присланное остаётся -1 и берётся из значений по умолчанию room.cpp
    p.win_ms = num(m, "win_ms", -1);
    p.every_s = num(m, "every_s", -1);
    p.stable_every_s = num(m, "stable_every_s", -1);
    p.night_every_s = num(m, "night_every_s", -1);
    p.silence_s = num(m, "silence_s", -1);
    p.cal_s = num(m, "cal_s", -1);
    room::set_ble(num(m, "on", 0) != 0, str(m, "uuid"), p);
  } else if (eq(type, "ota")) {
    if (!ota::start(str(m, "url"), report_ota)) ESP_LOGW(TAG, "обновление уже идёт");
  } else if (eq(type, "ping")) {
    cJSON *pong = cJSON_CreateObject();
    cJSON_AddStringToObject(pong, "type", "pong");
    cJSON_AddNumberToObject(pong, "id", num(m, "id", 0));
    send_json(pong);
  } else {
    ESP_LOGD(TAG, "пропускаю неизвестное сообщение %s", type ? type : "?");
  }
}

void on_text_chunk(const esp_websocket_event_data_t *d) {
  if (d->payload_offset == 0) s_rx_len = 0;
  if (d->payload_len > (int)MAX_TEXT) {
    ESP_LOGW(TAG, "слишком длинное сообщение (%d байт), пропускаю", d->payload_len);
    return;
  }
  memcpy(s_rx + s_rx_len, d->data_ptr, d->data_len);
  s_rx_len += d->data_len;
  if (d->payload_offset + d->data_len < d->payload_len) return;  // ждём остаток

  cJSON *m = cJSON_ParseWithLength(s_rx, s_rx_len);
  if (m) {
    on_message(m);
    cJSON_Delete(m);
  } else {
    ESP_LOGW(TAG, "битый JSON от сервера");
  }
}

void go_offline() {
  if (s_online) ESP_LOGW(TAG, "связь с сервером потеряна");
  s_online = false;
  ui::set_online(false);
  audio::listen(false);
  audio::play_stop();
  plan::on_link(false);
}

void on_ws_event(void *, esp_event_base_t, int32_t id, void *data) {
  auto *d = static_cast<esp_websocket_event_data_t *>(data);
  switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
      ESP_LOGI(TAG, "соединение с %s, отправляю hello", SERVER_URI);
      send_hello();
      break;
    case WEBSOCKET_EVENT_DATA:
      if (d->op_code == 0x2) {
        audio::play_data(reinterpret_cast<const uint8_t *>(d->data_ptr), d->data_len);
      } else if (d->op_code == 0x1 || (d->op_code == 0x0 && d->payload_offset > 0)) {
        on_text_chunk(d);
      } else if (d->op_code == 0x8 && d->data_len >= 2) {
        int code = ((uint8_t)d->data_ptr[0] << 8) | (uint8_t)d->data_ptr[1];
        if (code == 4001) ESP_LOGE(TAG, "сервер отклонил токен — проверь DEVICE_TOKEN в secrets.h");
        else ESP_LOGW(TAG, "сервер закрыл соединение, код %d", code);
      }
      break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_CLOSED:
    case WEBSOCKET_EVENT_ERROR:
      go_offline();
      break;
    default:
      break;
  }
}

void status_task(void *) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(STATUS_PERIOD_MS));
    if (const int claps = s_clap_pending.exchange(0)) {
      if (s_online) send_clap(claps);
      continue;
    }
    if (s_online) send_status();
  }
}

}  // namespace

void send_room(bool here, int rssi, const char *src, const RoomExtra &extra) {
  if (s_online) send_room_msg(here, rssi, src, extra);
}

void start() {
  room::start();
  audio::start(send_mic, send_played, send_barge_in);
  audio::set_clap_handler(on_clap);
  plan::start();
  xTaskCreatePinnedToCore(status_task, "link_status", 3072, nullptr, 3, &s_status_task, 0);

  esp_websocket_client_config_t cfg = {};
  cfg.uri = SERVER_URI;
  cfg.reconnect_timeout_ms = 3000;
  cfg.network_timeout_ms = 10000;
  cfg.buffer_size = 2048;
  cfg.task_stack = 6144;
  cfg.ping_interval_sec = 10;
  cfg.pingpong_timeout_sec = 30;
  s_client = esp_websocket_client_init(&cfg);
  esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY, on_ws_event, nullptr);
  esp_websocket_client_start(s_client);
}

bool online() { return s_online; }

}  // namespace uplink
