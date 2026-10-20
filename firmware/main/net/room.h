#pragma once
// «Человек в комнате»: BLE-маячок телефона (iBeacon из приложения Home Assistant) и, если подключён,
// радар LD2410C. Результат уходит серверу сообщением room (docs/protocol-next.md, docs/room-presence.md).
// BLE по умолчанию выключен: стек поднимается только по сообщению ble_scan, чтобы не отнимать у Wi-Fi
// ни память, ни эфир, пока это не проверено вживую.

#include <stddef.h>
#include <stdint.h>

namespace room {

// Параметры скана из сообщения ble_scan; -1 — не прислано, остаётся прежнее (или значение по умолчанию).
struct ScanParams {
  int win_ms = -1;         // окно скана
  int every_s = -1;        // период, пока человека «нет» или состояние не устоялось
  int stable_every_s = -1; // период при устоявшемся «здесь»
  int night_every_s = -1;  // период ночью
  int silence_s = -1;      // сколько не слышно маячка до «ушёл» (минимум; при редком скане растёт)
  int cal_s = -1;          // калибровка: столько секунд сканировать часто и слать room на каждый цикл
};

void start();                                  // сводная задача (и радар, если включён флагом)
void set_ble(bool on, const char *uuid_hex, const ScanParams &p = ScanParams());  // сообщение ble_scan
void set_night(bool on);                       // ночью скан реже
void set_thresholds(int near_dbm, int far_dbm);  // config: ble_near / ble_far (дБм, far < near)

// Разбор кадра LD2410: true, если в начале буфера целый кадр (present — есть ли цель).
bool parse_ld2410(const uint8_t *buf, size_t len, bool *present);

// Сглаживание RSSI и гистерезис «рядом/ушёл». Один выброс вниз/вверх отсекается медианой из трёх
// замеров, «рядом» нужно два замера подряд выше порога, «ушёл» — три подряд ниже.
struct Filter {
  float ema = -127;
  bool seen = false;
  bool here = false;
  int hist[3] = {0, 0, 0};
  uint8_t n = 0;
  uint8_t up = 0, down = 0;
  void update(int rssi, float near_dbm, float far_dbm);
  void silence();  // маячок не слышно давно
};

}  // namespace room
