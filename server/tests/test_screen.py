from jarvis import protocol
from jarvis.screen import Screen


def screen(now=1000.0, **kw):
    s = Screen(active_at=now)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_idle_then_sleep_after_silence():
    s = screen()
    s.apply(protocol.config(60), 1000)
    assert s.summary(1030)["mode"] == "idle"
    assert s.summary(1061)["mode"] == "sleep"


def test_voice_beats_timer_and_night():
    s = screen(night=True)
    s.apply(protocol.timer(300, "Чай"), 1000)
    assert s.summary(1001) == {"mode": "timer", "title": "Таймер", "detail": "Чай", "until": 1300000}
    s.apply(protocol.voice("listening"), 1002)
    assert s.summary(1003)["title"] == "Слушает"
    s.apply(protocol.voice("idle"), 1004)
    assert s.summary(1005)["mode"] == "timer"
    assert s.summary(1301)["mode"] == "night"     # таймер кончился, осталась ночь


def test_card_on_top_until_it_expires_or_hidden():
    s = screen()
    s.apply(protocol.voice("speaking"), 1000)
    s.apply(protocol.notify("call", "Мама", "звонит", ms=10_000), 1000)
    assert s.summary(1005) == {"mode": "card", "title": "Звонок", "detail": "Мама"}
    assert s.summary(1011)["mode"] == "speaking"
    s.apply(protocol.notify("msg", "Брат", "ок"), 1012)
    s.apply(protocol.notify_hide(), 1013)
    assert s.summary(1014)["mode"] == "speaking"


def test_player_keeps_awake():
    s = screen()
    s.apply(protocol.player("playing", "Nights", "Frank Ocean"), 1000)
    assert s.summary(5000) == {"mode": "player", "title": "Играет", "detail": "Frank Ocean — Nights"}
    s.apply(protocol.player("none"), 5001)
    assert s.summary(5002)["mode"] == "sleep"
