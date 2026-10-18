"""Нарезка шрифта Inter в сглаженные 8-битные маски для прошивки.

Запуск из папки firmware (Pillow стоит в tools/.venv):
  tools\\.venv\\Scripts\\python -I tools\\make_fonts.py assets\\fonts\\Inter.ttf main\\face\\fonts_data.cpp

Inter — переменный шрифт (оси «оптический размер» и «вес»), поэтому каждый набор ниже
режется со своими осями: крупные цифры — тонкие и плотные, мелкие подписи — пожирнее.
"""

import sys

from PIL import Image, ImageDraw, ImageFont

CYRILLIC = "".join(chr(c) for c in range(0x410, 0x450)) + "Ёё"
ASCII = "".join(chr(c) for c in range(32, 127))
EXTRA = "°«»—–…·−№"
DIGITS = "0123456789:"

# имя в C++, кегль px, оптический размер, вес, символы
FONTS = [
    ("kClock", 40, 32, 300, DIGITS),
    ("kTimer", 50, 32, 300, DIGITS),
    ("kUi", 15, 14, 500, ASCII + CYRILLIC + EXTRA),
    ("kUiBold", 15, 14, 600, ASCII + CYRILLIC + EXTRA),
    ("kTitle", 24, 24, 600, ASCII + CYRILLIC + EXTRA),
    ("kCaps", 12, 14, 600, ASCII + CYRILLIC + EXTRA),
]


def render(path, size, opsz, weight, chars):
    font = ImageFont.truetype(path, size)
    font.set_variation_by_axes([opsz, weight])
    glyphs, bits = [], bytearray()
    for ch in sorted(set(chars)):
        adv = round(font.getlength(ch) * 16)
        left, top, right, bottom = font.getbbox(ch, anchor="ls")
        w, h = max(0, right - left), max(0, bottom - top)
        off = len(bits)
        if w and h:
            img = Image.new("L", (w, h), 0)
            ImageDraw.Draw(img).text((-left, -top), ch, font=font, fill=255, anchor="ls")
            bits += img.tobytes()
        else:
            w = h = left = top = 0
        assert w < 256 and h < 256 and -128 <= left < 128 and -128 <= top < 128, ch
        glyphs.append((ord(ch), off, w, h, left, top, adv))
    cap = -font.getbbox("0", anchor="ls")[1]
    return glyphs, bits, cap


def main(src, dst):
    sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows по умолчанию не в UTF-8
    out = [
        "// Сгенерировано tools/make_fonts.py из Inter (лицензия OFL: assets/fonts/OFL.txt).",
        "// Руками не править: поменять список в make_fonts.py и запустить его заново.",
        '#include "face/text.h"',
        "",
        "namespace text {",
        "namespace {",
        "",
    ]
    fonts = []
    for name, size, opsz, weight, chars in FONTS:
        glyphs, bits, cap = render(src, size, opsz, weight, chars)
        out.append(f"const uint8_t {name}Bits[] = {{")
        for i in range(0, len(bits), 32):
            out.append("    " + ",".join(str(v) for v in bits[i:i + 32]) + ",")
        out.append("};")
        out.append(f"const Glyph {name}Glyphs[] = {{")
        for cp, off, w, h, x, y, adv in glyphs:
            out.append(f"    {{0x{cp:04X}, {off}, {w}, {h}, {x}, {y}, {adv}}},")
        out.append("};")
        out.append("")
        fonts.append(f"const Font {name} = {{{name}Glyphs, {len(glyphs)}, {name}Bits, {size}, {cap}}};")
        print(f"{name}: {len(glyphs)} символов, {len(bits)} байт")
    out += ["}  // namespace", ""] + fonts + ["", "}  // namespace text", ""]
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
