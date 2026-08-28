"""Pick the streaming segmenter's pause threshold from vad_shadow.log.

Reads the Phase A shadow records (segment bounds at each candidate
min_silence setting) plus wisprclone.log's job timing lines, prints summary
tables only. The felt-latency model is affine (whisper_s ~ a + b*audio_s):
whisper's cost is nearly flat up to ~28s of audio, so a naive per-second
rate would understate short-segment cost ~3x. Tuning stats use toggle
records only - PTT is just a single-segment sanity check.
"""

import json
import re
from collections import Counter
from pathlib import Path

import numpy as np

BASE = Path(__file__).parent
BLIP_S = 0.2        # segments shorter than this are mouth noise, not speech
MERGE_S = 2.0       # segments under this feed the merge-rule question
FORCE_CUT_S = 25.0  # segments past this would need a force-cut in streaming
MIN_POLISH_FIT = 5  # toggle records needed before the polish fit means anything

_JOB = re.compile(r"job: audio=([\d.]+)s whisper=([\d.]+)s polish=([\d.]+)s mode=(\w+)")


def load_shadow(path):
    recs = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return recs
    for line in lines:
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # torn final line from a shutdown mid-append
    return recs


def load_jobs(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [(float(a), float(w), float(p), m) for a, w, p, m in _JOB.findall(text)]


def affine_fit(xs, ys):
    """(a, b) for y ~ a + b*x."""
    b, a = np.polyfit(xs, ys, 1)  # highest power first
    return float(a), float(b)


def setting_row(recs, setting, whisper_ab, polish_ab):
    """Summary stats for one min_silence setting over toggle records."""
    wa, wb = whisper_ab
    counts, lasts, felts, wins, taxes, all_durs, blips = [], [], [], [], [], [], 0
    for rec in recs:
        if setting not in rec["segs"]:
            continue  # older record, predates this candidate setting
        durs = [e - s for s, e in rec["segs"][setting]]
        blips += sum(d < BLIP_S for d in durs)
        real = [d for d in durs if d >= BLIP_S]
        if not real:
            continue
        all_durs += real
        counts.append(len(real))
        lasts.append(real[-1])
        felt = wa + wb * real[-1]
        base = wa + wb * rec["audio_s"]  # today's pipeline: whisper the whole clip
        if polish_ab:
            pa, pb = polish_ab
            # polish is whole-transcript only (segment-parallel ruled out
            # 2026-08-27: Ollama serializes, blind pieces sever continuations),
            # so it starts after the last chunk and costs the same either way
            whole = pa + pb * rec["chars"]
            felt += whole
            base += whole
        felts.append(felt)
        wins.append(base - felt)
        taxes.append((len(real) - 1) * wa)  # extra fixed cost vs one whole-clip pass
    if not counts:
        return None
    return {"n": len(counts), "med_segs": np.median(counts),
            "one_seg": np.mean([c == 1 for c in counts]),
            "short": np.mean([d < MERGE_S for d in all_durs]),
            "long": np.mean([d > FORCE_CUT_S for d in all_durs]),
            "med_last": np.median(lasts), "med_felt": np.median(felts),
            "med_win": np.median(wins), "p90_win": np.percentile(wins, 90),
            "med_tax": np.median(taxes), "blips": blips}


def main():
    recs = load_shadow(BASE / "vad_shadow.log")
    jobs = load_jobs(BASE / "wisprclone.log")
    if not recs:
        raise SystemExit("no shadow records yet - is the streaming branch running?")

    print("records per day (a gap means the app ran off the streaming branch):")
    days = Counter(r["ts"][:10] for r in recs)
    for day in sorted(days):
        print(f"  {day}  {days[day]}")

    toggle = [r for r in recs if r["mode"] == "toggle"]
    ptt = [r for r in recs if r["mode"] == "ptt"]
    print(f"\ntoggle records (tuning set): {len(toggle)}, ptt (sanity): {len(ptt)}")
    if ptt:
        one = np.mean([len([1 for s, e in r["segs"]["700"] if e - s >= BLIP_S]) == 1
                       for r in ptt])
        print(f"ptt sanity: {one:.0%} single-segment at 700ms (expect ~100%)")

    whisper_ab = affine_fit([j[0] for j in jobs], [j[1] for j in jobs])
    print(f"\nwhisper cost model (n={len(jobs)} jobs): "
          f"{whisper_ab[0]:.2f}s + {whisper_ab[1]:.4f}/audio_s")
    pfit = [(r["chars"], r["polish_s"]) for r in toggle if r["polish_s"] > 0]
    polish_ab = None
    if len(pfit) >= MIN_POLISH_FIT:
        polish_ab = affine_fit([c for c, _ in pfit], [p for _, p in pfit])
        print(f"polish cost model (n={len(pfit)} records): "
              f"{polish_ab[0]:.2f}s + {polish_ab[1]:.4f}/char")
    else:
        print(f"polish cost model: only {len(pfit)} toggle records "
              f"(need {MIN_POLISH_FIT}) - felt latency below is whisper-only")

    if not toggle:
        raise SystemExit("no toggle records yet - the tuning table needs them")
    print(f"\nper-setting table, up to {len(toggle)} toggle records each "
          "(n differs per row - newer candidate settings have fewer records; "
          "felt = est. latency after you stop talking; win = felt saved vs "
          "today's whole-clip pipeline; tax = extra GPU s/dictation):")
    print(f"  {'ms':>5} {'n':>5} {'segs':>5} {'1seg':>5} {'<2s':>5} {'>25s':>5} "
          f"{'last':>6} {'felt':>6} {'win':>5} {'p90win':>6} {'tax':>5} {'blips':>5}")
    all_settings = sorted({s for r in toggle for s in r["segs"]}, key=int)
    for setting in all_settings:
        row = setting_row(toggle, setting, whisper_ab, polish_ab)
        if row is None:
            continue
        print(f"  {setting:>5} {row['n']:>5} {row['med_segs']:>5.1f} {row['one_seg']:>5.0%} "
              f"{row['short']:>5.0%} {row['long']:>5.0%} {row['med_last']:>5.1f}s "
              f"{row['med_felt']:>5.1f}s {row['med_win']:>4.1f}s {row['p90_win']:>5.1f}s "
              f"{row['med_tax']:>4.1f}s {row['blips']:>5}")


if __name__ == "__main__":
    main()
