#pragma once
// Звук: микрофон INMP441 → сервер, сервер → динамик MAX98357A. Два независимых I2S, так что
// слушать и говорить можно одновременно. Протокол — docs/protocol.md, раздел «Звук».
#include <cstddef>
#include <cstdint>

namespace audio {

using SendFn = bool (*)(const uint8_t *data, size_t len);  // отправить кадр микрофона
using DoneFn = void (*)();                                  // звук доигран
using BargeFn = void (*)();                                 // перебили: колонка замолчала сама

void start(SendFn send_mic, DoneFn on_played, BargeFn on_barge_in);

// Перебивание: пока колонка говорит, микрофон слушается локально (в сеть не идёт). Голос заметно
// громче эха собственного ответа дольше ~160 мс — колонка сразу замолкает, шлёт серверу
// on_barge_in и отдаёт ему последние 240 мс микрофона, чтобы начало фразы не потерялось.
// ratio — во сколько раз выше обычного эха должен быть звук (по умолчанию 4).
void set_barge_in(bool on, float ratio = 4.0f);

// Микрофон: кадры по 20 мс (16 кГц, 16 бит) уходят в send_mic, пока listen включён.
void listen(bool on, int gain_db = 12);

// Динамик. play_data зовётся из задачи WebSocket и никогда не ждёт: что не влезло — теряется.
void play_start(int rate);
void play_data(const uint8_t *data, size_t len);
void play_end();
void play_stop();
void set_volume(int level);  // 0…100

}  // namespace audio
