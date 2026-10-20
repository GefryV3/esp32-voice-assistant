#include "net/plan.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <ctime>

#include "audio/audio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "net/uplink.h"
#include "nvs.h"
#include "ui/ui.h"

namespace plan {
namespace {

const char *TAG = "plan";
constexpr int MAX_ITEMS = 16;
constexpr uint32_t STALE_S = 300;     // позже пяти минут уже не звоним: время сдвинулось, напоминание не про «сейчас»
constexpr int ALARM_SECONDS = 40;

struct Item {
  uint32_t at;          // Unix-время
  char label[32];
  uint8_t timer;        // 1 — таймер, 0 — будильник/напоминание
  uint8_t pad[3];
};

Item s_items[MAX_ITEMS];
int s_count = 0;
SemaphoreHandle_t s_lock = nullptr;
bool s_shown_offline = false;     // метка «нет связи» поставлена нами

struct Lock {
  Lock() { xSemaphoreTake(s_lock, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(s_lock); }
};

void save() {
  nvs_handle_t h;
  if (nvs_open("plan", NVS_READWRITE, &h) != ESP_OK) return;
  nvs_set_blob(h, "items", s_items, sizeof(Item) * s_count);
  nvs_commit(h);
  nvs_close(h);
}

void load() {
  nvs_handle_t h;
  if (nvs_open("plan", NVS_READONLY, &h) != ESP_OK) return;
  size_t size = sizeof s_items;
  if (nvs_get_blob(h, "items", s_items, &size) == ESP_OK) s_count = static_cast<int>(size / sizeof(Item));
  nvs_close(h);
  ESP_LOGI(TAG, "план из памяти: %d сигналов", s_count);
}

void add_all(const cJSON *arr, uint8_t timer, Item *out, int &n) {
  const cJSON *it = nullptr;
  cJSON_ArrayForEach(it, arr) {
    const cJSON *at = cJSON_GetObjectItemCaseSensitive(it, "at");
    const cJSON *label = cJSON_GetObjectItemCaseSensitive(it, "label");
    if (!cJSON_IsNumber(at) || at->valuedouble <= 0 || n >= MAX_ITEMS) continue;
    Item &x = out[n++];
    memset(&x, 0, sizeof x);
    x.at = static_cast<uint32_t>(at->valuedouble);
    x.timer = timer;
    if (cJSON_IsString(label)) strncpy(x.label, label->valuestring, sizeof x.label - 1);
  }
}

// Подпись для экрана и тона: что звучит
void show_fire(const Item &it) {
  char title[64];
  snprintf(title, sizeof title, "%s", it.label[0] ? it.label : (it.timer ? "Таймер" : "Сигнал"));
  ESP_LOGW(TAG, "связи нет, сигнал «%s» играю сам", title);
  audio::alarm(ALARM_SECONDS);
  ui::set_card(face::Card::Wake, "", title, "Нет связи с сервером", -1, ALARM_SECONDS * 1000);
}

void show_offline_label() {
  // ненавязчиво: на нижней дуге «Нет связи» и ближайший сигнал плана, если он есть
  char text[80] = "";
  {
    Lock lock;
    const Item *next = nullptr;
    for (int i = 0; i < s_count; ++i) if (!next || s_items[i].at < next->at) next = &s_items[i];
    if (next) {
      const time_t t = next->at;
      struct tm lt;
      localtime_r(&t, &lt);
      snprintf(text, sizeof text, "· %02d:%02d %s", lt.tm_hour, lt.tm_min, next->label);
    }
  }
  ui::set_agenda("Нет связи", text);
  s_shown_offline = true;
}

void check_task(void *) {
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    const time_t now = time(nullptr);
    if (now < 1700000000) continue;     // время ещё не приходило от сервера: часы показывают 1970
    Item fired[MAX_ITEMS];
    int nf = 0;
    bool changed = false;
    {
      Lock lock;
      for (int i = 0; i < s_count;) {
        if (s_items[i].at > static_cast<uint32_t>(now)) { ++i; continue; }
        // срок наступил: без связи звоним сами (если не слишком давно), со связью — это дело сервера
        if (!uplink::online() && static_cast<uint32_t>(now) - s_items[i].at <= STALE_S && nf < MAX_ITEMS) fired[nf++] = s_items[i];
        memmove(&s_items[i], &s_items[i + 1], sizeof(Item) * (s_count - i - 1));
        --s_count;
        changed = true;
      }
      if (changed) save();
    }
    if (nf) show_fire(fired[0]);      // несколько разом — один тон, показываем первый
    if (changed && !uplink::online()) show_offline_label();
  }
}

}  // namespace

void start() {
  s_lock = xSemaphoreCreateMutex();
  load();
  xTaskCreatePinnedToCore(check_task, "plan", 4096, nullptr, 2, nullptr, 0);
}

void on_plan(const cJSON *m) {
  Item fresh[MAX_ITEMS];
  int n = 0;
  add_all(cJSON_GetObjectItemCaseSensitive(m, "alarms"), 0, fresh, n);
  add_all(cJSON_GetObjectItemCaseSensitive(m, "timers"), 1, fresh, n);
  Lock lock;
  const bool same = n == s_count && memcmp(fresh, s_items, sizeof(Item) * n) == 0;
  if (same) return;
  memcpy(s_items, fresh, sizeof(Item) * n);
  s_count = n;
  save();
  ESP_LOGI(TAG, "план обновлён: %d сигналов", n);
}

void on_link(bool online) {
  if (online) {
    audio::alarm_stop();                  // сервер вернулся: тон не нужен, дальше работает он
    if (s_shown_offline) {
      s_shown_offline = false;
      ui::set_agenda("", "");             // сервер пришлёт свою повестку
    }
  } else if (s_lock) {
    show_offline_label();
  }
}

}  // namespace plan
