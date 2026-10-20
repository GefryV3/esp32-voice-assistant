"""Проверка детектора хлопков на синтетических сигналах без железа и без хост-компилятора C++.

Это построчный перенос firmware/main/audio/clap.h (те же константы, та же логика); при правке
детектора правь оба файла. Запуск:  python firmware/tools/clap_sim.py
"""
from __future__ import annotations

import math
import random
import sys

RATE = 16000
BLOCK = 80
BLOCK_MS = 5
SETTLE_BLOCKS = 10
DECAY_MAX = 0.33
HF_MIN = 0.45
REFRACTORY_BLOCKS = 16
GAP_MIN_MS = 250
GAP_MAX_MS = 800
LOCKOUT_MS = 1500
MUTE_TAIL_MS = 600
BG_ALPHA = 1.0 / 200.0
BG_MIN = 8.0


def peak_ratio(sens: int) -> float:
    return 20.0 - 1.5 * sens


def peak_min(sens: int) -> float:
    return 6000.0 - 450.0 * sens


class Detector:
    def __init__(self, sens: int = 5):
        self.sens = max(1, min(10, sens))
        self.buf: list[int] = []
        self.prev = 0
        self.bg = 100.0
        self.settling = False
        self.onset_peak = 0
        self.settle = 0
        self.settle_peak = 0
        self.refractory = 0
        self.lockout = 0
        self.mute_left = 1000 // BLOCK_MS   # первая секунда после включения — мусор микрофона
        self.since_first = -1

    def process(self, pcm, muted: bool = False) -> int:
        fired = 0
        for s in pcm:
            self.buf.append(int(s))
            if len(self.buf) == BLOCK:
                if self._block(self.buf, muted):
                    fired += 1
                self.buf = []
        return fired

    def _block(self, x, muted: bool) -> bool:
        peak = 0
        sum_abs = 0.0
        sum_diff = 0.0
        for v in x:
            a = abs(v)
            peak = max(peak, a)
            sum_abs += a
            sum_diff += abs(v - self.prev)
            self.prev = v
        mean = sum_abs / BLOCK
        hf = sum_diff / sum_abs if sum_abs > 1.0 else 0.0

        if muted:
            self.mute_left = MUTE_TAIL_MS // BLOCK_MS
            self.settling = False
            self.since_first = -1
            self.refractory = 0
            self.bg += (mean - self.bg) * BG_ALPHA
            return False
        if self.mute_left > 0:
            self.mute_left -= 1
            self.bg += (mean - self.bg) * BG_ALPHA
            return False
        if self.lockout > 0:
            self.lockout -= 1
        if self.since_first >= 0:
            self.since_first += BLOCK_MS
            if self.since_first > GAP_MAX_MS:
                self.since_first = -1

        fired = False
        bg = self.bg if self.bg > BG_MIN else BG_MIN
        if not self.settling:
            if self.refractory > 0:
                self.refractory -= 1
            if (self.refractory == 0 and self.lockout == 0 and peak >= peak_min(self.sens)
                    and peak >= peak_ratio(self.sens) * bg and hf >= HF_MIN):
                self.settling = True
                self.onset_peak = peak
                self.settle = SETTLE_BLOCKS
                self.settle_peak = 0
                self.refractory = REFRACTORY_BLOCKS
        else:
            if self.refractory > 0:
                self.refractory -= 1
            self.settle -= 1
            if self.settle < 2 and peak > self.settle_peak:
                self.settle_peak = peak
            if self.settle == 0:
                self.settling = False
                if self.settle_peak <= DECAY_MAX * self.onset_peak:
                    fired = self._on_clap()
        if not self.settling and peak < 3.0 * bg:
            self.bg += (mean - self.bg) * BG_ALPHA
        return fired

    def _on_clap(self) -> bool:
        if self.since_first >= 0:
            if self.since_first < GAP_MIN_MS:
                return False
            self.since_first = -1
            self.lockout = LOCKOUT_MS // BLOCK_MS
            return True
        self.since_first = 0
        return False


