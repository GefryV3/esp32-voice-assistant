"""Калибровка порогов BLE-маячка: `room calibrate` собирает RSSI из сообщений room и предлагает пороги.

Колонка в режиме калибровки (ble_scan с cal_s) часто сканирует и шлёт room на каждый цикл:
`raw` — RSSI последнего пакета маячка в этом цикле (нет поля — маячок не услышан), `heard` — сколько пакетов.
Человек в это время лежит там, где обычно спит/сидит, с телефоном как обычно (на тумбочке, в руке).
Пороги выбираем так, чтобы почти все такие замеры были «рядом» с запасом, а «ушёл» лежал заметно ниже.
"""
from __future__ import annotations

NEAR_MIN, NEAR_MAX = -90, -45      # допустимый диапазон порога «рядом», дБм
FAR_GAP = 7                        # «ушёл» ниже «рядом» на столько дБм (гистерезис)
NEAR_MARGIN = 3                    # запас под 10-м процентилем замеров
MIN_SAMPLES = 5                    # меньше услышанных пакетов — калибровать не из чего


def percentile(sorted_vals: list[int], q: float) -> float:
    if not sorted_vals:
        raise ValueError("пусто")
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


class Calibration:
    """Накопитель замеров: add(msg) на каждое room, report() — итог и предложение."""

    def __init__(self) -> None:
        self.raw: list[int] = []
        self.cycles = 0

    def add(self, msg: dict) -> None:
        if msg.get("type") != "room":
            return
        self.cycles += 1
        raw = msg.get("raw")
        if isinstance(raw, int) and not isinstance(raw, bool) and -127 <= raw <= 20:
            self.raw.append(raw)

    def suggest(self) -> tuple[int, int] | None:
        """(near, far) в дБм или None, если замеров мало."""
        if len(self.raw) < MIN_SAMPLES:
            return None
        s = sorted(self.raw)
        near = round(percentile(s, 0.10)) - NEAR_MARGIN
        near = max(NEAR_MIN, min(NEAR_MAX, near))
        return near, near - FAR_GAP

    def report(self) -> str:
        if self.cycles == 0:
            return "калибровка: колонка не прислала ни одного room (BLE выключен или колонка отключена?)"
        if len(self.raw) < MIN_SAMPLES:
            return (f"калибровка: услышано пакетов {len(self.raw)} из {self.cycles} циклов — мало. Проверь, что на "
                    "телефоне включён BLE Transmitter, UUID верный (JARVIS_BLE_UUID) и ble on")
        s = sorted(self.raw)
        near, far = self.suggest() or (0, 0)
        miss = 100 - round(100 * len(self.raw) / max(self.cycles, 1))
        return (f"калибровка: пакетов {len(s)} из {self.cycles} циклов (пропусков {miss} %); RSSI мин {s[0]}, "
                f"10 % {round(percentile(s, 0.10))}, медиана {round(percentile(s, 0.5))}, макс {s[-1]} дБм. "
                f"Предлагаю: рядом > {near} дБм, ушёл < {far} дБм. Применить: room calibrate apply")
