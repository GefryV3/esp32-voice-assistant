import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import stream_dsp  # noqa: E402
from stream_dsp import LOOK, RATE, Chain, Compressor, Equalizer, Limiter  # noqa: E402


@pytest.fixture
def eq(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_dsp, "SAVED", tmp_path / "eq.json")
    monkeypatch.setattr(stream_dsp, "CORRECTION", tmp_path / "correction.json")   # not the real measured one
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


def test_limiter_keeps_peaks_under_the_ceiling():
    x = tone(100, amp=1.4) + tone(2500, amp=0.6)
    assert np.abs(run(Limiter(ceiling=0.9), x)).max() <= 0.9 * 1.06     # a few percent may slip through the attack


def test_limiter_is_transparent_below_the_ceiling_and_delays_by_the_lookahead():
    x = tone(440, amp=0.4)
    y = run(Limiter(), x)
    assert np.allclose(y[LOOK:], x[:-LOOK], atol=1e-6)


def test_limiter_works_across_block_boundaries():
    x = np.zeros(RATE)
    x[RATE // 2] = 1.8                        # one loud click right in the middle
    assert np.abs(run(Limiter(ceiling=0.8), x, block=480)).max() < 0.8 * 1.06
