import json

from jarvis.gemini_live import LIVE_URL, LiveSession, setup_message
from jarvis.wake_vosk import MultiWake, VoskWake, normalize


class FakeRec:
    """Вместо Kaldi: частичный результат берётся из очереди, как будто слово договаривают."""

    def __init__(self, partials):
        self.partials = list(partials)
        self.last = ""

    def AcceptWaveform(self, pcm):
        self.last = self.partials.pop(0) if self.partials else ""
        return False

    def PartialResult(self):
        return json.dumps({"partial": self.last})

    def Result(self):
        return json.dumps({"text": self.last})


def test_fires_on_partial_without_waiting_for_phrase_end():
    scripts = [FakeRec(["", "ма", "мага", "мага"])]
    det = VoskWake("x", ("Мага",), recognizer_factory=lambda: scripts[0] if scripts else FakeRec([]))
    assert [det.process(b"\0" * 3200) for _ in range(3)] == [0.0, 0.0, 1.0]


def test_ignores_other_words_and_resets_after_hit():
    made = []

    def factory():
        made.append(1)
        return FakeRec(["мама", "магазин", "мага"])
    det = VoskWake("x", ("мага",), recognizer_factory=factory)
    assert [det.process(b"\0") for _ in range(3)] == [0.0, 0.0, 1.0]
    assert len(made) == 2             # после срабатывания распознаватель пересоздан


def test_normalize_and_multi():
    assert normalize("Мага ёж") == ["мага", "еж"]

    class Fixed:
        def __init__(self, v):
            self.v, self.resets = v, 0

        def process(self, pcm):
            return self.v

        def reset(self):
            self.resets += 1
    a, b = Fixed(0.2), Fixed(0.9)
    m = MultiWake([a, b])
    assert m.process(b"") == 0.9
    m.reset()
    assert (a.resets, b.resets) == (1, 1)


def test_affective_uses_alpha_endpoint_and_flag():
    on = setup_message("m", "v", "s", affective=True)["setup"]["generationConfig"]
    assert on["enableAffectiveDialog"] is True
    assert "enableAffectiveDialog" not in setup_message("m", "v", "s")["setup"]["generationConfig"]
    assert LiveSession("k", "m", "v", "s", affective=True)._url == LIVE_URL.replace("/v1beta.", "/v1alpha.")
    assert LiveSession("k", "m", "v", "s")._url == LIVE_URL
