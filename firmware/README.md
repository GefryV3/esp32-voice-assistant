# Прошивка колонки

C++17 на ESP-IDF 5.5.5. Модули описаны в [docs/architecture.md](../docs/architecture.md).

## Перед первой сборкой

1. `main/secrets.h`: если его нет — скопировать из `main/secrets.h.example`. Вписать Wi-Fi,
   адрес сервера и токен (тот же, что `JARVIS_DEVICE_TOKEN` в `server/.env`).
2. Собирать **только из PowerShell или cmd** (из Git Bash тулчейн ломается) и только из
   пути без кириллицы.

## Команды

```powershell
cd C:\dev\jarvis\firmware
.\idf.ps1 build                    # собрать
.\idf.ps1 -p COM5 flash monitor    # залить по USB и смотреть логи (COM-порт свой)
.\idf.ps1 size                     # сколько занято flash/RAM
.\idf.ps1 menuconfig               # настройки ESP-IDF
```

`idf.ps1` сам подставляет окружение ESP-IDF (он установлен ESPHome'ом в
`C:\Users\kurli\.local\idf`, `C:\idf` — короткая ссылка на эту папку).

Готовый образ для обновления по воздуху: `build\jarvis.bin`.

## Что видно на экране

- **Заставка 1.5 с** при включении: красная, зелёная и синяя полосы сверху вниз, цифры
  «123» слева направо. Если цвета или цифры перепутаны — поправить `LCD_BGR` /
  `LCD_MIRROR_X` в `main/board.h`.
- Дальше — лицо по макету [design/screen-mockup.html](../design/screen-mockup.html).
- **Красный перечёркнутый Wi-Fi слева от погоды** — нет связи с сервером.
- Часы и дата появляются, когда сервер прислал время.
- Раз в 5 с в лог пишется частота кадров и сколько занимает каждая часть кадра.

## Шрифты

Буквы для экрана нарезаны из Inter (`assets/fonts/Inter.ttf`, лицензия OFL) в
`main/face/fonts_data.cpp`. Чтобы поменять кегль или набор символов — правка списка в
`tools/make_fonts.py` и запуск:

```powershell
py -m venv tools\.venv
tools\.venv\Scripts\pip install pillow
tools\.venv\Scripts\python -I tools\make_fonts.py assets\fonts\Inter.ttf main\face\fonts_data.cpp
```

## Откат

Прошивка, залитая по воздуху, сама подтверждает себя после подключения к Wi-Fi. Если она
не дошла до Wi-Fi, после перезагрузки вернётся предыдущая. Вернуться на старый ESPHome
можно всегда: разметка памяти у них одинаковая.
