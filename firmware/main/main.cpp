// Jarvis — прошивка колонки. Порядок запуска: экран (сразу видно, что колонка жива) →
// Wi-Fi → связь с сервером. Всё дальнейшее происходит в задачах ui, wifi и uplink.
#include "display/lcd.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_system.h"
#include "face/face.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net/ota.h"
#include "net/uplink.h"
#include "net/wifi.h"
#include "nvs_flash.h"
#include "ui/ui.h"

static const char *TAG = "main";

static void init_nvs() {
  esp_err_t err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    // NVS от прошлой прошивки (ESPHome) в другом формате — начинаем с чистого
    ESP_ERROR_CHECK(nvs_flash_erase());
    err = nvs_flash_init();
  }
  ESP_ERROR_CHECK(err);
}

extern "C" void app_main() {
  ESP_LOGI(TAG, "Jarvis %s, причина перезапуска %d", esp_app_get_description()->version, (int)esp_reset_reason());
  init_nvs();
  ota::boot_check();  // после обновления по воздуху — откат, если не свяжемся с сервером

  lcd::init();
  if (face::init()) {
    face::splash();
    vTaskDelay(pdMS_TO_TICKS(1500));
  }
  ui::start();

  wifi::start();
  uplink::start();
}
