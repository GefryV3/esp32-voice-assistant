#include "net/wifi.h"

#include <cstring>

#include "board.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_ota_ops.h"
#include "esp_wifi.h"
#include "secrets.h"

namespace wifi {
namespace {

const char *TAG = "wifi";
volatile bool s_connected = false;

// Прошивка, залитая по воздуху, сначала «на испытании»: если до перезагрузки она не
// подтвердит себя, загрузчик вернёт прежнюю. Подтверждаем, когда есть сеть — значит,
// следующее обновление по воздуху точно дойдёт.
void confirm_firmware() {
  esp_ota_img_states_t state;
  const esp_partition_t *running = esp_ota_get_running_partition();
  if (esp_ota_get_state_partition(running, &state) == ESP_OK && state == ESP_OTA_IMG_PENDING_VERIFY) {
    esp_ota_mark_app_valid_cancel_rollback();
    ESP_LOGI(TAG, "новая прошивка подтверждена");
  }
}

void on_event(void *, esp_event_base_t base, int32_t id, void *data) {
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
    esp_wifi_connect();
  } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    auto *e = static_cast<wifi_event_sta_disconnected_t *>(data);
    if (s_connected) ESP_LOGW(TAG, "связь потеряна (причина %d)", e->reason);
    s_connected = false;
    esp_wifi_connect();  // драйвер сам выдерживает паузу между попытками
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    auto *e = static_cast<ip_event_got_ip_t *>(data);
    ESP_LOGI(TAG, "подключён, IP " IPSTR, IP2STR(&e->ip_info.ip));
    s_connected = true;
    confirm_firmware();
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

  wifi_config_t cfg = {};
  strncpy((char *)cfg.sta.ssid, WIFI_SSID, sizeof cfg.sta.ssid);
  strncpy((char *)cfg.sta.password, WIFI_PASSWORD, sizeof cfg.sta.password);
  cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
  ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
  ESP_ERROR_CHECK(esp_wifi_start());
  // Энергосбережение Wi-Fi даёт задержки до сотен мс — для живого звука не годится.
  esp_wifi_set_ps(WIFI_PS_NONE);
  ESP_LOGI(TAG, "подключаюсь к «%s»", WIFI_SSID);
}

bool connected() { return s_connected; }

int rssi() {
  wifi_ap_record_t ap;
  return (s_connected && esp_wifi_sta_get_ap_info(&ap) == ESP_OK) ? ap.rssi : 0;
}

}  // namespace wifi
