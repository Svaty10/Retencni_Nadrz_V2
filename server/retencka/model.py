"""Pure calculations: packet decoding, calibration, volume, battery, consumption.

No I/O here – everything is unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional, Sequence

FLAG_BAT_ERR = 0x01
FLAG_SHT_ERR = 0x02
FLAG_PROBE_ERR = 0x04
FLAG_PROBE_CLIP = 0x08
FLAG_BAT_LOW = 0x10
FLAG_BOOT = 0x20
FLAG_WDT_RESET = 0x40
FLAG_FAST = 0x80

# diagnostic byte (uzel/include/config.h DIAG_*)
DIAG_LBT_MASK = 0x03          # listen-before-talk back-offs before the previous TX
DIAG_HEATED = 0x04            # SHT40 heater ran since the previous packet
DIAG_BOD_RESET = 0x08         # brown-out reset
DIAG_BAT_FRESH = 0x10         # battery measured in this cycle
DIAG_RADIO_REINIT = 0x20      # a TX failed, radio re-initialised
SAG_UNIT_MV = 5
SAG_UNKNOWN = 0xFF

FLAG_NAMES = {
    FLAG_BAT_ERR: "Chyba měření baterie",
    FLAG_SHT_ERR: "Chyba čidla SHT40",
    FLAG_PROBE_ERR: "Chyba měření sondy",
    FLAG_PROBE_CLIP: "Sonda mimo rozsah ADC",
    FLAG_BAT_LOW: "Vybitá baterie – úsporný režim",
    FLAG_BOOT: "Uzel restartován",
    FLAG_WDT_RESET: "Restart watchdogem",
    FLAG_FAST: "Rychlý režim (60 s)",
}

NODE_INTERVAL_S = 300
TEMP_INVALID = -32768
U16_INVALID = 0xFFFF


@dataclass
class Packet:
    flags: int
    node_id: int
    seq: int
    vbat_mv: Optional[int]
    temp_x100: Optional[int]
    hum_x100: Optional[int]
    probe_100uv: Optional[int]
    rssi_dbm: Optional[float] = None
    lqi: Optional[int] = None
    diag: Optional[int] = None
    vbat_sag_mv: Optional[int] = None


def flag_list(flags: int) -> list[str]:
    return [name for bit, name in FLAG_NAMES.items() if flags & bit and bit != FLAG_FAST]


# --------------------------------------------------------------------------- level
def column_cm(probe_mv: float, points: Sequence[Sequence[float]]) -> float:
    """Water column above the sensor [cm] by piecewise-linear interpolation.

    Outside the calibrated range the nearest segment is extrapolated (better than
    clamping – a slightly over-full tank must not read as "exactly full").
    One point = proportional through the origin. Points with the same voltage
    (only possible in a config that bypassed validation, e.g. an old backup) are
    merged into their mean, so a segment never has zero width.
    """
    merged: dict[float, list[float]] = {}
    for a, b in points:
        merged.setdefault(float(a), []).append(float(b))
    pts = sorted((x, sum(ys) / len(ys)) for x, ys in merged.items())
    if not pts:
        return 0.0
    if len(pts) == 1:
        mv0, cm0 = pts[0]
        return cm0 * probe_mv / mv0 if mv0 > 0 else 0.0
    if probe_mv <= pts[0][0]:
        (x0, y0), (x1, y1) = pts[0], pts[1]
    elif probe_mv >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
    else:
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= probe_mv <= x1:
                break
    return y0 + (probe_mv - x0) * (y1 - y0) / (x1 - x0)


@dataclass
class Level:
    column_cm: float
    level_cm: float
    percent: float
    volume_l: float
    max_volume_l: float


def tank_height_cm(s: dict) -> float:
    if s.get("tank_shape") == "cyl_h":
        return max(0.0, float(s.get("tank_diameter_cm") or 0.0))
    return max(0.0, float(s.get("max_depth_cm") or 0.0))


def _volume_and_max_l(level_cm: float, s: dict) -> tuple[float, float]:
    shape = s.get("tank_shape") or "area"
    if shape == "cyl_h":
        d = float(s.get("tank_diameter_cm") or 0.0)
        length = float(s.get("tank_length_cm") or 0.0)
        if d <= 0.0 or length <= 0.0:
            return 0.0, 0.0
        r = d / 2.0
        h = max(0.0, min(d, level_cm))
        u = max(-1.0, min(1.0, (r - h) / r)) if r > 0 else 1.0
        seg_cm2 = (r * r * math.acos(u) - (r - h) * math.sqrt(max(0.0, 2.0 * r * h - h * h))) if r > 0 else 0.0
        max_cm2 = math.pi * r * r
        return seg_cm2 * length / 1000.0, max_cm2 * length / 1000.0
    if shape == "cyl_v":
        d = float(s.get("tank_diameter_cm") or 0.0)
        max_d = float(s.get("max_depth_cm") or 0.0)
        if d <= 0.0 or max_d <= 0.0:
            return 0.0, 0.0
        h = max(0.0, min(max_d, level_cm))
        area_m2 = math.pi * ((d / 100.0) / 2.0) ** 2
        return area_m2 * h * 10.0, area_m2 * max_d * 10.0
    if shape == "rect":
        length = float(s.get("tank_length_cm") or 0.0)
        width = float(s.get("tank_width_cm") or 0.0)
        max_d = float(s.get("max_depth_cm") or 0.0)
        if length <= 0.0 or width <= 0.0 or max_d <= 0.0:
            return 0.0, 0.0
        h = max(0.0, min(max_d, level_cm))
        area_m2 = (length / 100.0) * (width / 100.0)
        return area_m2 * h * 10.0, area_m2 * max_d * 10.0
    max_d = float(s.get("max_depth_cm") or 0.0)
    area_m2 = float(s.get("tank_area_m2") or 0.0)
    if max_d <= 0.0 or area_m2 <= 0.0:
        return 0.0, 0.0
    h = max(0.0, min(max_d, level_cm))
    return area_m2 * h * 10.0, area_m2 * max_d * 10.0


def compute_level(probe_100uv: float, s: dict) -> Level:
    mv = probe_100uv / 10.0
    col = column_cm(mv, s["cal_points"])
    max_d = tank_height_cm(s)
    level = max(0.0, min(max_d, col + float(s["sensor_offset_cm"])))
    vol, max_vol = _volume_and_max_l(level, s)
    pct = vol / max_vol * 100.0 if max_vol > 0 else 0.0
    return Level(col, level, pct, vol, max_vol)


def level_from_mm(level_mm: float, s: dict) -> Level:
    """Level object from an already computed (filtered) height above the bottom."""
    max_d = tank_height_cm(s)
    level = max(0.0, min(max_d, level_mm / 10.0))
    vol, max_vol = _volume_and_max_l(level, s)
    pct = vol / max_vol * 100.0 if max_vol > 0 else 0.0
    return Level(level - float(s["sensor_offset_cm"]), level, pct, vol, max_vol)


def deadband_mm(s: dict) -> float:
    """Probe uncertainty: accuracy (% of full scale) x probe range, e.g. 0.5 % x 200 cm = 10 mm."""
    return max(1.0, float(s["probe_accuracy_pct"]) / 100.0 * float(s["probe_range_cm"]) * 10.0)


def deadband_l(s: dict) -> float:
    shape = s.get("tank_shape") or "area"
    if shape == "cyl_h":
        d = float(s.get("tank_diameter_cm") or 0.0)
        length = float(s.get("tank_length_cm") or 0.0)
        eff_area_m2 = (d / 100.0) * (length / 100.0)
    elif shape == "cyl_v":
        d = float(s.get("tank_diameter_cm") or 0.0)
        eff_area_m2 = math.pi * ((d / 100.0) / 2.0) ** 2
    elif shape == "rect":
        length = float(s.get("tank_length_cm") or 0.0)
        width = float(s.get("tank_width_cm") or 0.0)
        eff_area_m2 = (length / 100.0) * (width / 100.0)
    else:
        eff_area_m2 = float(s.get("tank_area_m2") or 0.0)
    eff_area_m2 = max(0.0, eff_area_m2)
    return deadband_mm(s) / 10.0 * eff_area_m2 * 10.0


class LevelFilter:
    """Removes probe noise without delaying real changes.

    1. median of the last 3 raw readings  -> single spikes disappear
    2. change smaller than 2x dead band   -> exponential smoothing (no "teeth" in charts)
       change larger                      -> follow immediately (refill, heavy rain)

    While refilling (follow=True) the value is the raw reading: the rise is real,
    and both smoothing and the median would lag behind it – the valve would close
    late (hundreds of litres over the target) and the lag would show up as
    "rain" after the refill ended.
    """

    ALPHA = 0.3

    def __init__(self) -> None:
        self.raw: list[float] = []
        self.value: Optional[float] = None

    def update(self, raw_mm: float, deadband: float, follow: bool = False) -> float:
        self.raw = (self.raw + [raw_mm])[-3:]
        if follow:
            self.value = raw_mm
            return self.value
        med = sorted(self.raw)[len(self.raw) // 2]
        if self.value is None or abs(med - self.value) > 2 * deadband:
            self.value = med
        else:
            self.value += self.ALPHA * (med - self.value)
        return self.value


def max_volume_l(s: dict) -> float:
    return _volume_and_max_l(tank_height_cm(s), s)[1]


# ------------------------------------------------------------------------- battery
# Li-FeS2 (Energizer L91 class) cell voltage at very light load (the node measures
# with the radio asleep) vs. remaining capacity. The curve is flat for most of the
# life and falls steeply at the end: 1.0 V/cell (3.0 V for 3 cells) = empty (0 %),
# 1.1 V/cell (3.3 V) ≈ 1 %. The percentage is indicative only.
_LIFES2_CELL = [
    (1.00, 0.0), (1.10, 1.0), (1.20, 3.0), (1.30, 7.0), (1.38, 13.0), (1.42, 22.0),
    (1.45, 35.0), (1.48, 50.0), (1.52, 65.0), (1.57, 80.0), (1.63, 92.0), (1.70, 98.0),
    (1.78, 100.0),
]
# Alkaline (LR6) cell at very light load: the voltage falls steadily over the whole
# life (about 1.30 V/cell at half capacity), so a Li-FeS2 curve would report "low"
# at half life. 1.0 V/cell is practically empty here too (the node's cut-off).
_ALKALINE_CELL = [
    (1.00, 0.0), (1.05, 2.0), (1.10, 5.0), (1.15, 10.0), (1.20, 20.0), (1.25, 35.0),
    (1.30, 50.0), (1.35, 65.0), (1.40, 78.0), (1.45, 88.0), (1.50, 95.0), (1.55, 99.0),
    (1.60, 100.0),
]
BATTERY_CURVES = {"lifes2": _LIFES2_CELL, "alkaline": _ALKALINE_CELL}
BATTERY_SELF_DISCHARGE = {"lifes2": 0.02, "alkaline": 0.03}   # share of capacity per year
BATTERY_LOW_PCT = 5.0
BATTERY_CUTOFF_MV = 3000          # node switches itself off below this (uzel/include/config.h)
QUICK_CHECK_FW = 11               # firmware from which the node checks the probe every minute
QUICK_CHECKS_SLOTS = 1440         # one wake-up per minute: a full cycle or a quick check


def battery_percent(vbat_v: float, cells: int = 3, kind: str = "lifes2") -> float:
    v = vbat_v / cells
    pts = BATTERY_CURVES.get(kind, _LIFES2_CELL)
    if v <= pts[0][0]:
        return 0.0
    if v >= pts[-1][0]:
        return 100.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= v <= x1:
            return y0 + (v - x0) * (y1 - y0) / (x1 - x0)
    return 0.0


def battery_days_left(percent: float, cycles_per_day: float, s: dict,
                      heats_per_day: float = 0.0, kind: str = "lifes2",
                      quick_checks: bool = False) -> Optional[float]:
    """Remaining runtime from an energy model (voltage trends of Li-FeS2 are useless).
    Uses the real measurement rate and the real heater use of the last days.
    quick_checks: the node (fw >= QUICK_CHECK_FW) wakes every minute, each minute
    without a transmission is a probe-only check."""
    checks = max(0.0, QUICK_CHECKS_SLOTS - cycles_per_day) if quick_checks else 0.0
    cap_key = "battery_capacity_alk_mah" if kind == "alkaline" else "battery_capacity_mah"
    # capacity of the tank's battery (settings.NODE_SCHEMA, inheritable); typical 3x AA as fallback
    nominal_mas = float(s.get(cap_key) or (2500.0 if kind == "alkaline" else 3000.0)) * 3600.0
    cap_mas = nominal_mas * percent / 100.0
    per_day = (cycles_per_day * float(s["node_cycle_mAs"])      # measure + transmit
               + checks * float(s.get("node_check_mAs", 1.4))   # quick probe checks
               + heats_per_day * float(s.get("node_heat_mAs", 66.0))   # SHT40 heater
               + float(s["node_sleep_uA"]) * 86.4               # sleep current
               + nominal_mas * BATTERY_SELF_DISCHARGE.get(kind, 0.02) / 365.0)   # self-discharge
    if per_day <= 0:
        return None
    return cap_mas / per_day


# --------------------------------------------------------------------- consumption
def flows(series: Sequence[tuple[int, float, int]], deadband_l: float):
    """Split a (ts, volume_l, refilling) series into consumption and inflow events.

    Uses a hysteresis reference instead of sample-to-sample differences, so slow
    consumption (smaller than the dead band per interval) is still counted.
    Yields (ts, kind, liters) with kind in {"use", "rain", "refill"}.
    """
    ref: Optional[float] = None
    for ts, vol, refilling in series:
        if vol is None:
            continue
        if ref is None:
            ref = vol
            continue
        if vol < ref - deadband_l:
            yield ts, "use", ref - vol
            ref = vol
        elif vol > ref + deadband_l:
            yield ts, ("refill" if refilling else "rain"), vol - ref
            ref = vol


def inflow_intervals(series: Sequence[tuple[int, float, int]], deadband_l: float,
                     merge_gap_s: int = 2700) -> list[tuple[int, int, float]]:
    """Periods when the tank filled by itself (rain), from a (ts, volume_l, refilling)
    series: rises above the dead band outside refilling. Start = last sample before
    the level started to rise, end = last rising sample; rises closer than
    merge_gap_s are one interval. Returns [(start_ts, end_ts, litres)]."""
    out: list[list] = []
    ref: Optional[float] = None
    rise_start: Optional[int] = None
    for ts, vol, refilling in series:
        if vol is None:
            continue
        if ref is None or refilling:
            ref, rise_start = vol, ts
            continue
        if vol <= ref + deadband_l / 3.0:
            rise_start = ts
            if vol < ref - deadband_l:
                ref = vol                      # consumption moves the reference down
            continue
        if vol > ref + deadband_l:
            gained = vol - ref
            start = rise_start if rise_start is not None else ts
            if out and start - out[-1][1] <= merge_gap_s:
                out[-1][1] = ts
                out[-1][2] += gained
            else:
                out.append([start, ts, gained])
            ref, rise_start = vol, ts
    return [(a, b, round(c)) for a, b, c in out]
