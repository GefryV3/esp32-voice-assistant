#include "net/room.h"

#include <string.h>

#include "board.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net/uplink.h"
#include "sdkconfig.h"
// BLE собирается только с sdkconfig.ble (см. docs/room-presence.md): обычная прошивка стек не несёт,
// потому что он съедает ~22 КБ внутренней RAM (IRAM) даже когда выключен.
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
constexpr float NEAR_DBM = -78.0f;  // сглаженный RSSI выше — «рядом»
constexpr float FAR_DBM = -85.0f;   // ниже — «ушёл» (между ними держим прежнее состояние)
constexpr float EMA_K = 0.3f;
constexpr int64_t SILENCE_US = 30LL * 1000000;  // маячка не слышно 30 с — «ушёл»
constexpr int64_t REPORT_US = 30LL * 1000000;
constexpr int64_t RADAR_HOLD_US = 20LL * 1000000;  // радар: держим «есть» после последней цели

volatile bool s_ble_on = false;
bool s_nimble_up = false;
bool s_scanning = false;
uint8_t s_uuid[16];
bool s_have_uuid = false;

Filter s_filter;
int64_t s_last_seen_us = 0;
portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
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

#if HAVE_BLE
int on_gap(struct ble_gap_event *ev, void *) {
  if (ev->type == BLE_GAP_EVENT_DISC) {
    if (s_have_uuid && is_our_beacon(ev->disc.data, ev->disc.length_data)) {
      taskENTER_CRITICAL(&s_mux);
      s_filter.update(ev->disc.rssi);
      s_last_seen_us = esp_timer_get_time();
      taskEXIT_CRITICAL(&s_mux);
    }
  } else if (ev->type == BLE_GAP_EVENT_DISC_COMPLETE) {
    s_scanning = false;
  }
  return 0;
}

void start_scan() {
  if (!s_ble_on || !s_nimble_up || s_scanning || !ble_hs_synced()) return;
  ble_gap_disc_params p = {};
  p.itvl = 1600;   // 1,0 с (единицы по 0,625 мс)
  p.window = 160;  // слушаем 100 мс: около 10 % эфира, остальное Wi-Fi
  p.passive = 1;
  p.filter_duplicates = 0;
  int rc = ble_gap_disc(BLE_OWN_ADDR_PUBLIC, BLE_HS_FOREVER, &p, on_gap, nullptr);
  if (rc == 0) {
    s_scanning = true;
    ESP_LOGI(TAG, "BLE-сканирование запущено");
  } else {
    ESP_LOGW(TAG, "ble_gap_disc: %d", rc);
  }
}

void on_sync() { start_scan(); }

void host_task(void *) {
  nimble_port_run();
  nimble_port_freertos_deinit();
}

void ble_up() {
  if (s_nimble_up) return;
  size_t before = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  int rc = nimble_port_init();
  if (rc != 0) {
    ESP_LOGE(TAG, "nimble_port_init: %d", rc);
    return;
  }
  ble_hs_cfg.sync_cb = on_sync;
  nimble_port_freertos_init(host_task);
  s_nimble_up = true;
  ESP_LOGI(TAG, "NimBLE поднят, внутренняя RAM %u → %u байт", (unsigned)before,
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
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

void room_task(void *) {
  bool last_here = false;
  const char *last_src = "";
  int64_t last_report = 0;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    const char *src = "none";
    bool here = false;
    int rssi = 0;
#if ROOM_RADAR
    if (s_radar_seen) {
      src = "radar";
      here = s_radar_present;
    }
#endif
    if (s_ble_on && s_have_uuid) {
      taskENTER_CRITICAL(&s_mux);
      if (s_filter.seen && esp_timer_get_time() - s_last_seen_us > SILENCE_US) s_filter.silence();
      bool ble_here = s_filter.here;
      rssi = s_filter.seen ? (int)s_filter.ema : 0;
      taskEXIT_CRITICAL(&s_mux);
      if (strcmp(src, "radar") != 0 || ble_here) {
        src = "ble";
        here = ble_here;
      }
    }
    if (strcmp(src, "none") == 0 && !s_ble_on) continue;  // ничего не включено — молчим
    int64_t now = esp_timer_get_time();
    if (here != last_here || strcmp(src, last_src) != 0 || now - last_report >= REPORT_US) {
      last_here = here;
      last_src = src;
      last_report = now;
      uplink::send_room(here, rssi, src);
    }
  }
}

}  // namespace

void Filter::update(int rssi) {
  ema = seen ? ema + EMA_K * (rssi - ema) : (float)rssi;
  seen = true;
  if (ema > NEAR_DBM) here = true;
  else if (ema < FAR_DBM) here = false;
}

void Filter::silence() {
  here = false;
  seen = false;
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

void set_ble(bool on, const char *uuid_hex) {
#if !HAVE_BLE
  if (on) ESP_LOGW(TAG, "ble_scan: эта прошивка собрана без BLE (нужен sdkconfig.ble)");
  (void)uuid_hex;
  return;
#else
  if (uuid_hex) s_have_uuid = parse_uuid(uuid_hex, s_uuid);
  if (on && !s_have_uuid) ESP_LOGW(TAG, "ble_scan без UUID: сканировать нечего, шлю src=none");
  if (on == s_ble_on) return;
  s_ble_on = on;
  if (on) {
    ble_up();
    start_scan();
  } else if (s_nimble_up && s_scanning) {
    ble_gap_disc_cancel();
    s_scanning = false;
    taskENTER_CRITICAL(&s_mux);
    s_filter.silence();
    taskEXIT_CRITICAL(&s_mux);
  }
#endif
}

void start() {
  xTaskCreatePinnedToCore(room_task, "room", 3072, nullptr, 2, nullptr, 0);
#if ROOM_RADAR
  xTaskCreatePinnedToCore(radar_task, "radar", 3072, nullptr, 2, nullptr, 0);
#endif
}

}  // namespace room
