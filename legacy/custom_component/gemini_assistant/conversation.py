"""Conversation agent для Home Assistant поверх Gemini.

Подключается в Assist Pipeline как обычный conversation agent - HA сам
прогоняет STT перед ним и TTS после него, здесь только "мозги".

ВАЖНО: SDK google-genai активно развивается, перед первым запуском сверь
сигнатуры generate_content/FunctionDeclaration с актуальной документацией
(https://ai.google.dev/gemini-api/docs/function-calling) - здесь дан рабочий
на момент написания, но достаточно стабильный по структуре, вариант.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .google_tools import GoogleServices
from .keep_tools import KeepNotes
from .memory import MemoryStore

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Поднимаем conversation-entity из данных, собранных в __init__.async_setup."""
    from . import DOMAIN  # локальный импорт против циклической зависимости

    data = hass.data[DOMAIN]
    agent = GeminiConversationAgent(
        hass, data["conf"]["api_key"], data["memory"], data["google"], data["keep"]
    )
    data["agent"] = agent
    async_add_entities([agent])

SYSTEM_PROMPT_TEMPLATE = """\
Ты - домашний голосовой ассистент пользователя. Общайся кратко, дружелюбно,
на русском языке, как в живом разговоре (это озвучится вслух через TTS -
не используй списки, markdown и длинные абзацы).

Сейчас {now}. Все даты и времена (due_iso в напоминаниях, start_iso в
событиях календаря) указывай в местном времени пользователя, в формате
ISO 8601 без смещения, например 2026-07-08T18:00:00.

{memory_context}

У тебя есть инструменты: запомнить факт, поставить/снять напоминание,
посмотреть/создать события в календаре, посмотреть непрочитанные письма,
записать заметку в Google Keep (основной способ, когда просят что-то
"записать" или "не забыть" - именно add_keep_note, а не remember_fact),
добавить задачу в Google Tasks (add_task - используй только если явно
просят именно список дел/задачу, а не просто заметку), управлять
устройствами умного дома. Используй инструменты, когда это уместно, не
спрашивай лишний раз подтверждения на безобидные действия.

Ты умеешь развлекать: подбросить монетку, бросить кубик, колесо фортуны
(random_choice — выбрать за пользователя из вариантов), камень-ножницы-бумага,
крестики-нолики, магический шар предсказаний (magic_8_ball), а также проводить
викторины — сам придумывай вопросы по теме, веди счёт вслух и на правильный
ответ вызывай react с mood happy. Оживляй общение мимикой через react (happy —
радость/похвала, surprised — удивление). Если просят новости или сводку по теме
(Украина и Россия, Европа, наука и т.п.) — вызывай news_summary с нужной темой.
"""

COMPACT_PROMPT_TEMPLATE = """\
Ты ведёшь долгосрочную сводку разговоров голосового ассистента с пользователем.

Прежняя сводка:
{summary}

Новые реплики за последнее время:
{dialog}

Объедини всё в одну обновлённую сводку: важные детали о пользователе,
договорённости, незакрытые темы. Не больше 10 предложений. В ответе верни
только текст сводки, без пояснений и заголовков.
"""


