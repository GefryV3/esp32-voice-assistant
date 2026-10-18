#pragma once
// Связь с сервером по WebSocket: рукопожатие, команды экрану, статус. Протокол — docs/protocol.md.

namespace uplink {

void start();
bool online();  // подключены и получили welcome

}  // namespace uplink
