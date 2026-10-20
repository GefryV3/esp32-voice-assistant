"""Переводчик (идея #46) на любые языки, которые знает Gemini.

Устроен как немецкий режим (german.py): подсказку живой сессии не поменять, поэтому правила приходят
в РЕЗУЛЬТАТЕ функции translate_mode и действуют до конца разговора; если сессия открывается уже в режиме
(консоль `tr on <язык>`), те же правила идут в системную подсказку вместо «говори только по-русски».
Режимы: phrase (разово перевести фразу), interpret (постоянный переводчик русский ⇄ язык до слов
«хватит переводить»), off. Режим живёт на один разговор. С немецким режимом не пересекается:
включение одного выключает другой.
"""

from __future__ import annotations

import re
from typing import Any

MAX_LANG = 40

TOOLS: list[dict[str, Any]] = [
    {"name": "translate_mode", "description": "Переводчик на любой язык, который ты знаешь. mode=phrase — "
     "«переведи эту фразу на X», «как будет … по-X»: разовый перевод. mode=interpret — «переводи с русского "
     "на X», «будь переводчиком»: постоянный переводчик. mode=off — «хватит переводить» (это НЕ конец "
     "разговора). language — название языка по-русски («английский», «турецкий», «японский»). Вызови и "
     "дальше строго следуй rules из ответа.",
     "parameters": {"type": "OBJECT", "properties": {
         "mode": {"type": "STRING"}, "language": {"type": "STRING"}}, "required": ["mode"]}},
]

PHRASE = ("phrase", "фраза", "разово", "once", "one")
INTERPRET = ("interpret", "переводчик", "постоянно", "синхронный", "on", "вкл", "translate")
OFF = ("off", "стоп", "выкл", "хватит")


def clean_language(raw: str) -> str:
    """Название языка: буквы, пробелы и дефис, коротко. Иначе пусто."""
    lang = " ".join(str(raw).split())
    return lang if lang and len(lang) <= MAX_LANG and re.fullmatch(r"[^\W\d_]+(?:[ \-][^\W\d_]+)*", lang) else ""


def phrase_rules(language: str) -> str:
    return (f"Разовый перевод на язык «{language}». Скажи перевод фразы пользователя на этот язык чётко и "
            "не слишком быстро, потом одним коротким предложением по-русски дай русскую запись звучания "
            "(как читать) — только если язык с другой письменностью или произношение непростое. Если фразу "
            "не расслышал, переспроси. После перевода режим не меняется: дальше говори по-русски. "
            f"Если не уверен, что знаешь язык «{language}» достаточно хорошо, честно скажи об этом.")


def interpret_rules(language: str) -> str:
    return (f"Режим постоянного переводчика: русский ⇄ {language}. Всё, что пользователь говорит по-русски, "
            f"произноси на языке «{language}», только перевод, без вступлений, комментариев и своих мнений. "
            f"Всё, что слышишь на языке «{language}», переводи на русский так же: только перевод. Не отвечай "
            "на содержание сам, не шути и не поправляй, даже если вопрос адресован тебе (переводи и его). "
            "Говори чётко, не слишком быстро; имена и числа не искажай. Если не расслышал, одной фразой "
            "по-русски переспроси. Не вызывай end_conversation, пока режим включён. Выход: «хватит "
            "переводить», «стоп переводчик» — вызови translate_mode с mode=off и один раз коротко скажи "
            "по-русски, что переводчик выключен.")


class Translator:
    """Расширение мозга: .TOOLS, .tool_names, call(), end_conversation()."""

    TOOLS = TOOLS
    tool_names = frozenset(t["name"] for t in TOOLS)

    def __init__(self, german: Any = None):
        self.german = german
        self.active = False
        self.language = ""

    def start(self, language: str) -> str:
        if self.german is not None and getattr(self.german, "active", False):
            self.german.stop()
        self.active, self.language = True, language
        return interpret_rules(language)

    def stop(self) -> bool:
        was, self.active, self.language = self.active, False, ""
        return was

    end_conversation = stop

    def prompt_paragraph(self) -> str:
        """Абзац подсказки вместо «Говори только по-русски», если сессия открыта уже в режиме."""
        return interpret_rules(self.language) if self.active else ""

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name != "translate_mode":
            return {"ok": False, "error": f"нет такого инструмента: {name}"}
        mode = str(args.get("mode", "")).strip().casefold()
        if mode in OFF:
            return {"ok": True, "mode": "off", "was_active": self.stop(),
                    "note": "переводчик выключен; скажи это одной короткой фразой по-русски и дальше говори по-русски"}
        language = clean_language(args.get("language", ""))
        if mode in PHRASE + INTERPRET and not language:
            return {"ok": False, "error": "на какой язык переводить? спроси пользователя"}
        if mode in PHRASE:
            return {"ok": True, "mode": "phrase", "language": language, "rules": phrase_rules(language)}
        if mode in INTERPRET:
            return {"ok": True, "mode": "interpret", "language": language, "rules": self.start(language)}
        return {"ok": False, "error": "mode: phrase, interpret или off"}

    HELP = "tr on <язык> | off | status | prompt — постоянный переводчик (со следующего разговора)"

    async def command(self, line: str) -> str | None:
        head, _, rest = line.strip().partition(" ")
        if head.lower() != "tr":
            return None
        sub, _, arg = rest.strip().partition(" ")
        match sub.lower():
            case "on":
                language = clean_language(arg)
                if not language:
                    return "tr on <язык>, например: tr on английский"
                return f"переводчик (русский ⇄ {language}) включён; правила:\n{self.start(language)}"
            case "off":
                return "переводчик выключен" if self.stop() else "переводчик и так выключен"
            case "status":
                return f"переводчик: {'русский ⇄ ' + self.language if self.active else 'выключен'}"
            case "prompt":
                return self.prompt_paragraph() or "режим выключен"
        return self.HELP
