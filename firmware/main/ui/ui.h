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

uint32_t fps();  // кадров в секунду за последние ~5 с

}  // namespace ui