class GeminiConversationAgent(conversation.ConversationEntity):
    """Кастомный conversation agent (entity), вызывающий Gemini с function calling.

    Именно conversation.ConversationEntity, а не устаревший
    AbstractConversationAgent: только entity показывается в выпадающем списке
    "Диалоговый агент" в настройках Assist pipeline современного HA.
    """

    _attr_name = "Gemini Assistant"
    _attr_unique_id = "gemini_assistant_conversation"
    # CONTROL = агент может управлять умным домом (у нас есть tool call_ha_service);
    # без этого флага HA прячет часть настроек пайплайна.
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        memory: MemoryStore,
        google: GoogleServices | None,
        keep: KeepNotes | None = None,
    ) -> None:
        self.hass = hass
        self._memory = memory
        self._google = google
        self._keep = keep
        self._history: dict[str, list[dict[str, Any]]] = {}

        from google import genai  # импорт здесь, чтобы не падать при отсутствии пакета на этапе discovery

        self._client = genai.Client(api_key=api_key)

    @property
    def supported_languages(self) -> list[str]:
        return ["ru"]

    # ---------- основной вход из Assist Pipeline ----------

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        conversation_id = user_input.conversation_id or "default"
        history = self._history.setdefault(conversation_id, [])
        history.append({"role": "user", "text": user_input.text})

        reply_text = await self.hass.async_add_executor_job(
            self._generate_reply, conversation_id, history
        )

        history.append({"role": "assistant", "text": reply_text})
        self._trim_history(history)

        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(reply_text)
        return conversation.ConversationResult(
            response=response, conversation_id=conversation_id
        )

    def _trim_history(self, history: list[dict[str, Any]], keep_last: int = 20) -> None:
        # Полная сводка старых разговоров пишется в memory.json ежедневной
        # автоматизацией через сервис gemini_assistant.compact_history
        # (см. homeassistant/automations.yaml), здесь - просто не даём списку
        # разрастаться в рамках одной сессии.
        if len(history) > keep_last:
            del history[: len(history) - keep_last]

    # ---------- суточная компакция истории (сервис gemini_assistant.compact_history) ----------

    async def async_compact_history(self) -> None:
        """Свернуть накопленные реплики всех диалогов в conversation_summary."""
        await self.hass.async_add_executor_job(self._compact_history)

    def _compact_history(self) -> None:
        lines = [
            f"{h['role']}: {h['text']}"
            for history in self._history.values()
            for h in history
        ]
        if not lines:
            return
        prompt = COMPACT_PROMPT_TEMPLATE.format(
            summary=self._memory.get_conversation_summary() or "(пока нет)",
            dialog="\n".join(lines),
        )
        result = self._client.models.generate_content(
            model="gemini-flash-lite-latest", contents=prompt
        )
        summary = (result.text or "").strip()
        if summary:
            self._memory.update_conversation_summary(summary)
            self._history.clear()
            _LOGGER.info("Компакция истории: сводка обновлена, история очищена")

    # ---------- вызов Gemini с ручным циклом function calling ----------

    def _generate_reply(self, conversation_id: str, history: list[dict[str, Any]]) -> str:
        from google.genai import types

        now = self._memory.get_now()
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            now=now.strftime("%Y-%m-%d %H:%M (%A, %Z)"),
            memory_context=self._memory.as_prompt_context(),
        )

        contents = [
            types.Content(role=h["role"] if h["role"] == "user" else "model", parts=[types.Part(text=h["text"])])
            for h in history
        ]

        tools = [types.Tool(function_declarations=self._tool_declarations())]

        for _ in range(4):  # ограничиваем число раундов tool-calling на всякий случай
            result = self._client.models.generate_content(
                model="gemini-flash-lite-latest",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=tools,
                ),
            )
            candidate = result.candidates[0]
            function_calls = [
                p.function_call for p in candidate.content.parts if p.function_call
            ]
            if not function_calls:
                return "".join(p.text or "" for p in candidate.content.parts)

            contents.append(candidate.content)
            for call in function_calls:
                tool_result = self._dispatch_tool(call.name, dict(call.args))
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=call.name, response={"result": tool_result}
                                )
                            )
                        ],
                    )
                )

        return "Что-то я слишком долго думаю - давай попробуем ещё раз попроще."

    # ---------- описание инструментов для модели ----------

    def _tool_declarations(self) -> list[dict[str, Any]]:
        decls: list[dict[str, Any]] = [
            {
                "name": "remember_fact",
                "description": "Сохранить важный факт о пользователе в долгосрочную память.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            {
                "name": "forget_fact",
                "description": "Удалить из памяти факт, содержащий указанный текст.",
                "parameters": {
                    "type": "object",
                    "properties": {"needle": {"type": "string"}},
                    "required": ["needle"],
                },
            },
            {
                "name": "add_reminder",
                "description": "Поставить напоминание на конкретное время.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "due_iso": {
                            "type": "string",
                            "description": "Дата и время в формате ISO 8601, например 2026-07-08T18:00:00",
                        },
                    },
                    "required": ["text", "due_iso"],
                },
            },
            {
                "name": "call_ha_service",
                "description": "Вызвать произвольный сервис Home Assistant, например включить свет.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string", "description": "например light"},
                        "service": {"type": "string", "description": "например turn_on"},
                        "entity_id": {"type": "string"},
                    },
                    "required": ["domain", "service", "entity_id"],
                },
            },
            {
                "name": "flip_coin",
                "description": (
                    "Подбросить монетку: на экране спутника крутится и приземляется монета, "
                    "инструмент возвращает результат. Используй, когда просят подбросить монетку, "
                    "решить орёл/решка или выбрать случайно из двух вариантов. После вызова "
                    "озвучь результат."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "tic_tac_toe",
                "description": (
                    "Игра в крестики-нолики на экране спутника. Ты играешь ноликами (O), "
                    "пользователь — крестиками (X). Клетки 0..8: 0 верх-лево, 1 верх-центр, "
                    "2 верх-право, 3 центр-лево, 4 центр, 5 центр-право, 6 низ-лево, 7 низ-центр, "
                    "8 низ-право. cell=-1 начинает новую игру (поле очищается). Иначе cell 0..8 — "
                    "куда пользователь ставит крестик; инструмент сделает его ход, затем твой "
                    "ответный ход ноликом, нарисует поле и вернёт состояние. Озвучь свой ход "
                    "и статус (твой ход / ты выиграл / я выиграл / ничья)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"cell": {"type": "integer", "description": "-1 начать заново, или 0..8 — ход крестика"}},
                    "required": ["cell"],
                },
            },
            {
                "name": "roll_dice",
                "description": (
                    "Бросить игральный кубик (d6). На экране спутника кубик подпрыгивает и "
                    "приземляется на выпавшую грань. Используй, когда просят бросить кубик "
                    "или нужно случайное число 1..6. Озвучь результат."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "random_choice",
                "description": (
                    "Колесо фортуны — выбрать случайно один из нескольких вариантов. На "
                    "экране крутится колесо и останавливается на выбранном секторе. Используй, "
                    "когда просят 'выбери за меня', 'что мне выбрать', или дают список "
                    "вариантов. Передай options (от 2 до 8 вариантов). Озвучь выбор."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "варианты на выбор, от 2 до 8 штук",
                        }
                    },
                    "required": ["options"],
                },
            },
            {
                "name": "rock_paper_scissors",
                "description": (
                    "Сыграть в камень-ножницы-бумагу. Пользователь называет свой выбор, ты "
                    "выбираешь случайно, показываешь бросок на экране и определяешь победителя. "
                    "Озвучь оба броска и кто выиграл."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_choice": {
                            "type": "string",
                            "description": "выбор пользователя: камень, ножницы или бумага",
                        }
                    },
                    "required": ["user_choice"],
                },
            },
            {
                "name": "magic_8_ball",
                "description": (
                    "Магический шар предсказаний: пользователь задаёт вопрос да/нет, ты трясёшь "
                    "шар на экране и даёшь случайный ответ из классического набора. Верни "
                    "выпавший ответ и озвучь его."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "react",
                "description": (
                    "Показать эмоцию на лице спутника (короткая анимация). mood='happy' — "
                    "радость (правильный ответ в викторине, хорошая новость, похвала/поздравление); "
                    "mood='surprised' — удивление. Вызывай, чтобы оживлять общение мимикой."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"mood": {"type": "string", "description": "happy или surprised"}},
                    "required": ["mood"],
                },
            },
            {
                "name": "news_summary",
                "description": (
                    "Свежая сводка новостей по теме через поиск в интернете. Используй, когда "
                    "просят 'что по новостям', 'новости про Украину и Россию', 'что в Европе', "
                    "'новости науки и открытий' и т.п. Передай topic — тему сводки. Верни готовый "
                    "краткий текст и озвучь его."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "тема сводки: 'Украина и Россия', 'Европа', 'наука и открытия' или 'главные новости'",
                        }
                    },
                    "required": ["topic"],
                },
            },
        ]
        if self._google is not None:
            decls += [
                {
                    "name": "list_upcoming_events",
                    "description": "Получить ближайшие события Google-календаря.",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "name": "create_event",
                    "description": "Создать событие в Google-календаре.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "start_iso": {"type": "string"},
                            "duration_minutes": {"type": "integer"},
                        },
                        "required": ["summary", "start_iso"],
                    },
                },
                {
                    "name": "list_unread_subjects",
                    "description": "Получить темы непрочитанных писем Gmail.",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "name": "add_task",
                    "description": (
                        "Добавить задачу в Google Tasks (список дел/to-do). "
                        "Используй только если пользователь явно просит именно "
                        "задачу/список дел, а не просто заметку - для заметок есть add_keep_note."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            ]
        if self._keep is not None:
            decls.append(
                {
                    "name": "add_keep_note",
                    "description": (
                        "Записать заметку в Google Keep - основной способ, когда пользователь "
                        "просит что-то записать/не забыть. Появится в приложении Google Keep на телефоне."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "title": {"type": "string", "description": "необязательный заголовок заметки"},
                        },
                        "required": ["text"],
                    },
                }
            )
        return decls

    def _esp_call(self, service: str, data: dict[str, Any]) -> None:
        """Вызвать сервис ESPHome-спутника (анимации/эмоции); ошибки не критичны."""
        try:
            self.hass.services.call("esphome", service, data, blocking=False)
        except Exception:  # noqa: BLE001 - анимация не критична
            _LOGGER.warning("не удалось вызвать esphome.%s", service)

    # Динамик спутника для «живых» звуков (в духе Cozmo) во время игр/реакций.
    _SPEAKER = "media_player.spalnia_domashnii_assistent_mediapleer"
    _WWW = "http://192.168.178.86:8123/local"

    def _play_www(self, name: str) -> None:
        """Проиграть короткий wav из /config/www на динамике спутника (не критично)."""
        try:
            self.hass.services.call(
                "media_player", "play_media",
                {
                    "entity_id": self._SPEAKER,
                    "media_content_id": f"{self._WWW}/{name}.wav",
                    "media_content_type": "music",
                    "announce": True,
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 - звук не критичен
            _LOGGER.warning("не удалось проиграть звук %s", name)

    def _news_summary(self, topic: str) -> str:
        """Свежая сводка новостей по теме через Gemini с поиском в интернете.

        Отдельный вызов модели с включённым google_search-grounding: обычная
        модель диалога отвечает из устаревших знаний, а новости должны быть
        свежими. Берём модель побольше (flash-latest) - grounding на ней
        надёжнее; вызовы редкие (по запросу), на суточную квоту не давит.
        """
        from google.genai import types

        prompt = (
            f"Дай краткую сводку самых свежих новостей по теме: {topic}. "
            "4-6 коротких пунктов, только проверенные факты за последние дни, "
            "по-русски, простым разговорным языком для чтения вслух, без ссылок, "
            "сносок и markdown."
        )
        try:
            res = self._client.models.generate_content(
                model="gemini-flash-latest",
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            txt = (res.text or "").strip()
            return txt or "не удалось получить свежую сводку новостей"
        except Exception as err:  # noqa: BLE001 - вернём модели понятную ошибку
            _LOGGER.exception("news_summary grounding failed")
            return f"не смог получить свежие новости: {err}"

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> Any:
        try:
            if name == "remember_fact":
                self._memory.add_fact(args["text"])
                return "запомнено"
            if name == "forget_fact":
                removed = self._memory.forget_fact(args["needle"])
                return f"удалено записей: {removed}"
            if name == "add_reminder":
                self._memory.add_reminder(args["text"], args["due_iso"])
                return "напоминание поставлено"
            if name == "call_ha_service":
                self.hass.services.call(
                    args["domain"],
                    args["service"],
                    {"entity_id": args["entity_id"]},
                    blocking=True,
                )
                return "готово"
            if name == "flip_coin":
                import random
                res = random.randint(0, 1)
                try:
                    self.hass.services.call(
                        "esphome",
                        "home_voice_satellite_flip_coin",
                        {"result": res},
                        blocking=False,
                    )
                except Exception:  # noqa: BLE001 - анимация не критична
                    _LOGGER.warning("не удалось запустить анимацию монетки")
                self._play_www("boop")
                return "выпал орёл" if res == 0 else "выпала решка"
            if name == "tic_tac_toe":
                cell = int(args.get("cell", -1))
                board = getattr(self, "_ttt", None)
                if board is None or cell == -1:
                    board = ["."] * 9
                    self._ttt = board
                lines = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
                def winner(b):
                    for a, c, d in lines:
                        if b[a] != "." and b[a] == b[c] == b[d]:
                            return b[a]
                    return "draw" if "." not in b else None
                def minimax(b, pl):
                    w = winner(b)
                    if w == "O": return (1, -1)
                    if w == "X": return (-1, -1)
                    if w == "draw": return (0, -1)
                    bm = -1
                    if pl == "O":
                        best = -2
                        for i in range(9):
                            if b[i] == ".":
                                b[i] = "O"; s = minimax(b, "X")[0]; b[i] = "."
                                if s > best: best, bm = s, i
                    else:
                        best = 2
                        for i in range(9):
                            if b[i] == ".":
                                b[i] = "X"; s = minimax(b, "O")[0]; b[i] = "."
                                if s < best: best, bm = s, i
                    return (best, bm)
                def render():
                    try:
                        self.hass.services.call(
                            "esphome", "home_voice_satellite_set_ttt",
                            {"board": "".join(board)}, blocking=False,
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.warning("не удалось отрисовать поле")
                if cell == -1:
                    render(); return "новая игра, поле пустое, ты ходишь крестиком (X)"
                if cell < 0 or cell > 8 or board[cell] != ".":
                    return "эта клетка занята или неверная, выбери свободную 0..8"
                board[cell] = "X"
                w = winner(board)
                if w == "X": render(); return f"пользователь поставил крестик в {cell} и ВЫИГРАЛ, игра окончена"
                if w == "draw": render(); return "все клетки заняты, ничья"
                mv = minimax(board, "O")[1]
                if mv >= 0: board[mv] = "O"
                render()
                w2 = winner(board)
                if w2 == "O": return f"ты сходил в {cell}, я поставил нолик в {mv} и ВЫИГРАЛ, игра окончена"
                if w2 == "draw": return f"ты сходил в {cell}, я в {mv}, ничья"
                return f"ты сходил в {cell}, я ответил ноликом в {mv}, теперь твой ход"
            if name == "roll_dice":
                import random
                n = random.randint(1, 6)
                self._esp_call("home_voice_satellite_roll_dice", {"result": n})
                self._play_www("boop")
                return f"выпало {n}"
            if name == "random_choice":
                import random
                opts = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
                if len(opts) < 2:
                    return "нужно минимум два варианта"
                opts = opts[:8]
                idx = random.randrange(len(opts))
                self._esp_call("home_voice_satellite_spin_wheel", {"count": len(opts), "result": idx})
                self._play_www("whistle")
                return f"колесо выбрало: {opts[idx]}"
            if name == "rock_paper_scissors":
                import random
                mapping = {"камень": 0, "rock": 0, "бумага": 1, "paper": 1, "ножницы": 2, "scissors": 2}
                u = mapping.get(str(args.get("user_choice", "")).strip().lower())
                if u is None:
                    return "не понял выбор, скажи камень, ножницы или бумага"
                mine = random.randint(0, 2)
                if mine == u:
                    result = 0
                elif (mine == 0 and u == 2) or (mine == 2 and u == 1) or (mine == 1 and u == 0):
                    result = 1  # ассистент выиграл
                else:
                    result = 2  # выиграл пользователь
                self._esp_call("home_voice_satellite_play_rps", {"mine": mine, "result": result})
                self._play_www("surprise" if result == 1 else "grumble" if result == 2 else "curious")
                label = {0: "камень", 1: "бумага", 2: "ножницы"}
                outcome = "ничья" if result == 0 else ("я выиграл" if result == 1 else "ты выиграл")
                return f"ты — {label[u]}, я — {label[mine]}: {outcome}"
            if name == "magic_8_ball":
                import random
                answers = [
                    "Бесспорно", "Определённо да", "Даже не сомневайся", "Скорее всего да",
                    "Хороший знак, да", "Пока неясно, спроси позже", "Лучше не рассказывать сейчас",
                    "Не рассчитывай на это", "Мой ответ — нет", "Очень сомнительно",
                ]
                idx = random.randrange(len(answers))
                self._esp_call("home_voice_satellite_magic8", {"answer": idx + 1})
                self._play_www("curious")
                return answers[idx]
            if name == "react":
                mood = str(args.get("mood", "happy")).strip().lower()
                code = 2 if mood in ("surprised", "surprise", "alarm", "удивление", "тревога") else 1
                self._esp_call("home_voice_satellite_set_emotion", {"code": code, "ms": 4000})
                self._play_www("surprise" if code == 2 else "happy")
                return "ок"
            if name == "news_summary":
                return self._news_summary(str(args.get("topic", "главные новости")))
            if name == "list_upcoming_events" and self._google:
                return self._google.list_upcoming_events()
            if name == "create_event" and self._google:
                return self._google.create_event(
                    args["summary"],
                    args["start_iso"],
                    args.get("duration_minutes", 60),
                )
            if name == "list_unread_subjects" and self._google:
                return self._google.list_unread_subjects()
            if name == "add_task" and self._google:
                self._google.add_note(args["text"])
                return "задача добавлена в Google Tasks"
            if name == "add_keep_note" and self._keep:
                self._keep.add_note(args["text"], args.get("title", ""))
                return "заметка записана в Google Keep"
        except Exception as err:  # noqa: BLE001 - модель должна получить хоть какой-то ответ
            _LOGGER.exception("Ошибка инструмента %s", name)
            return f"ошибка: {err}"
        return "неизвестный инструмент"
