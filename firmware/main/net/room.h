#pragma once
// «Человек в комнате»: BLE-маячок телефона (iBeacon из приложения Home Assistant) и, если подключён,
// радар LD2410C. Результат уходит серверу сообщением room (docs/protocol-next.md, docs/room-presence.md).
// BLE по умолчанию выключен: стек поднимается только по сообщению ble_scan, чтобы не отнимать у Wi-Fi
// ни память, ни эфир, пока это не проверено вживую.

#include <stddef.h>
#include <stdint.h>

namespace room {

void start();                                  // сводная задача (и радар, если включён флагом)
void set_ble(bool on, const char *uuid_hex);   // сообщение ble_scan; uuid — 32 hex или nullptr

// Разбор кадра LD2410: true, если в начале буфера целый кадр (present — есть ли цель).
bool parse_ld2410(const uint8_t *buf, size_t len, bool *present);

// Сглаживание RSSI и гистерезис «рядом/ушёл».
struct Filter {
  float ema = -127;
  bool seen = false;
  bool here = false;
  void update(int rssi);
  void silence();  // маячок не слышно давно
};

}  // namespace room
