#pragma once
// Связь с сервером по WebSocket: рукопожатие, команды экрану, статус. Протокол — docs/protocol.md.

namespace uplink {

void start();
void send_room(bool here, int rssi, const char *src);  // сообщение room серверу
bool online();  // подключены и получили welcome

}  // namespace uplink
