import json

import pytest

from jarvis import protocol
from jarvis.console import CommandError, parse_command
from jarvis.protocol import ProtocolError


def hello(**over):
    msg = {"type": "hello", "proto": 1, "token": "t", "device": "jarvis-hub",
           "fw": "0.1.0", "caps": ["display", "teleport"]}
    msg.update(over)
    return json.dumps(msg)


def test_parse_hello_ok_and_drops_unknown_caps():
    h = protocol.parse_hello(hello())
    assert h.device == "jarvis-hub"
    assert h.caps == {"display"}


@pytest.mark.parametrize("raw, code", [
    (hello(type="status"), protocol.CLOSE_BAD_HELLO),
    (hello(proto=2), protocol.CLOSE_BAD_VERSION),
    (hello(device="Bad Name!"), protocol.CLOSE_BAD_HELLO),
    (hello(token=None), protocol.CLOSE_BAD_HELLO),
    ("{not json", protocol.CLOSE_BAD_HELLO),
    (b"\x00\x01", protocol.CLOSE_BAD_HELLO),
])
def test_parse_hello_rejects(raw, code):
    with pytest.raises(ProtocolError) as e:
        protocol.parse_hello(raw)
    assert e.value.close_code == code


def test_builders_validate_ranges():
    assert protocol.dice(6) == {"type": "game", "name": "dice", "result": 6}
    assert protocol.wheel(4, 3)["result"] == 3
    with pytest.raises(ValueError):
        protocol.dice(7)
    with pytest.raises(ValueError):
        protocol.wheel(4, 4)
    with pytest.raises(ValueError):
        protocol.brightness(True)          # bool — не число
    with pytest.raises(ValueError):
        protocol.ttt("XOXOXOXO")           # 8 клеток
    with pytest.raises(ValueError):
        protocol.voice("dancing")
    with pytest.raises(ValueError):
        protocol.emotion("alarm")          # старое имя, новый экран его не знает
    with pytest.raises(ValueError):
        protocol.caption("reply", "я" * 64)  # 128 байт UTF-8 — не влезет в буфер колонки
    with pytest.raises(ValueError):
        protocol.timer(-1)
    with pytest.raises(ValueError):
        protocol.config(sleep_clock="left")


def test_screen_builders():
    assert protocol.caption("reply", "Свет включён", "light") == {
        "type": "caption", "kind": "reply", "text": "Свет включён", "icon": "light"}
    assert protocol.timer(600, "Паста") == {"type": "timer", "sec": 600, "label": "Паста"}
    assert protocol.agenda("ВРАЧ", "ЗАВТРА В 9:30") == {
        "type": "agenda", "accent": "ВРАЧ", "text": "ЗАВТРА В 9:30"}
    assert protocol.config(120) == {"type": "config", "sleep_after": 120}


def test_encode_keeps_cyrillic_compact():
    assert protocol.encode({"type": "x", "t": "привет"}) == '{"type":"x","t":"привет"}'


@pytest.mark.parametrize("line, expected", [
    ("voice listening", {"type": "voice", "state": "listening"}),
    ("emotion happy", {"type": "emotion", "name": "happy", "ms": 2600}),
    ("emotion skeptic 1500", {"type": "emotion", "name": "skeptic", "ms": 1500}),
    ("say heard Включи свет", {"type": "caption", "kind": "heard", "text": "Включи свет", "icon": "none"}),
    ("say reply light Свет на кухне включён",
     {"type": "caption", "kind": "reply", "text": "Свет на кухне включён", "icon": "light"}),
    ("say none", {"type": "caption", "kind": "none", "text": "", "icon": "none"}),
    ("timer 30 Паста на ужин", {"type": "timer", "sec": 30, "label": "Паста на ужин"}),
    ("timer 0", {"type": "timer", "sec": 0, "label": ""}),
    ("agenda ВРАЧ | ЗАВТРА В 9:30", {"type": "agenda", "accent": "ВРАЧ", "text": "ЗАВТРА В 9:30"}),
    ("agenda", {"type": "agenda", "accent": "", "text": ""}),
    ("sleep 120 bottom", {"type": "config", "sleep_after": 120, "sleep_clock": "bottom"}),
    ("wheel 6 2", {"type": "game", "name": "wheel", "count": 6, "result": 2}),
    ("ttt x.o......", {"type": "game", "name": "ttt", "board": "X.O......"}),
    ("weather 3 -2", {"type": "weather", "cond": 3, "temp": -2}),
    ("night on", {"type": "night", "on": True}),
    ("task working 40 Telegram-бот",
     {"type": "task_status", "state": "working", "label": "Telegram-бот", "pct": 40}),
    ("task working Без процента", {"type": "task_status", "state": "working", "label": "Без процента", "pct": -1}),
    ("task needs_input", {"type": "task_status", "state": "needs_input", "label": "", "pct": -1}),
])
def test_parse_command(line, expected):
    assert parse_command(line) == expected


@pytest.mark.parametrize("line", ["", "dice", "dice x", "dice 9", "night maybe", "fly",
                                  "emotion alarm", "timer soon", "task sleeping", "task working 101", "say shout громко", "sleep 5"])
def test_parse_command_errors(line):
    with pytest.raises(CommandError):
        parse_command(line)


def test_barge_message():
    assert protocol.barge(True, 4.0) == {"type": "barge", "on": 1, "ratio_x10": 40}
    assert protocol.barge(False, 2.5)["ratio_x10"] == 25


def test_task_status_validates():
    assert protocol.task_status("done", "Сборка", 100) == {
        "type": "task_status", "state": "done", "label": "Сборка", "pct": 100}
    for bad in (lambda: protocol.task_status("busy"), lambda: protocol.task_status("working", "x", 101),
                lambda: protocol.task_status("working", "x", -2), lambda: protocol.task_status("working", "я" * 16)):
        with pytest.raises(ValueError):
            bad()
