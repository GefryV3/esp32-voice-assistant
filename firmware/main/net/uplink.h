#pragma once
// Связь с сервером по WebSocket: рукопожатие, команды экрану, статус. Протокол — docs/protocol.md.

namespace uplink {

void start();
// Необязательные поля room: память (диагностика BLE) и сырой замер для калибровки.
struct RoomExtra {
  int heap_free = -1, heap_min = -1;  // внутренняя RAM, байт; -1 — не слать
  bool has_raw = false;               // маячок слышен в этом цикле скана (только в режиме калибровки)
  int raw = 0, heard = 0;
};
void send_room(bool here, int rssi, const char *src, const RoomExtra &extra = RoomExtra());  // сообщение room серверу
bool online();  // подключены и получили welcome

}  // namespace uplink