class Highpass:
    """Баттерворт 2-го порядка, как Highpass в audio.cpp."""

    def __init__(self, fc: float = 100.0, fs: float = RATE):
        w0 = 2 * math.pi * fc / fs
        c, alpha = math.cos(w0), math.sin(w0) / math.sqrt(2)
        a0 = 1 + alpha
        self.b0 = self.b2 = (1 + c) / 2 / a0
        self.b1 = -(1 + c) / a0
        self.a1 = -2 * c / a0
        self.a2 = (1 - alpha) / a0
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def __call__(self, x: float) -> float:
        y = self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2
        self.x2, self.x1, self.y2, self.y1 = self.x1, x, self.y1, y
        return y


# ── синтетические сигналы ──


def room(seconds: float, sigma: float = 150.0, seed: int = 1) -> list[float]:
    rnd = random.Random(seed)
    return [rnd.gauss(0, sigma) for _ in range(int(seconds * RATE))]


def add(sig: list[float], at: float, burst: list[float]) -> None:
    i0 = int(at * RATE)
    for i, v in enumerate(burst):
        if i0 + i < len(sig):
            sig[i0 + i] += v


def clap(amp: float, tau_ms: float = 8.0, seed: int = 7) -> list[float]:
    """Шумовой щелчок с экспоненциальным затуханием (после ФНЧ ~4 кГц — не белый)."""
    rnd = random.Random(seed)
    n = int(RATE * tau_ms * 8 / 1000)
    out, lp = [], 0.0
    for i in range(n):
        lp += (rnd.gauss(0, 1) - lp) * 0.7
        out.append(amp * 1.6 * lp * math.exp(-i / (RATE * tau_ms / 1000)))
    return out


def vowel(f0: float, seconds: float, amp: float, attack_ms: float = 10.0) -> list[float]:
    n = int(seconds * RATE)
    out = []
    for i in range(n):
        t = i / RATE
        env = min(1.0, t * 1000 / attack_ms) * min(1.0, (seconds - t) * 1000 / 30)
        s = sum(math.sin(2 * math.pi * f0 * h * t) / h for h in range(1, 14))
        out.append(amp * env * s / 2.5)
    return out


def speech(seconds: float, amp: float = 6000.0, seed: int = 3) -> list[float]:
    """Слоги по ~200 мс с резким началом и «п» перед гласной (взрывной шум 20 мс)."""
    rnd = random.Random(seed)
    sig = [0.0] * int(seconds * RATE)
    t = 0.1
    while t < seconds - 0.3:
        add(sig, t, [rnd.gauss(0, amp * 0.5) * math.exp(-i / (RATE * 0.008)) for i in range(int(RATE * 0.02))])
        add(sig, t + 0.015, vowel(110 + rnd.random() * 90, 0.18, amp))
        t += 0.28
    return sig


def music(seconds: float, seed: int = 4) -> list[float]:
    """Аккорд плюс «бочка» 60 Гц с затуханием каждые полсекунды."""
    sig = [sum(2500 * math.sin(2 * math.pi * f * i / RATE) for f in (220, 277, 330)) for i in range(int(seconds * RATE))]
    kick = [9000 * math.sin(2 * math.pi * 60 * i / RATE) * math.exp(-i / (RATE * 0.05)) for i in range(int(RATE * 0.3))]
    t = 0.0
    while t < seconds - 0.3:
        add(sig, t, kick)
        t += 0.5
    return sig


def wifi_click(amp: float = 9000.0) -> list[float]:
    """Бросок питания микрофона: затухающая синусоида 70 Гц."""
    return [amp * math.sin(2 * math.pi * 70 * i / RATE) * math.exp(-i / (RATE * 0.02)) for i in range(int(RATE * 0.15))]


