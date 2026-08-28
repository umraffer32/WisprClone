"""Measure Ollama's real concurrency on polish-shaped requests.

Segment-parallel polish only pays if N simultaneous calls finish in about
one call's wall time. server.log says OLLAMA_NUM_PARALLEL:1, which would
serialize them; this measures instead of trusting the config line. Fires
the exact request shape transcribe._polish uses (model, temp 0, num_ctx
8192, num_predict cap) at 1..4 genuinely concurrent threads over distinct
equal-ish dictation lines from history.log, then plays the actual
Direction-B trade: one long transcript polished whole vs split in three
and fired concurrently. Summary numbers only. Optional argv[1] points at
a checkout holding config.toml + history.log (default: this directory).
"""

import re
import sys
import threading
import time
import tomllib
from pathlib import Path

import requests

from transcribe import POLISH_PROMPT

BASE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
_HISTORY_LINE = re.compile(r"^\[[^\]]+\] (.*)$")

_s = requests.Session()
_s.trust_env = False


def polish_call(cfg, text):
    p = cfg["polish"]
    t0 = time.monotonic()
    r = _s.post("http://127.0.0.1:11434/api/generate", json={
        "model": p["model"], "stream": False, "keep_alive": "24h",
        "prompt": POLISH_PROMPT + text,
        "options": {"temperature": 0, "num_ctx": 8192,
                    "num_predict": max(64, int(len(text) / 4 * p["max_ratio"]))},
    }, timeout=p["timeout_s"] * 4)  # queued calls stack behind each other
    r.raise_for_status()
    return time.monotonic() - t0


def concurrent(cfg, texts):
    """(wall_s, [per-call_s]) for len(texts) genuinely simultaneous calls."""
    durs = [None] * len(texts)
    go = threading.Barrier(len(texts))

    def work(i):
        go.wait()
        durs[i] = polish_call(cfg, texts[i])

    threads = [threading.Thread(target=work, args=(i,)) for i in range(len(texts))]
    t0 = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return time.monotonic() - t0, durs


def main():
    with open(BASE / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    lines = []
    for line in (BASE / "history.log").read_text(encoding="utf-8").splitlines():
        m = _HISTORY_LINE.match(line)
        if m and 200 <= len(m.group(1)) <= 400 and m.group(1) not in lines:
            lines.append(m.group(1))
    if len(lines) < 10:
        raise SystemExit("history.log too thin for the sweep")
    print(f"model={cfg['polish']['model']}, {len(lines)} candidate texts "
          f"(200-400 chars), warming...")
    polish_call(cfg, lines[0])

    singles = [polish_call(cfg, lines[i]) for i in (1, 2, 3)]
    single = sorted(singles)[1]
    print(f"single-call time (3 reps, 200-400 char texts): "
          f"{' '.join(f'{s:.2f}s' for s in singles)} -> med {single:.2f}s")

    print(f"\n{'n':>3} {'wall':>7} {'n*single':>9} {'speedup':>8}  per-call")
    used = 4
    for n in (2, 3, 4):
        texts = lines[used:used + n]
        used += n
        wall, durs = concurrent(cfg, texts)
        print(f"{n:>3} {wall:>6.2f}s {n * single:>8.2f}s {n * single / wall:>7.2f}x  "
              f"{' '.join(f'{d:.2f}s' for d in durs)}")

    long_lines = [m.group(1) for line in
                  (BASE / "history.log").read_text(encoding="utf-8").splitlines()
                  if (m := _HISTORY_LINE.match(line)) and len(m.group(1)) >= 700]
    if not long_lines:
        print("\nno 700+ char dictation in history.log for the whole-vs-split trade")
        return
    text = max(long_lines, key=len)
    # split in three at the sentence ends nearest the third points
    ends = [m.end() for m in re.finditer(r"[.!?] ", text)]
    cuts = [min(ends, key=lambda e: abs(e - len(text) * f)) if ends else
            int(len(text) * f) for f in (1 / 3, 2 / 3)]
    if cuts[0] >= cuts[1]:  # too few sentences - fall back to char thirds
        cuts = [len(text) // 3, 2 * len(text) // 3]
    parts = [text[:cuts[0]].strip(), text[cuts[0]:cuts[1]].strip(),
             text[cuts[1]:].strip()]
    print(f"\nDirection-B trade on a {len(text)}-char dictation "
          f"(parts {[len(p) for p in parts]} chars):")
    for rep in range(2):
        whole = polish_call(cfg, text)
        wall, durs = concurrent(cfg, parts)
        print(f"  rep{rep + 1}: whole {whole:.2f}s vs 3-way concurrent wall "
              f"{wall:.2f}s ({' '.join(f'{d:.2f}s' for d in durs)})")


if __name__ == "__main__":
    main()
