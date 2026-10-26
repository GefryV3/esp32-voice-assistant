#include "net/room.h"

#include <algorithm>
#include <string.h>

#include "audio/audio.h"
#include "board.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/idf_additions.h"
#include "freertos/task.h"
#include "net/ota.h"
#include "net/uplink.h"
#include "net/wifi.h"
#include "sdkconfig.h"
// BLE собирается только с sdkconfig.ble (см. docs/room-presence.md, docs/changes/ble-tuning.md): без него
// стек в образ не входит. Стек урезан до «только наблюдатель», сканирование короткое и редкое.
#define HAVE_BLE CONFIG_BT_NIMBLE_ENABLED
#if HAVE_BLE
#include "host/ble_hs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#endif
#if ROOM_RADAR
#include "driver/uart.h"
#endif

namespace room {
namespace {

const char *TAG = "room";

// ── значения по умолчанию; всё меняется сообщениями ble_scan и config (docs/protocol-next.md) ──
constexpr float NEAR_DBM = -78.0f;  // сглаженный RSSI выше — «рядом»
constexpr float FAR_DBM = -85.0f;   // ниже — «ушёл» (между ними держим прежнее состояние)
constexpr float EMA_K = 0.3f;
constexpr int DEF_WIN_MS = 80;       // окно скана: ~1/3 интервала рекламы (Android «balanced» 250 мс)
constexpr int DEF_EVERY_S = 3;       // период, пока «нет» или не устоялось: ~2,7 % эфира
constexpr int DEF_STABLE_S = 6;      // период при устоявшемся «здесь»: ~1,3 %
constexpr int DEF_NIGHT_S = 15;      // ночью; окно при этом не меньше NIGHT_WIN_MS, чтобы не пропускать
constexpr int NIGHT_WIN_MS = 200;
constexpr int DEF_SILENCE_S = 90;    // маячка не слышно столько — «ушёл» (лёгкие паузы Android не в счёт)
constexpr int SILENCE_PERIODS = 15;  // …и не меньше стольких периодов скана подряд (при редком скане дольше)
constexpr int STABLE_AFTER_S = 60;   // «здесь» держится столько — можно сканировать реже
constexpr int CAL_WIN_MS = 200;      // калибровка: окно и период
constexpr int CAL_EVERY_S = 1;
constexpr int WIFI_POOR_DBM = -80;   // Wi-Fi слабее — скан на паузу, пока не вернётся выше WIFI_OK_DBM
constexpr int WIFI_OK_DBM = -77;
constexpr int64_t ONLINE_HOLD_US = 20LL * 1000000;    // после возврата связи ещё держим паузу
constexpr int64_t PAUSE_UNKNOWN_US = 300LL * 1000000;  // пауза дольше 5 минут — «не знаю» (src none)
constexpr int64_t REPORT_US = 30LL * 1000000;
constexpr int64_t HEAP_LOG_US = 60LL * 1000000;
constexpr int64_t RADAR_HOLD_US = 20LL * 1000000;  // радар: держим «есть» после последней цели
constexpr int TICK_MS = 250;

volatile bool s_ble_on = false;
bool s_nimble_up = false;
bool s_scanning = false;
uint8_t s_uuid[16];
bool s_have_uuid = false;

volatile int s_win_ms = DEF_WIN_MS, s_every_s = DEF_EVERY_S, s_stable_s = DEF_STABLE_S, s_night_s = DEF_NIGHT_S,
             s_silence_s = DEF_SILENCE_S;
volatile float s_near = NEAR_DBM, s_far = FAR_DBM;
volatile bool s_night = false;
volatile int64_t s_cal_until_us = 0;
volatile int64_t s_next_scan_us = 0;

Filter s_filter;
int64_t s_last_seen_us = 0;
int64_t s_here_since_us = 0;
portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
// пакеты за текущий цикл скана (для адаптивной скважности и калибровки)
volatile int s_cycle_heard = 0, s_cycle_raw = 0;
volatile int s_done_heard = 0, s_done_raw = 0;
volatile bool s_cycle_done = false;
volatile bool s_last_cycle_heard = false;
volatile bool s_radar_present = false;
volatile bool s_radar_seen = false;

int hex1(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

bool parse_uuid(const char *hex, uint8_t out[16]) {
  if (!hex || strlen(hex) != 32) return false;
  for (int i = 0; i < 16; i++) {
    int h = hex1(hex[2 * i]), l = hex1(hex[2 * i + 1]);
    if (h < 0 || l < 0) return false;
    out[i] = (uint8_t)(h << 4 | l);
  }
  return true;
}

// iBeacon: AD-структура 0xFF, данные 4C 00 02 15 <uuid 16> <major 2> <minor 2> <tx 1>
bool is_our_beacon(const uint8_t *d, uint8_t len) {
  for (int i = 0; i + 1 < len;) {
    int l = d[i];
    if (l == 0 || i + 1 + l > len) break;
    if (d[i + 1] == 0xFF && l >= 25 && d[i + 2] == 0x4C && d[i + 3] == 0x00 && d[i + 4] == 0x02 &&
        d[i + 5] == 0x15) {
      return memcmp(d + i + 6, s_uuid, 16) == 0;
    }
    i += l + 1;
  }
  return false;
}

size_t free_internal() { return heap_caps_get_free_size(MALLOC_CAP_INTERNAL); }
size_t min_internal() { return heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL); }

#if HAVE_BLE
int on_gap(struct ble_gap_event *ev, void *) {
  if (ev->type == BLE_GAP_EVENT_DISC) {
    if (s_have_uuid && is_our_beacon(ev->disc.data, ev->disc.length_data)) {
      taskENTER_CRITICAL(&s_mux);
      s_filter.update(ev->disc.rssi, s_near, s_far);
      s_last_seen_us = esp_timer_get_time();
      s_cycle_heard = s_cycle_heard + 1;
      s_cycle_raw = ev->disc.rssi;
      taskEXIT_CRITICAL(&s_mux);
    }
  } else if (ev->type == BLE_GAP_EVENT_DISC_COMPLETE) {
    taskENTER_CRITICAL(&s_mux);
    s_scanning = false;
    s_done_heard = s_cycle_heard;
    s_done_raw = s_cycle_raw;
    s_last_cycle_heard = s_cycle_heard > 0;
    s_cycle_done = true;
    taskEXIT_CRITICAL(&s_mux);
  }
  return 0;
}

// Одно короткое пассивное окно: потом контроллер сам останавливается и шлёт DISC_COMPLETE.
void start_scan(int win_ms) {
  if (!s_ble_on || !s_nimble_up || s_scanning || !ble_hs_synced()) return;
  ble_gap_disc_params p = {};
  p.itvl = p.window = (uint16_t)(win_ms * 1000 / 625);  // единицы по 0,625 мс: слушаем всё окно подряд
  p.passive = 1;
  p.filter_duplicates = 1;  // маячок один раз за окно: хосту меньше работы
  s_cycle_heard = 0;
  int rc = ble_gap_disc(BLE_OWN_ADDR_PUBLIC, win_ms, &p, on_gap, nullptr);
  if (rc == 0) {
    s_scanning = true;
  } else {
    ESP_LOGW(TAG, "ble_gap_disc: %d", rc);
  }
}

void host_task(void *) {
  nimble_port_run();
  nimble_port_freertos_deinit();
}

void ble_up() {
  if (s_nimble_up) return;
  size_t before = free_internal();
  int rc = nimble_port_init();
  if (rc != 0) {
    ESP_LOGE(TAG, "nimble_port_init: %d", rc);
    return;
  }
  nimble_port_freertos_init(host_task);
  s_nimble_up = true;
  ESP_LOGI(TAG, "NimBLE поднят, внутренняя RAM %u → %u байт (минимум за всё время %u)", (unsigned)before,
           (unsigned)free_internal(), (unsigned)min_internal());
}

#endif  // HAVE_BLE

#if ROOM_RADAR
void radar_task(void *) {
  uart_config_t cfg = {};
  cfg.baud_rate = 256000;
  cfg.data_bits = UART_DATA_8_BITS;
  cfg.parity = UART_PARITY_DISABLE;
  cfg.stop_bits = UART_STOP_BITS_1;
  cfg.source_clk = UART_SCLK_DEFAULT;
  uart_driver_install(UART_NUM_1, 512, 0, 0, nullptr, 0);
  uart_param_config(UART_NUM_1, &cfg);
  uart_set_pin(UART_NUM_1, board::RADAR_TX, board::RADAR_RX, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
  static const uint8_t head[4] = {0xF4, 0xF3, 0xF2, 0xF1};
  uint8_t buf[128];
  size_t n = 0;
  int64_t last_target_us = 0;
  for (;;) {
    int got = uart_read_bytes(UART_NUM_1, buf + n, sizeof buf - n, pdMS_TO_TICKS(100));
    if (got > 0) n += got;
    for (;;) {
      size_t i = 0;
      while (i + 4 <= n && memcmp(buf + i, head, 4) != 0) i++;
      if (i) {
        memmove(buf, buf + i, n - i);
        n -= i;
      }
      bool present = false;
      if (!parse_ld2410(buf, n, &present)) break;
      size_t frame = 4 + 2 + (buf[4] | buf[5] << 8) + 4;
      memmove(buf, buf + frame, n - frame);
      n -= frame;
      s_radar_seen = true;
      if (present) last_target_us = esp_timer_get_time();
    }
    if (n >= sizeof buf) n = 0;  // мусор без заголовка
    s_radar_present = last_target_us && esp_timer_get_time() - last_target_us < RADAR_HOLD_US;
  }
}
#endif

// Почему скан сейчас на паузе (nullptr — можно сканировать). Скан не должен мешать Wi-Fi там, где ему и так
// тяжело: нет связи или она только что вернулась, слабый сигнал, звук в динамик, запись флеша (OTA).
const char *pause_reason(int64_t now, bool calibrating) {
  static bool prev_net_ok = false;
  static int64_t online_since_us = 0;
  static bool wifi_poor = false;
  bool net_ok = uplink::online() && wifi::connected();
  if (net_ok && !prev_net_ok) online_since_us = now;
  prev_net_ok = net_ok;
  int w = wifi::rssi();
  if (w != 0) {
    if (w < WIFI_POOR_DBM) wifi_poor = true;
    else if (w > WIFI_OK_DBM) wifi_poor = false;
  }
  if (ota::busy()) return "обновление прошивки";
  if (!net_ok) return "нет связи";
  if (now - online_since_us < ONLINE_HOLD_US) return "связь только что вернулась";
  if (calibrating) return nullptr;
  if (wifi_poor) return "слабый Wi-Fi";
  if (audio::playing()) return "звук в динамик";
  return nullptr;
}

void room_task(void *) {
  bool last_here = false;
  const char *last_src = "";
  int64_t last_report = 0, last_tick = esp_timer_get_time(), paused_since = 0, last_heap_log = 0;
  const char *last_pause = nullptr;
  bool logged_after_scan = false;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(TICK_MS));
    int64_t now = esp_timer_get_time();
    int64_t dt = now - last_tick;
    last_tick = now;
    const char *src = "none";
    bool here = false;
    int rssi = 0;
    bool calibrating = now < s_cal_until_us;
    uplink::RoomExtra extra;
#if ROOM_RADAR
    if (s_radar_seen) {
      src = "radar";
      here = s_radar_present;
    }
#endif
    bool unknown = false;  // BLE молчит слишком давно из-за пауз — сигналу верить нельзя
#if HAVE_BLE
    if (s_ble_on && s_have_uuid) {
      const char *pause = pause_reason(now, calibrating);
      if (pause != last_pause) {
        if (pause) ESP_LOGI(TAG, "BLE-скан на паузе: %s", pause);
        else ESP_LOGI(TAG, "BLE-скан продолжается");
        last_pause = pause;
        paused_since = pause ? now : 0;
      }
      if (pause) {
        if (s_scanning) ble_gap_disc_cancel();
        taskENTER_CRITICAL(&s_mux);
        if (s_filter.seen) s_last_seen_us += dt;  // время паузы не считаем молчанием маячка
        taskEXIT_CRITICAL(&s_mux);
        unknown = now - paused_since > PAUSE_UNKNOWN_US;
      } else if (!s_scanning && s_nimble_up && now >= s_next_scan_us) {
        bool stable = s_filter.here && s_last_cycle_heard && now - s_here_since_us >= (int64_t)STABLE_AFTER_S * 1000000;
        int period_s = calibrating ? CAL_EVERY_S : s_night ? s_night_s : stable ? s_stable_s : s_every_s;
        int win = calibrating ? CAL_WIN_MS : s_night ? std::max<int>((int)s_win_ms, NIGHT_WIN_MS) : (int)s_win_ms;
        s_next_scan_us = now + (int64_t)period_s * 1000000;
        start_scan(win);
        if (!logged_after_scan && s_scanning) {
          logged_after_scan = true;
          ESP_LOGI(TAG, "BLE-сканирование запущено (окно %d мс раз в %d с), внутренняя RAM %u, минимум %u", win,
                   period_s, (unsigned)free_internal(), (unsigned)min_internal());
        }
      }
      int period_now = s_night ? s_night_s : s_every_s;
      int64_t silence_us = (int64_t)std::max<int>((int)s_silence_s, SILENCE_PERIODS * period_now) * 1000000;
      taskENTER_CRITICAL(&s_mux);
      if (s_filter.seen && now - s_last_seen_us > silence_us) s_filter.silence();
      bool ble_here = s_filter.here;
      rssi = s_filter.seen ? (int)s_filter.ema : 0;
      taskEXIT_CRITICAL(&s_mux);
      if (ble_here && !last_here) s_here_since_us = now;
      if (!unknown && (strcmp(src, "radar") != 0 || ble_here)) {
        src = "ble";
        here = ble_here;
      }
    }
    if (s_ble_on && now - last_heap_log >= HEAP_LOG_US) {
      last_heap_log = now;
      ESP_LOGI(TAG, "внутренняя RAM: свободно %u, минимум за всё время %u", (unsigned)free_internal(),
               (unsigned)min_internal());
    }
#endif
    bool cycle_done = false;
    if (s_cycle_done) {
      taskENTER_CRITICAL(&s_mux);
      s_cycle_done = false;
      extra.raw = s_done_raw;
      extra.heard = s_done_heard;
      extra.has_raw = calibrating && s_done_heard > 0;
      taskEXIT_CRITICAL(&s_mux);
      cycle_done = calibrating;  // на калибровке сообщаем о каждом цикле, даже без пакета
    }
    if (strcmp(src, "none") == 0 && !s_ble_on && !unknown) continue;  // ничего не включено — молчим
    if (here != last_here || strcmp(src, last_src) != 0 || now - last_report >= REPORT_US || cycle_done) {
      last_here = here;
      last_src = src;
      last_report = now;
      extra.heap_free = (int)free_internal();
      extra.heap_min = (int)min_internal();
      uplink::send_room(here, rssi, src, extra);
    }
  }
}

}  // namespace

