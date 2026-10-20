"""Рисует схему архитектуры для README: architecture-{dark,light}[-ru].svg рядом с этим файлом."""
from pathlib import Path

THEMES = {
    "dark": dict(box="#161b22", stroke="#30363d", group="#0d1117", gstroke="#3d444d", text="#e6edf3",
                 muted="#9198a1", accent="#58a6ff", accent_bg="#0c2d6b", line="#6e7681", pill="#21262d"),
    "light": dict(box="#ffffff", stroke="#d1d9e0", group="#f6f8fa", gstroke="#c8d1da", text="#1f2328",
                  muted="#59636e", accent="#0969da", accent_bg="#ddf4ff", line="#818b98", pill="#f6f8fa"),
}

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"


def box(x, y, w, h, title, sub="", fill="box", stroke="stroke", r=10, accent=False):
    t_y = y + h / 2 + (-4 if sub else 5)
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{{{fill}}}" '
           f'stroke="{{{"accent" if accent else stroke}}}" stroke-width="{1.5 if accent else 1}"/>',
           f'<text x="{x + w / 2}" y="{t_y}" class="t">{title}</text>']
    if sub:
        out.append(f'<text x="{x + w / 2}" y="{t_y + 17}" class="s">{sub}</text>')
    return "\n".join(out)


def group(x, y, w, h, title, sub):
    return "\n".join([
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{{group}}" stroke="{{gstroke}}"/>',
        f'<text x="{x + 20}" y="{y + 30}" class="g st">{title}</text>',
        f'<text x="{x + 20}" y="{y + 49}" class="s st">{sub}</text>',
    ])


def pill(x, y, w, label):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="28" rx="14" fill="{{pill}}" stroke="{{stroke}}"/>'
            f'<text x="{x + w / 2}" y="{y + 18.5}" class="p">{label}</text>')


def edge(d, audio=False, both=False, label="", lx=0, ly=0):
    cls = "ea" if audio else "ec"
    marker = "a" if audio else "c"
    start = f' marker-start="url(#{marker}s)"' if both else ""
    out = [f'<path d="{d}" class="{cls}" marker-end="url(#{marker}e)"{start}/>']
    if label:
        out.append(f'<text x="{lx}" y="{ly}" class="l">{label}</text>')
    return "\n".join(out)


BODY = "\n".join([
    # left column: clients
    group(20, 20, 250, 250, "Speaker", "ESP32-S3 · C++17 · ESP-IDF 5.5"),
    pill(40, 92, 210, "INMP441 microphone"),
    pill(40, 130, 210, "MAX98357A amplifier"),
    pill(40, 168, 210, "GC9A01 round display"),
    pill(40, 206, 210, "face engine · 60 fps"),
    box(20, 300, 250, 58, "Android app", "Kotlin · Jetpack Compose"),
    box(20, 376, 250, 58, "PC agent", "presence · battery · links"),
    box(20, 452, 250, 58, "Telegram bot", "same conversation as voice"),

    # middle column: server
    group(340, 20, 290, 490, "Brain", "Python · asyncio · Home Assistant add-on"),
    box(365, 84, 240, 56, "Device hub", "WebSocket · OTA · screen"),
    box(365, 172, 240, 56, "Wake word", "custom Russian word · Vosk"),
    box(365, 260, 240, 56, "Voice session", "streaming · barge-in", accent=True, fill="accent_bg"),
    box(365, 348, 240, 140, "", ""),
    '<text x="485" y="376" class="t">Tools &amp; memory</text>',
    '<text x="485" y="400" class="s">reminders · timers · lights</text>',
    '<text x="485" y="418" class="s">calendar · mail · tasks</text>',
    '<text x="485" y="436" class="s">search agents · routines</text>',
    '<text x="485" y="454" class="s">presence · learning</text>',
    '<text x="485" y="472" class="s">plug-in skills</text>',

    # right column: services
    '<text x="720" y="44" class="g st">Legend</text>',
    '<path d="M720 66 h44" class="ea" marker-end="url(#ae)"/>',
    '<text x="774" y="70" class="s st">audio stream (PCM)</text>',
    '<path d="M720 92 h44" class="ec" marker-end="url(#ce)"/>',
    '<text x="774" y="96" class="s st">commands · data</text>',
    '<text x="720" y="128" class="s st">Every device keeps one WebSocket</text>',
    '<text x="720" y="146" class="s st">to the hub: JSON text frames for</text>',
    '<text x="720" y="164" class="s st">commands, binary frames for audio.</text>',
    '<text x="720" y="198" class="s st">The speaker stays usable offline:</text>',
    '<text x="720" y="216" class="s st">clock, alarms, timers and claps</text>',
    '<text x="720" y="234" class="s st">run on the device itself.</text>',
    box(700, 260, 240, 56, "Gemini Live API", "speech ⇄ speech, tools", accent=True, fill="accent_bg"),
    box(700, 352, 240, 56, "Home Assistant", "lights · sensors · presence"),
    box(700, 432, 240, 56, "Web services", "Google · search · weather · transit"),

    # edges
    edge("M270 112 C310 112 320 104 365 104", audio=True, both=True),
    edge("M270 190 C315 190 320 124 365 124", both=True),
    edge("M270 329 C320 329 330 136 365 132", both=True),
    edge("M270 405 C326 405 336 140 365 138", both=True),
    edge("M270 481 C320 481 330 430 365 430", both=True),
    edge("M485 140 V170", audio=True),
    edge("M485 228 V258", audio=True),
    edge("M485 316 V346", both=True),
    edge("M605 280 H698", audio=True),
    edge("M698 296 H607", audio=True),
    edge("M605 380 H698", both=True),
    edge("M605 460 H698"),
])

TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 530" width="960" height="530" role="img"
     aria-label="Architecture: the ESP32-S3 speaker, Android app, PC agent and Telegram connect to the Python brain, which talks to Gemini Live, Home Assistant and web services">
<defs>
  <marker id="ae" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{accent}"/></marker>
  <marker id="as" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{accent}"/></marker>
  <marker id="ce" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{line}"/></marker>
  <marker id="cs" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{line}"/></marker>
</defs>
<style>
  text {{ font-family: {font}; text-anchor: middle; }}
  .t {{ font-size: 15px; font-weight: 600; fill: {text}; }}
  .g {{ font-size: 17px; font-weight: 700; fill: {text}; }}
  .s {{ font-size: 12.5px; fill: {muted}; }}
  .p {{ font-size: 12.5px; fill: {text}; }}
  .st {{ text-anchor: start; }}
  .l {{ font-size: 12px; fill: {muted}; font-style: italic; }}
  .ea {{ fill: none; stroke: {accent}; stroke-width: 2; }}
  .ec {{ fill: none; stroke: {line}; stroke-width: 1.5; stroke-dasharray: 5 4; }}
</style>
{body}
</svg>
"""

out = Path(__file__).resolve().parent
for name, c in THEMES.items():
    body = BODY
    for k, v in c.items():
        body = body.replace("{" + k + "}", v)
    svg = TEMPLATE.format(body=body, font=FONT, **c)
    (out / f"architecture-{name}.svg").write_text(svg, encoding="utf-8")
    print(name, len(svg))

RU = {
    '>Speaker<': '>Колонка<', "INMP441 microphone": "микрофон INMP441", "MAX98357A amplifier": "усилитель MAX98357A",
    "GC9A01 round display": "круглый экран GC9A01", "face engine · 60 fps": "движок лица · 60 к/с",
    '>Android app<': '>Приложение Android<', '>PC agent<': '>Агент на ПК<',
    "presence · battery · links": "присутствие · заряд · ссылки", '>Telegram bot<': '>Telegram-бот<',
    "same conversation as voice": "тот же разговор, что голосом", '>Brain<': '>Мозг<',
    "Python · asyncio · Home Assistant add-on": "Python · asyncio · аддон Home Assistant",
    '>Device hub<': '>Хаб устройств<', "WebSocket · OTA · screen": "WebSocket · OTA · экран",
    '>Wake word<': '>Слово пробуждения<', "custom Russian word · Vosk": "своё слово «Мага» · Vosk",
    '>Voice session<': '>Голосовая сессия<', "streaming · barge-in": "поток · перебивание",
    "Tools &amp; memory": "Инструменты и память", "reminders · timers · lights": "напоминания · таймеры · свет",
    "calendar · mail · tasks": "календарь · почта · задачи", "search agents · routines": "поиск в интернете · рутины",
    "presence · learning": "присутствие · обучение", "plug-in skills": "навыки-плагины",
    ">Legend<": ">Обозначения<", "audio stream (PCM)": "поток звука (PCM)", "commands · data": "команды · данные",
    "Every device keeps one WebSocket": "Один WebSocket на устройство:",
    "to the hub: JSON text frames for": "в текстовых кадрах JSON — команды,",
    "commands, binary frames for audio.": "в бинарных — звук.",
    "The speaker stays usable offline:": "Без сервера колонка не беспомощна:",
    "clock, alarms, timers and claps": "часы, будильник, таймеры и хлопки",
    "run on the device itself.": "работают на ней самой.",
    "speech ⇄ speech, tools": "речь ⇄ речь, инструменты", "lights · sensors · presence": "свет · датчики · присутствие",
    '>Web services<': '>Веб-сервисы<', "Google · search · weather · transit": "Google · поиск · погода · транспорт",
}
RU_ARIA = ("Архитектура: колонка на ESP32-S3, приложение Android, агент на ПК и Telegram подключены к мозгу на Python, "
           "который говорит с Gemini Live, Home Assistant и веб-сервисами")
for name, c in THEMES.items():
    body = BODY
    for en, ru in RU.items():
        body = body.replace(en, ru)
    for k, v in c.items():
        body = body.replace("{" + k + "}", v)
    svg = TEMPLATE.format(body=body, font=FONT, **c)
    a = svg.index('aria-label="') + 12
    svg = svg[:a] + RU_ARIA + svg[svg.index('">\n<defs>'):]
    (out / f"architecture-{name}-ru.svg").write_text(svg, encoding="utf-8")
    print(name, "ru", len(svg))
