#include "net/wifi.h"

#include <algorithm>
#include <cstring>

#include "board.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "secrets.h"

namespace wifi {
namespace {

const char *TAG = "wifi";
volatile bool s_connected = false;

// Сети с одним паролем: основная и ретранслятор. Основная скрыта (не видна в поиске),
// поэтому выбираем не по списку сетей, а по очереди: 3 неудачи подряд — следующая.
#ifdef WIFI_SSID_2
const char *const SSIDS[] = {WIFI_SSID, WIFI_SSID_2};
#else
const char *const SSIDS[] = {WIFI_SSID};
#endif
constexpr int NETS = sizeof SSIDS / sizeof SSIDS[0];
constexpr uint32_t FAILS_BEFORE_SWITCH = 3;
int s_net = 0;

void use_network(int i) {
  s_net = i;
  wifi_config_t cfg = {};
  memcpy(cfg.sta.ssid, SSIDS[i], std::min(strlen(SSIDS[i]), sizeof cfg.sta.ssid));  // 32 байта без нуля — можно
  strncpy((char *)cfg.sta.password, WIFI_PASSWORD, sizeof cfg.sta.password);
  cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
  cfg.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;      // из нескольких точек с одним именем —
  cfg.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;  // самая громкая
  ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
}

void on_event(void *, esp_event_base_t base, int32_t id, void *data) {
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    esp_wifi_connect();
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    auto *e = static_cast<wifi_event_sta_disconnected_t *>(data);
    static uint32_t fails = 0;
    if (s_connected) {
      ESP_LOGW(TAG, "связь потеряна (причина %d, сигнал %d дБм)", e->reason, e->rssi);
      fails = 0;
    } else {
      ++fails;
      // 201 — сеть не найдена, 2/15 — не дождались ответа точки (слабый сигнал), 15/204 — пароль
      if (fails % 10 == 1) {
        ESP_LOGW(TAG, "«%s»: не подключилась, попытка %lu (причина %d, сигнал %d дБм)", SSIDS[s_net],
                 (unsigned long)fails, e->reason, e->rssi);
      }
      if (NETS > 1 && fails % FAILS_BEFORE_SWITCH == 0) {
        use_network((s_net + 1) % NETS);
        ESP_LOGI(TAG, "пробую «%s»", SSIDS[s_net]);
      }
    }
    s_connected = false;
    esp_wifi_connect();  // драйвер сам выдерживает паузу между попытками
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    auto *e = static_cast<ip_event_got_ip_t *>(data);
    ESP_LOGI(TAG, "подключён к «%s», IP " IPSTR, SSIDS[s_net], IP2STR(&e->ip_info.ip));
    s_connected = true;   // прошивку подтверждает uplink, когда ответит сервер (net/ota)
  }
}

}  // namespace

void start() {
  ESP_ERROR_CHECK(esp_netif_init());
  ESP_ERROR_CHECK(esp_event_loop_create_default());
  esp_netif_t *netif = esp_netif_create_default_wifi_sta();
  esp_netif_set_hostname(netif, board::DEVICE_NAME);

  wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_wifi_init(&init));
  ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, nullptr));
  ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, nullptr));

  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
  use_network(0);
  ESP_ERROR_CHECK(esp_wifi_start());
  // Энергосбережение Wi-Fi даёт задержки до сотен мс — для живого звука не годится.
  esp_wifi_set_ps(WIFI_PS_NONE);
  ESP_LOGI(TAG, "подключаюсь к «%s»%s", SSIDS[0], NETS > 1 ? " (есть запасная сеть)" : "");
}

bool connected() { return s_connected; }

int rssi() {
  wifi_ap_record_t ap;
  return (s_connected && esp_wifi_sta_get_ap_info(&ap) == ESP_OK) ? ap.rssi : 0;
}

}  // namespace wifi
