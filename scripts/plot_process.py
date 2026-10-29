"""Plot the controller process image recorded by `make demo`.

Reads artifacts/demo_process.csv and writes docs/screenshots/process_timeline.png.
Needs matplotlib, which is deliberately not in the pinned project dependencies:

    python scripts/plot_process.py            # any Python with matplotlib installed
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "artifacts" / "demo_process.csv"
OUT = ROOT / "docs" / "screenshots" / "process_timeline.png"

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e1e0d9"
SERIES = "#2a78d6"  # categorical slot 1
CRITICAL = "#d03b3b"  # status: critical (alarm active), always labelled


def spans(t: list[float], flags: list[bool]) -> list[tuple[float, float]]:
    out, start = [], None
    for ti, f in zip(t, flags, strict=True):
        if f and start is None:
            start = ti
        elif not f and start is not None:
            out.append((start, ti))
            start = None
    if start is not None:
        out.append((start, t[-1]))
    return out


def main() -> int:
    rows = list(csv.DictReader(SRC.open(encoding="utf-8")))
    if not rows:
        print(f"no samples in {SRC.relative_to(ROOT).as_posix()}")
        return 1
    t = [float(r["process_s"]) for r in rows]
    p = [float(r["pressure_torr"]) for r in rows]
    temp = [float(r["temperature_c"]) for r in rows]
    sp = [float(r["temperature_sp_c"]) for r in rows]
    alarm = [int(r["alarm_word"]) != 0 for r in rows]
    fault = [int(r["faults"]) != 0 for r in rows]
    wafer = [int(r["wafer"]) for r in rows]

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "text.color": TEXT})
    fig, (ax_p, ax_t) = plt.subplots(2, 1, figsize=(12, 6.4), sharex=True, facecolor=SURFACE)
    for ax in (ax_p, ax_t):
        ax.set_facecolor(SURFACE)
        ax.grid(True, axis="y", color=GRID, linewidth=0.8)
        ax.tick_params(colors=TEXT_2, labelsize=9)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        for a, b in spans(t, alarm):
            ax.axvspan(a, b, color=CRITICAL, alpha=0.30, linewidth=0)
        for a, b in spans(t, fault):
            ax.axvspan(a, b, facecolor="none", edgecolor=TEXT_2, hatch="//", alpha=0.35, linewidth=0)

    ax_p.plot(t, p, color=SERIES, linewidth=2)
    ax_p.set_yscale("log")
    ax_p.set_ylabel("Chamber pressure [Torr]", color=TEXT_2)
    ax_p.axhline(0.05, color=TEXT_2, linewidth=1, linestyle=(0, (4, 3)))
    ax_p.text(t[-1], 0.058, "base pressure 0.05 Torr", ha="right", color=TEXT_2, va="bottom", fontsize=9)

    ax_t.plot(t, temp, color=SERIES, linewidth=2, label="chuck temperature")
    ax_t.plot(t, sp, color=TEXT_2, linewidth=1.2, linestyle=(0, (4, 3)), label="setpoint")
    ax_t.set_ylabel("Chuck temperature [°C]", color=TEXT_2)
    ax_t.set_xlabel("Process time [s] (controller clock, demo runs at x25)", color=TEXT_2)
    ax_t.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=2, frameon=False, fontsize=9)

    # wafer labels along the top of the pressure panel
    for w in sorted({x for x in wafer if x > 0}):
        ts = [ti for ti, wi in zip(t, wafer, strict=True) if wi == w]
        ax_p.text(
            (ts[0] + ts[-1]) / 2,
            1.04,
            f"wafer {w}",
            transform=ax_p.get_xaxis_transform(),
            ha="center",
            color=TEXT_2,
            fontsize=9,
        )
    for a, b in spans(t, alarm):
        ax_p.annotate(
            "ALID 1 set: pump-down timeout\n(cleared by RESUME)",
            xy=((a + b) / 2, 2.0),
            xytext=(16, 34),
            textcoords="offset points",
            ha="left",
            color=TEXT,
            fontsize=9,
            arrowprops={"arrowstyle": "-", "color": TEXT_2, "linewidth": 0.8},
        )
    for a, _ in spans(t, fault):
        ax_p.annotate(
            "vacuum leak injected\n(hatched: fault active)",
            xy=(a, 100),
            xytext=(-24, -46),
            textcoords="offset points",
            ha="right",
            color=TEXT,
            fontsize=9,
            arrowprops={"arrowstyle": "-", "color": TEXT_2, "linewidth": 0.8},
        )

    fig.suptitle(
        "gemsim demo - controller process image during LOT-0001",
        x=0.01,
        ha="left",
        fontsize=12,
        fontweight="bold",
        color=TEXT,
    )
    fig.text(
        0.01,
        0.925,
        f"{len(rows)} samples from artifacts/demo_process.csv, recorded every 0.1 s wall time"
        " and on every state or alarm change",
        fontsize=9,
        color=TEXT_2,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=150, facecolor=SURFACE)
    print(f"wrote {OUT.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
