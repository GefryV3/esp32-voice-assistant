"""Разовый скрипт для получения master_token Google Keep (gkeepapi).

Прогнать ОДИН РАЗ на любом компьютере с Python (не обязательно на том, где HA):

    pip install gkeepapi
    python get_keep_master_token.py

Понадобится:
1. Включённый двухфакторный вход (2-Step Verification) на аккаунте Google -
   myaccount.google.com/security -> "2-Step Verification" -> Turn on.
2. App Password для этого - myaccount.google.com/apppasswords ->
   "Select app: Other (custom name)" -> назови, например, "gkeepapi" ->
   Generate -> скопируй 16-значный пароль (без пробелов).

Обычный пароль от аккаунта Google тут не подойдёт - только App Password.

Результат - файл keep_token.json с email и master_token - скопируй его на
ноутбук с Home Assistant в /config/gemini_assistant/keep_token.json (путь
задаётся в configuration.yaml как gemini_assistant.keep_token_path).

Дальше логиниться заново не нужно - master_token переживает практически
бессрочно, пока ты сам не отзовёшь его в настройках аккаунта Google.
"""
import getpass
import json

import gkeepapi

email = input("Google-аккаунт (email): ").strip()
app_password = getpass.getpass("App Password (16 символов, без пробелов): ").strip()

keep = gkeepapi.Keep()
keep.login(email, app_password)

master_token = keep.getMasterToken()

with open("keep_token.json", "w", encoding="utf-8") as f:
    json.dump({"email": email, "master_token": master_token}, f, ensure_ascii=False, indent=2)

print("Готово: keep_token.json создан рядом со скриптом.")
print("Скопируй его в /config/gemini_assistant/keep_token.json на ноутбуке с Home Assistant.")
