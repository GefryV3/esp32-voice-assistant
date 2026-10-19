#include "net/ota.h"

#include <atomic>
#include <cstdio>
#include <cstring>

#include "audio/audio.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "secrets.h"

namespace ota {
namespace {

const char *TAG = "ota";
constexpr int BOOT_CONFIRM_S = 120;   // столько ждём связи с сервером после обновления

std::atomic<bool> s_busy{false};
std::atomic<bool> s_confirmed{false};
char s_url[256];
ReportFn s_report = nullptr;
esp_timer_handle_t s_guard = nullptr;

bool pending_verify() {
  esp_ota_img_states_t state;
  return esp_ota_get_state_partition(esp_ota_get_running_partition(), &state) == ESP_OK &&
         state == ESP_OTA_IMG_PENDING_VERIFY;
}

// http://хост:порт из SERVER_URI ("ws://192.168.178.86:8765/device") + путь от сервера
bool make_url(const char *path) {
  const char *host = strstr(SERVER_URI, "://");
  if (!host) return false;
  host += 3;
  const char *end = strchr(host, '/');
  const int n = end ? (int)(end - host) : (int)strlen(host);
  return snprintf(s_url, sizeof s_url, "http://%.*s%s", n, host, path) < (int)sizeof s_url;
}

void report(const char *state, int pct, const char *text = "") {
  if (s_report) s_report(state, pct, text);
}

void ota_task(void *) {
  audio::play_stop();
  esp_http_client_config_t http = {};
  http.url = s_url;
  http.timeout_ms = 15000;
  http.buffer_size = 4096;
  http.keep_alive_enable = true;
  esp_https_ota_config_t cfg = {};
  cfg.http_config = &http;

  esp_https_ota_handle_t h = nullptr;
  esp_err_t err = esp_https_ota_begin(&cfg, &h);
  if (err == ESP_OK) {
    const int total = esp_https_ota_get_image_size(h);
    ESP_LOGI(TAG, "качаю %d байт", total);
    int last = -1;
    while ((err = esp_https_ota_perform(h)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
      const int pct = total > 0 ? (int)(100LL * esp_https_ota_get_image_len_read(h) / total) : 0;
      if (pct / 10 != last / 10) {
        last = pct;
        report("progress", pct);
      }
    }
    if (err == ESP_OK && !esp_https_ota_is_complete_data_received(h)) err = ESP_ERR_INVALID_SIZE;
    if (err == ESP_OK) {
      err = esp_https_ota_finish(h);   // проверяет образ и делает его загрузочным
    } else {
      esp_https_ota_abort(h);
    }
  }
  if (err == ESP_OK) {
    ESP_LOGI(TAG, "готово, перезагружаюсь в новую прошивку");
    report("done", 100);
    vTaskDelay(pdMS_TO_TICKS(800));   // дать сообщению уйти
    esp_restart();
  }
  ESP_LOGE(TAG, "не вышло: %s", esp_err_to_name(err));
  report("error", 0, esp_err_to_name(err));
  s_busy = false;
  vTaskDelete(nullptr);
}

void guard_expired(void *) {
  if (s_confirmed) return;
  ESP_LOGE(TAG, "новая прошивка за %d с не связалась с сервером — откатываюсь", BOOT_CONFIRM_S);
  esp_ota_mark_app_invalid_rollback_and_reboot();
}

}  // namespace

void boot_check() {
  if (!pending_verify()) return;
  ESP_LOGW(TAG, "прошивка на испытании: жду связи с сервером %d с", BOOT_CONFIRM_S);
  esp_timer_create_args_t args = {};
  args.callback = guard_expired;
  args.name = "ota_guard";
  if (esp_timer_create(&args, &s_guard) == ESP_OK) esp_timer_start_once(s_guard, BOOT_CONFIRM_S * 1000000ULL);
}

void confirm() {
  if (s_confirmed.exchange(true)) return;
  if (pending_verify()) {
    esp_ota_mark_app_valid_cancel_rollback();
    ESP_LOGI(TAG, "новая прошивка подтверждена");
  }
  if (s_guard) esp_timer_stop(s_guard);
}

bool start(const char *path, ReportFn report_fn) {
  if (s_busy.exchange(true)) return false;
  s_report = report_fn;
  if (!path || !make_url(path)) {
    report("error", 0, "плохой адрес");
    s_busy = false;
    return true;
  }
  ESP_LOGI(TAG, "обновление: %s", s_url);
  if (xTaskCreatePinnedToCore(ota_task, "ota", 8192, nullptr, 4, nullptr, 0) != pdPASS) {
    report("error", 0, "нет памяти под задачу");
    s_busy = false;
  }
  return true;
}

bool busy() { return s_busy; }

}  // namespace ota
