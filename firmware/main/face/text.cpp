#include "face/text.h"

namespace text {

const Glyph *find(const Font &f, uint32_t cp) {
  int lo = 0, hi = f.count - 1;
  while (lo <= hi) {
    const int mid = (lo + hi) / 2;
    const uint32_t c = f.glyphs[mid].cp;
    if (c == cp) return &f.glyphs[mid];
    if (c < cp) lo = mid + 1;
    else hi = mid - 1;
  }
  return nullptr;
}

uint32_t next(const char *&s) {
  const uint8_t *p = reinterpret_cast<const uint8_t *>(s);
  const uint32_t c = p[0];
  if (!c) return 0;
  const int n = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 0;
  if (n <= 1) {
    s += 1;
    return n ? c : '?';
  }
  uint32_t cp = c & (0x7F >> n);
  for (int i = 1; i < n; i++) {
    if ((p[i] & 0xC0) != 0x80) {  // оборванный символ
      s += i;
      return '?';
    }
    cp = (cp << 6) | (p[i] & 0x3F);
  }
  s += n;
  return cp;
}

float width(const Font &f, const char *s, float tracking) {
  float w = 0;
  int n = 0;
  for (uint32_t cp; (cp = next(s));) {
    if (const Glyph *g = find(f, cp)) {
      w += g->adv / 16.0f;
      n++;
    }
  }
  return n ? w + tracking * (n - 1) : 0;
}

float digit_cell(const Font &f) {
  float m = 0;
  for (uint32_t c = '0'; c <= '9'; c++) {
    const Glyph *g = find(f, c);
    if (g && g->adv / 16.0f > m) m = g->adv / 16.0f;
  }
  return m;
}

}  // namespace text
