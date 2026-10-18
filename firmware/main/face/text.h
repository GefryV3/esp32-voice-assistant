#pragma once
// Шрифты: Inter, заранее нарезанный в сглаженные 8-битные маски
// (tools/make_fonts.py → fonts_data.cpp). Здесь — поиск символов и ширина строк.
#include <cstdint>

namespace text {

struct Glyph {
  uint32_t cp;   // код символа (Unicode)
  uint32_t off;  // начало маски в Font::bits
  uint8_t w, h;  // размер маски
  int8_t x, y;   // левый верхний угол маски от точки на базовой линии
  uint16_t adv;  // шаг до следующего символа, 1/16 px
};

struct Font {
  const Glyph *glyphs;  // по возрастанию cp
  uint16_t count;
  const uint8_t *bits;
  uint8_t size;  // кегль, px
  uint8_t cap;   // высота цифр над базовой линией, px
};

extern const Font kClock;  // цифры часов: Inter Light 40
extern const Font kTimer;  // цифры таймера: Inter Light 50
extern const Font kUi;      // подписи: Inter Medium 15
extern const Font kUiBold;  // заголовки карточек: Inter SemiBold 15
extern const Font kTitle;   // кто звонит: Inter SemiBold 24
extern const Font kCaps;    // надписи по дуге и капслоком: Inter SemiBold 12

const Glyph *find(const Font &f, uint32_t cp);

// Следующий символ строки UTF-8, s сдвигается. 0 — конец строки.
uint32_t next(const char *&s);

// Ширина строки, px; tracking — добавка между символами.
float width(const Font &f, const char *s, float tracking = 0);

// Ширина ячейки цифр одинаковой ширины (часы не «прыгают» при смене цифр).
float digit_cell(const Font &f);

}  // namespace text
