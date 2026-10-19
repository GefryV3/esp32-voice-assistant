#pragma once
// Обновление прошивки по воздуху: сервер присылает {"type":"ota","url":"/firmware.bin?…"},
// колонка качает образ с того же сервера по HTTP во второй слот и перезагружается.
// Новая прошивка «на испытании»: если за BOOT_CONFIRM_S после загрузки она не связалась с
// сервером (confirm()), загрузчик вернёт прежнюю. Протокол — docs/protocol.md, «Обновление».

namespace ota {

// Отчёт о ходе: state — "progress" (pct 0…100), "done" или "error" (text — причина).
using ReportFn = void (*)(const char *state, int pct, const char *text);

void boot_check();                         // в app_main: следить, подтвердит ли себя новая прошивка
void confirm();                            // связались с сервером — прошивка рабочая
bool start(const char *path, ReportFn report);   // false — уже идёт
bool busy();

}  // namespace ota