void Filter::update(int rssi, float near_dbm, float far_dbm) {
  // медиана из трёх последних замеров гасит одиночный выброс (дверь, тело между телефоном и колонкой)
  if (n < 3) hist[n++] = rssi;
  else {
    hist[0] = hist[1];
    hist[1] = hist[2];
    hist[2] = rssi;
  }
  int a = hist[0], b = n > 1 ? hist[1] : rssi, c = n > 2 ? hist[2] : rssi;
  int med = std::max(std::min(a, b), std::min(std::max(a, b), c));
  if (!seen) {
    ema = (float)rssi;
    seen = true;
    if (ema > near_dbm) here = true;  // первый же замер рядом — сразу «здесь»
    up = down = 0;
    return;
  }
  ema = ema + EMA_K * (med - ema);
  if (ema > near_dbm) {
    down = 0;
    if (++up >= 2) here = true;
  } else if (ema < far_dbm) {
    up = 0;
    if (++down >= 3) here = false;
  } else {
    up = down = 0;
  }
}

void Filter::silence() {
  here = false;
  seen = false;
  n = up = down = 0;
}

bool parse_ld2410(const uint8_t *b, size_t len, bool *present) {
  static const uint8_t head[4] = {0xF4, 0xF3, 0xF2, 0xF1};
  static const uint8_t tail[4] = {0xF8, 0xF7, 0xF6, 0xF5};
  if (len < 10 || memcmp(b, head, 4) != 0) return false;
  size_t dl = b[4] | (b[5] << 8);
  if (dl < 4 || dl > 100 || len < 4 + 2 + dl + 4) return false;
  if (memcmp(b + 6 + dl, tail, 4) != 0) return false;
  // данные: тип (0x02 основной, 0x01 инженерный), 0xAA, состояние цели (0 нет, 1 движется, 2 стоит, 3 оба)
  *present = (b[6] == 0x02 || b[6] == 0x01) && b[7] == 0xAA && b[8] != 0;
  return true;
}

