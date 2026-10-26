"""Своё русское слово пробуждения («Мага») на Vosk: распознавание речи с грамматикой из пары
слов, а не обученная под слово модель.

Распознаватель слышит только «мага», похожие слова-приманки и [unk] для всего остального:
чужая речь уходит в [unk] и слово не срабатывает. Срабатываем по частичному результату, не
дожидаясь конца фразы: ответ быстрее. Модель vosk-model-small-ru (~45 МБ) лежит в data/models
и при первом запуске скачивается.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_NAME = "vosk-model-small-ru-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
RATE = 16_000
# похожие на «мага» слова: без них распознаватель угадывал бы «мага» вместо них
DECOYS = ("мама", "магия", "магу", "маг", "мало", "много", "нага", "сага", "рага", "мач",
          "мара", "мамы", "магазин", "машу", "мамой", "наго", "могу", "мне", "ага", "ма",
          "макар", "макс", "мак", "марк", "марка", "магнит", "магазина", "магазины", "мача", "мазь", "мать")

_models: dict[str, object] = {}     # одна загруженная модель на процесс

# Words that stop an answer mid-sentence (the speaker streams its echo-cancelled mic while talking).
STOP_WORDS = ("стоп", "хватит", "замолчи", "подожди", "стой")
# What is left of the story's own echo is speech too: with a tiny grammar Vosk forces it onto «стоп».
# Common short words and look-alikes give it somewhere else to go.
STOP_DECOYS = ("сто", "топ", "стол", "стал", "стон", "стоит", "столь", "хватать", "хвост", "звезды", "замок",
               "подождать", "тихо", "тут", "там", "так", "как", "вот", "дом", "лес", "кот", "он", "она", "они",
               "и", "а", "но", "что", "это", "был", "жил", "его", "на", "в", "с", "по", "до", "от", "не",
               "да", "нет", "мама", "сказка", "море", "небо", "ночь", "день", "солнце", "свет")


def ensure_model(model_dir: Path) -> Path:
    """Скачать и распаковать модель, если её ещё нет. Возвращает папку модели."""
    path = model_dir / MODEL_NAME
    if (path / "am").is_dir():
        return path
    model_dir.mkdir(parents=True, exist_ok=True)
    log.info("скачиваю модель Vosk %s (~45 МБ)", MODEL_NAME)
    archive = model_dir / f"{MODEL_NAME}.zip.part"
    urllib.request.urlretrieve(MODEL_URL, archive)
    with zipfile.ZipFile(archive) as z:
        z.extractall(model_dir)
    archive.unlink()
    return path


def normalize(text: str) -> list[str]:
    return text.casefold().replace("ё", "е").split()


class VoskWake:
    """Потоковый детектор с тем же интерфейсом, что WakeWord: process() → 0…1, reset()."""

    def __init__(self, model_dir: Path | str, words: tuple[str, ...] = ("мага",), *, recognizer_factory=None):
        self.words = tuple(normalize(" ".join(words)))
        grammar = json.dumps([*self.words, *(d for d in DECOYS if d not in self.words), "[unk]"],
                             ensure_ascii=False)
        if recognizer_factory is None:
            from vosk import KaldiRecognizer, Model, SetLogLevel
            SetLogLevel(-1)
            key = str(model_dir)
            if key not in _models:
                _models[key] = Model(key)
            recognizer_factory = lambda: KaldiRecognizer(_models[key], RATE, grammar)  # noqa: E731
        self._factory = recognizer_factory
        self.reset()

    def reset(self) -> None:
        self._rec = self._factory()

    def process(self, pcm: bytes) -> float:
        if self._rec.AcceptWaveform(pcm):
            text = json.loads(self._rec.Result()).get("text", "")
        else:
            text = json.loads(self._rec.PartialResult()).get("partial", "")
        if any(w in self.words for w in normalize(text)):
            self.reset()          # не срабатывать второй раз на то же слово
            return 1.0
        return 0.0


class StopWord:
    """«Стоп» посреди ответа. Строже слова пробуждения: только по законченной фразе и с уверенностью
    распознавателя не ниже MIN_CONF — остаток эха ответа не должен превращаться в «стоп»."""

    MIN_CONF = 0.85

    def __init__(self, model_dir: Path | str, words: tuple[str, ...] = STOP_WORDS, *, recognizer_factory=None):
        self.words = tuple(normalize(" ".join(words)))
        grammar = json.dumps([*self.words, *(d for d in STOP_DECOYS if d not in self.words), "[unk]"],
                             ensure_ascii=False)
        if recognizer_factory is None:
            from vosk import KaldiRecognizer, Model, SetLogLevel
            SetLogLevel(-1)
            key = str(model_dir)
            if key not in _models:
                _models[key] = Model(key)

            def recognizer_factory():
                rec = KaldiRecognizer(_models[key], RATE, grammar)
                rec.SetWords(True)
                return rec
        self._factory = recognizer_factory
        self.reset()

    def reset(self) -> None:
        self._rec = self._factory()

    def process(self, pcm: bytes) -> float:
        if not self._rec.AcceptWaveform(pcm):
            return 0.0
        for w in json.loads(self._rec.Result()).get("result", []):
            if normalize(w.get("word", "")) and normalize(w["word"])[0] in self.words and w.get("conf", 0) >= self.MIN_CONF:
                log.info("«%s» (уверенность %.2f)", w["word"], w["conf"])
                return 1.0
        return 0.0


class MultiWake:
    """Несколько детекторов сразу (Мага и Hey Jarvis): берём лучшую оценку."""

    def __init__(self, detectors: list):
        self.detectors = detectors

    def reset(self) -> None:
        for d in self.detectors:
            d.reset()

    def process(self, pcm: bytes) -> float:
        return max(d.process(pcm) for d in self.detectors)
