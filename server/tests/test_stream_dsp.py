import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import stream_dsp  # noqa: E402
from stream_dsp import RATE, Chain, Compressor, Equalizer  # noqa: E402


@pytest.fixture
def eq(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_dsp, "SAVED", tmp_path / "eq.json")
    return Equalizer()


def tone(freq, seconds=2.0, amp=0.3):
    t = np.arange(int(RATE * seconds)) / RATE
    return amp * np.sin(2 * np.pi * freq * t)


def run(chain, x, block=4800):
    return np.concatenate([chain.process(x[i:i + block]) for i in range(0, len(x), block)])


def level(x, freq):
    """Amplitude of one frequency in the last second (single-bin DFT)."""
    x = x[-RATE:]
    t = np.arange(len(x)) / RATE
    return 2 * abs(np.sum(x * np.exp(-2j * np.pi * freq * t))) / len(x)


def test_flat_chain_passes_a_tone_untouched(eq):
    x = tone(1000)
    assert level(run(Chain(eq), x), 1000) == pytest.approx(0.3, rel=0.03)


def test_bass_cut_removes_deep_bass_but_keeps_the_rest(eq):
    eq.set([0] * 8, 0, {"cut": 120})
    chain = Chain(eq)
    assert level(run(chain, tone(50)), 50) < 0.03
    assert level(run(chain, tone(1000)), 1000) > 0.28


def test_virtual_bass_adds_harmonics_of_the_cut_bass(eq):
    eq.set([0] * 8, 0, {"cut": 120, "vbass": 0})
    plain = run(Chain(eq), tone(60))
    eq.set([0] * 8, 0, {"cut": 120, "vbass": 100})
    virtual = run(Chain(eq), tone(60))
    assert level(virtual, 120) > 5 * max(level(plain, 120), 1e-4)


def test_compressor_raises_quiet_parts_more_than_loud_ones(eq):
    eq.set([0] * 8, 0, {"comp": 100})
    chain = Chain(eq)
    quiet = level(run(chain, tone(1000, amp=0.02)), 1000) / 0.02
    loud = level(run(chain, tone(1000, amp=0.5)), 1000) / 0.5
    assert quiet > 2 * loud            # the quiet tone gets a lot of make-up gain, the loud one is held back


def test_compressor_is_transparent_when_off():
    x = tone(500)
    assert np.array_equal(Compressor().process(x, 0.0), x)


def test_output_never_exceeds_full_scale(eq):
    eq.set([15] * 8, 12, {"comp": 100, "vbass": 100})
    assert np.abs(run(Chain(eq), tone(80, amp=0.9))).max() <= 1.0


def test_settings_are_clamped_and_saved(eq):
    eq.set([99] * 8, 99, {"cut": 5, "vbass": 500})
    gains, volume, fx, _ = eq.snapshot()
    assert gains == [stream_dsp.MAX_DB] * 8 and volume == 12.0 and fx["cut"] == 20.0 and fx["vbass"] == 100.0
    assert Equalizer().fx["vbass"] == 100.0       # read back from the saved file