def run(sig: list[float], sens: int = 5, muted_from: float | None = None, muted_to: float | None = None) -> int:
    det = Detector(sens)
    hp = Highpass()
    pcm = [max(-32768, min(32767, int(hp(v)))) for v in sig]
    total = 0
    step = 320   # кадр 20 мс, как в mic_task
    for i in range(0, len(pcm) - step + 1, step):
        t = i / RATE
        muted = muted_from is not None and muted_from <= t < (muted_to or 1e9)
        total += det.process(pcm[i:i + step], muted)
    return total


CASES: list[tuple[str, int, int, dict]] = []   # (имя, ожидается срабатываний, чувствительность, пустое)


def main() -> int:
    failed = 0

    def check(name: str, got: int, want: int) -> None:
        nonlocal failed
        ok = got == want
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: сработало {got}, ждали {want}")

    def claps(times, amp=12000.0, sigma=150.0, tau=8.0):
        s = room(max(times) + 2.6, sigma)
        for k, t in enumerate(times):
            add(s, t + 0.6, clap(amp, tau, seed=10 + k))   # первая секунда глушится
        return s

    check("два хлопка через 0.4 с", run(claps([1.0, 1.4])), 1)
    check("два хлопка через 0.3 с", run(claps([1.0, 1.3])), 1)
    check("два хлопка через 0.7 с", run(claps([1.0, 1.7])), 1)
    check("один хлопок", run(claps([1.0])), 0)
    check("два хлопка через 1.2 с (долго)", run(claps([1.0, 2.2])), 0)
    check("два хлопка через 0.12 с (эхо)", run(claps([1.0, 1.12])), 0)
    check("три хлопка по 0.4 с — одно срабатывание", run(claps([1.0, 1.4, 1.8])), 1)
    check("две серии с паузой 3 с", run(claps([1.0, 1.4, 4.5, 4.9])), 2)
    check("хлопки с эхом комнаты (tau 30 мс)", run(claps([1.0, 1.45], tau=30.0)), 1)
    check("хлопки в шумной комнате (sigma 600)", run(claps([1.0, 1.4], amp=16000, sigma=600.0)), 1)
    # пик хлопка после фильтра ≈ 2.15 × amp
    check("тихие хлопки (пик ~2200), чувствительность 5", run(claps([1.0, 1.4], amp=1000.0)), 0)
    check("тихие хлопки (пик ~2200), чувствительность 10", run(claps([1.0, 1.4], amp=1000.0), sens=10), 1)
    check("средние хлопки (пик ~3900), чувствительность 1", run(claps([1.0, 1.4], amp=1800.0), sens=1), 0)
    check("средние хлопки (пик ~4300), чувствительность 5", run(claps([1.0, 1.4], amp=2000.0), sens=5), 1)
    check("тишина", run(room(5.0)), 0)
    check("речь 8 с", run(speech(8.0)), 0)
    check("громкая речь 8 с (амплитуда 14000)", run(speech(8.0, amp=14000.0)), 0)
    check("музыка с бочкой 6 с", run(music(6.0)), 0)
    check("щелчки питания Wi-Fi (две за 0.4 с)", run(_with(room(4.0), [(1.5, wifi_click()), (1.9, wifi_click())])), 0)
    # хлопки, пока играет колонка, не считаются; после — снова работают
    s = claps([1.0, 1.4, 4.0, 4.4])
    check("хлопки во время ответа колонки глушатся", run(s, muted_from=1.1, muted_to=3.1), 1)
    # речь, на которую накладывается хлопок-серия
    s = speech(6.0, amp=3000.0)
    add(s, 2.5, clap(15000, seed=21))
    add(s, 2.9, clap(15000, seed=22))
    check("хлопки поверх тихой речи", run(s), 1)
    print("\nвсё прошло" if not failed else f"\nупало: {failed}")
    return 1 if failed else 0


def _with(sig: list[float], items) -> list[float]:
    for at, burst in items:
        add(sig, at, burst)
    return sig


if __name__ == "__main__":
    sys.exit(main())




