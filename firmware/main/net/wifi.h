#pragma once
// Wi-Fi в режиме клиента: подключение, переподключение, подтверждение обновления.

namespace wifi {

void start();
bool connected();
int rssi();  // дБм, 0 — нет связи

}  // namespace wifi
