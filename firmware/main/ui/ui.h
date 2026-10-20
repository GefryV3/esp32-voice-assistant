#pragma once
// Состояние экрана и задача отрисовки. Сеть только меняет «что показывать» через эти
// функции (из любой задачи, под мьютексом), рисует всегда одна задача ui.
#include <cstdint>

#include "face/face.h"

namespace ui {

void start();

void set_voice(face::Voice v);
void show_expr(face::Expr e, uint32_t ms);
void set_weather(int cond, int temp);
void set_night(bool on);
void set_brightness(int level);
void set_online(bool online);
void set_timer(uint32_t sec, const char *label);  // 0 — снять таймер
void set_caption(face::Caption kind, const char *text, face::Icon icon);
void set_agenda(const char *accent, const char *text);  // пустые — убрать
void set_sleep(uint32_t after_s, bool clock_top);       // через сколько засыпать и где часы во сне
// Карточка с телефона (level — заряд для батареи, −1 — нет) и мини-плеер.
void set_card(face::Card kind, const char *app, const char *title, const char *text, int level, uint32_t ms);
void hide_card();
void set_player(bool on, bool playing, const char *title, const char *artist, int pos_s, int dur_s);

// Ход длинной задачи (task_status): кольцо по краю и подпись. pct −1 — процент неизвестен.
void set_task(face::Task state, const char *label, int pct);
// Игра на экране: p0, p1 — параметры по виду (монетка: p0 результат; кубик: p0; колесо: p0 секторов,
// p1 выпало; камень-ножницы-бумага: p0 мой, p1 исход; шар: p0 ответ; крестики: board 9 символов).
void set_game(face::Game g, int p0, int p1, const char *board, uint32_t ms);
void set_reminders(int count);

uint32_t fps();  // кадров в секунду за последние ~5 с

}  // namespace ui