void set_night(bool on) { s_night = on; }

void set_thresholds(int near_dbm, int far_dbm) {
  if (far_dbm >= near_dbm) {
    ESP_LOGW(TAG, "пороги BLE: far %d должен быть ниже near %d, игнорирую", far_dbm, near_dbm);
    return;
  }
  s_near = (float)near_dbm;
  s_far = (float)far_dbm;
  ESP_LOGI(TAG, "пороги BLE: рядом > %d дБм, ушёл < %d дБм", near_dbm, far_dbm);
}

void set_ble(bool on, const char *uuid_hex, const ScanParams &p) {
#if !HAVE_BLE
  if (on) ESP_LOGW(TAG, "ble_scan: эта прошивка собрана без BLE (нужен sdkconfig.ble)");
  (void)uuid_hex;
  (void)p;
  return;
#else
  if (uuid_hex) s_have_uuid = parse_uuid(uuid_hex, s_uuid);
  if (p.win_ms > 0) s_win_ms = std::min(p.win_ms, 500);
  if (p.every_s > 0) s_every_s = p.every_s;
  if (p.stable_every_s > 0) s_stable_s = p.stable_every_s;
  if (p.night_every_s > 0) s_night_s = p.night_every_s;
  if (p.silence_s > 0) s_silence_s = p.silence_s;
  if (on && !s_have_uuid) ESP_LOGW(TAG, "ble_scan без UUID: сканировать нечего, шлю src=none");
  if (on && p.cal_s > 0) {
    s_cal_until_us = esp_timer_get_time() + (int64_t)p.cal_s * 1000000;
    s_next_scan_us = 0;
    ESP_LOGI(TAG, "калибровка BLE на %d с", p.cal_s);
  }
  if (on == s_ble_on) return;
  s_ble_on = on;
  if (on) {
    s_next_scan_us = 0;
    ble_up();
  } else if (s_nimble_up) {
    if (s_scanning) ble_gap_disc_cancel();
    taskENTER_CRITICAL(&s_mux);
    s_filter.silence();
    taskEXIT_CRITICAL(&s_mux);
    s_cal_until_us = 0;
  }
#endif
}

void start() {
  // stacks in PSRAM: internal RAM goes to the echo canceller (these tasks never touch flash)
  xTaskCreatePinnedToCoreWithCaps(room_task, "room", 4096, nullptr, 2, nullptr, 0, MALLOC_CAP_SPIRAM);
#if ROOM_RADAR
  xTaskCreatePinnedToCoreWithCaps(radar_task, "radar", 3072, nullptr, 2, nullptr, 0, MALLOC_CAP_SPIRAM);
#endif
}

}  // namespace room
