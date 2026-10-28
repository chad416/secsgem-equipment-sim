"""Render captured demo output as PNG screenshots (Windows, headless Microsoft Edge).

Inputs are files written by a real run (nothing is typed in by hand):
  artifacts/demo_console.txt   console output of `make demo > artifacts/demo_console.txt 2>&1`
  artifacts/demo_trace.sml     SML trace of the same run

Outputs in docs/screenshots/:
  demo_console.png            start and end of the console output (middle lines elided, with a marker)
  trace_fault_recovery.png    trace sections 12-14: alarm, hold, RESUME, recovery

    python scripts/render_screenshots.py
"""

from __future__ import annotations

import html
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
OUT = ROOT / "docs" / "screenshots"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin: 0; background: #1a1a19; }}
.win {{ margin: 16px; border-radius: 8px; overflow: hidden; border: 1px solid #2c2c2a; }}
.bar {{ background: #2c2c2a; color: #c3c2b7; font: 13px Segoe UI, sans-serif; padding: 8px 12px; }}
pre {{ margin: 0; padding: 12px 14px; color: #f0efec; background: #1a1a19;
       font: 13px/1.35 Consolas, 'Cascadia Mono', monospace; white-space: pre-wrap; word-break: break-all; }}
.note {{ color: #9085e9; }} .warn {{ color: #eda100; }} .elide {{ color: #8a8983; font-style: italic; }}
.ok {{ color: #1baf7a; font-weight: bold; }}
</style></head><body><div class="win"><div class="bar">{title}</div><pre>{body}</pre></div></body></html>"""


def colour(line: str) -> str:
    esc = html.escape(line)
    if line.startswith("// ----"):
        return f'<span class="note">{esc}</span>'
    if " WARNING " in line:
        return f'<span class="warn">{esc}</span>'
    if line.startswith("[...") and line.endswith("...]"):
        return f'<span class="elide">{esc}</span>'
    if line.startswith("result") and "PASS" in line:
        return f'<span class="ok">{esc}</span>'
    return esc


def shoot(name: str, title: str, lines: list[str], width: int) -> None:
    # 13 px Consolas: ~7.2 px per character, 17.6 px per row; long lines wrap
    chars_per_row = (width - 70) // 7.2
    rows = sum(max(1, -(-len(x) // int(chars_per_row))) for x in lines)
    height = int(rows * 17.6) + 100
    page = PAGE.format(title=html.escape(title), body="\n".join(colour(x) for x in lines))
    # Persistent work dir: msedge.exe hands off to a background process and returns
    # before the screenshot exists, so a TemporaryDirectory would vanish under it.
    work = Path(tempfile.gettempdir()) / "gemsim_screenshots"
    work.mkdir(exist_ok=True)
    src = work / f"{name}.html"
    src.write_text(page, encoding="utf-8")
    out = OUT / f"{name}.png"
    out.unlink(missing_ok=True)
    cmd = [
        str(EDGE),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--user-data-dir={work / 'profile'}",
        f"--window-size={width},{height}",
        f"--screenshot={out}",
        src.as_uri(),
    ]
    subprocess.run(cmd, check=True, timeout=60, capture_output=True)
    deadline = time.monotonic() + 30
    last = -1
    while time.monotonic() < deadline:
        size = out.stat().st_size if out.exists() else -1
        if size > 0 and size == last:
            print(f"wrote {out} ({size} bytes)")
            return
        last = size
        time.sleep(0.5)
    raise RuntimeError(f"Edge did not write {out}")


def elide(lines: list[str], head: int, tail: int) -> list[str]:
    if len(lines) <= head + tail:
        return lines
    return [*lines[:head], f"[... {len(lines) - head - tail} lines omitted ...]", *lines[len(lines) - tail :]]


def section(trace: list[str], start: str, stop: str) -> list[str]:
    i = next(n for n, x in enumerate(trace) if x.startswith(start))
    j = next(n for n, x in enumerate(trace) if x.startswith(stop) and n > i)
    return trace[i:j]


def main() -> int:
    if not EDGE.exists():
        print(f"Microsoft Edge not found at {EDGE}")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    console = (ART / "demo_console.txt").read_text(encoding="utf-8").splitlines()

    def at(prefix: str) -> int:
        return next(n for n, x in enumerate(console) if x.startswith(prefix))

    # start-up, the lot start up to the recovery after RESUME, and the summary
    keep = [
        (0, at("// ---- 4.")),
        (at("// ---- 11."), at("// ---- 14.") + 12),
        (at("// ---- 16."), len(console)),
    ]
    shown: list[str] = []
    for n, (a, b) in enumerate(keep):
        if n:
            shown.append(f"[... {a - keep[n - 1][1]} lines omitted ...]")
        shown.extend(console[a:b])
    shoot("demo_console", "make demo  (console output, captured)", shown, 1500)
    trace = (ART / "demo_trace.sml").read_text(encoding="utf-8").splitlines()
    fault = section(trace, "// ---- 12.", "// ---- 15.")
    shoot("trace_fault_recovery", "artifacts/demo_trace.sml  (sections 12-14)", elide(fault, 118, 0), 900)
    return 0


if __name__ == "__main__":
    sys.exit(main())
