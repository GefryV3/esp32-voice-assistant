import asyncio

from jarvis.brain import Brain
from jarvis.health import ErrorLog, Health, uptime_words


class Hello:
    fw = "0.9.1"


class Dev:
    hello = Hello()
    connected_at = 1000.0


class Hub:
    def __init__(self, dev=None):
        self.dev = dev

    def get(self):
        return self.dev


class Peer:
    def __init__(self, state):
        self.state = state


class Bridge:
    def __init__(self):
        self.phone = Peer({})
        self.phone_state = {"battery": 41, "charging": True}
        self.pc = Peer({"battery": 80})
        self.bt = [{"name": "Nothing Ear", "level": 15}, {"name": "x", "level": -1}]


class HA:
    def __init__(self, fail=None):
        self.fail = fail

    async def request(self, type_, **kw):
        if self.fail == "hang":
            await asyncio.sleep(100)
        if self.fail:
            raise RuntimeError(self.fail)
        return {}


class G:
    def __init__(self, ok=True):
        self.ok = ok

    async def api(self, *a, **k):
        if not self.ok:
            raise RuntimeError("Google не дал доступ")
        return {}


def test_error_counter_window_and_persistence(tmp_path):
    t = [0.0]
    errs = ErrorLog(clock=lambda: t[0])
    brain = Brain(tmp_path / "b.db")
    errs.bind(brain)
    errs.add("голос")
    t[0] = 3600
    errs.add("telegram")
    errs.add("голос")
    assert errs.count() == {"голос": 2, "telegram": 1}
    t[0] = 86400 + 10                        # первая ошибка старше суток
    assert errs.count() == {"telegram": 1, "голос": 1}
    again = ErrorLog(clock=lambda: t[0])
    again.bind(brain)
    assert again.count() == errs.count()


def test_uptime_words():
    assert uptime_words(125) == "2 мин" and uptime_words(3 * 3600 + 600) == "3 ч 10 мин"
    assert uptime_words(2 * 86400 + 5 * 3600) == "2 д 5 ч"


async def test_report_all_good():
    t = [5000.0]
    errs = ErrorLog(clock=lambda: t[0])
    h = Health(hub=Hub(Dev()), bridge=Bridge(), ha=HA(), google=G(), errors=errs, started=1000.0,
               clock=lambda: t[0], version="0.13.0")
    r = await h.call("health_report", {})
    assert r["server_version"] == "0.13.0" and r["uptime"] == "1 ч 6 мин"
    assert r["speaker"] == {"link": "на связи", "firmware": "0.9.1", "connected_min": 66}
    assert r["phone"] == "на связи" and r["pc"] == "на связи"
    assert r["home_assistant"] == "на связи" and r["google"] == "на связи"
    assert r["gemini_errors_24h"] == 0 and r["problems"] == []
    assert r["batteries"] == ["телефон 41 %, заряжается", "ноутбук 80 %", "Nothing Ear 15 %"]


async def test_report_problems(monkeypatch):
    import jarvis.health as hm
    monkeypatch.setattr(hm, "CHECK_TIMEOUT_S", 0.05)
    errs = ErrorLog()
    for _ in range(6):
        errs.add("голос")
    b = Bridge()
    b.phone = None
    h = Health(hub=Hub(None), bridge=b, ha=HA("hang"), google=G(False), errors=errs)
    r = await h.report()
    assert r["speaker"]["link"] == "не на связи" and r["phone"] == "не на связи"
    assert "таймаут" in r["home_assistant"] and "ошибка" in r["google"]
    assert set(r["problems"]) == {"home_assistant", "google", "speaker", "gemini"}


async def test_report_without_optional_parts_and_console():
    h = Health(errors=ErrorLog())
    r = await h.report()
    assert r["home_assistant"] == "не настроен" and r["google"] == "не настроен" and r["batteries"] == "данных нет"
    assert "server_version" in await h.command("health")
    assert await h.command("что-то") is None


async def test_health_as_brain_extension(tmp_path):
    # 0.14.0 падал на каждом разговоре: у Health не было .TOOLS, а Brain.tools() их собирает
    brain = Brain(tmp_path / "b.db")
    brain.extensions = [Health(errors=ErrorLog())]
    assert "health_report" in {t["name"] for t in brain.tools()}
    r = await brain.call("health_report", {}, None)
    assert "server_version" in r
