"""Check whether pause length in long dictations supports a paragraph-break
threshold.

Reads vad_shadow.log's "300" bucket (finest-grained Silero segments, so the
gap between consecutive segments is a real measured pause of at least
0.3s) and reports the pause-length distribution for long toggle-mode
dictations, plus how many paragraph breaks a few candidate thresholds
would actually produce. Summary output only, run by hand.
"""

import json
import statistics
from pathlib import Path

BASE = Path(__file__).parent.parent
LONG_S = 45.0  # audio_s cutoff for the "long dictation" case this feature targets
ALT_CUTOFFS = (30.0, 60.0)
THRESHOLDS = (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)
HIST_BUCKETS = [(0, 0.5), (0.5, 1), (1, 1.5), (1.5, 2), (2, 2.5), (2.5, 3), (3, None)]


def load_records(path):
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


def gaps_300(rec):
    """Consecutive gap lengths (s) between the "300"-bucket segments."""
    segs = rec["segs"].get("300")
    if not segs or len(segs) < 2:
        return []
    return [next_seg[0] - prev_seg[1] for prev_seg, next_seg in zip(segs, segs[1:])]


def print_corpus_breakdown(recs):
    print(f"total lines: {len(recs)}")
    toggle = [r for r in recs if r["mode"] == "toggle"]
    ptt = [r for r in recs if r["mode"] == "ptt"]
    print(f"  toggle: {len(toggle)}, ptt: {len(ptt)}")
    for cutoff in sorted({LONG_S, *ALT_CUTOFFS}):
        long_toggle = [r for r in toggle if r["audio_s"] >= cutoff]
        long_ptt = [r for r in ptt if r["audio_s"] >= cutoff]
        tag = " (long-dictation cutoff used below)" if cutoff == LONG_S else ""
        print(f"  audio_s >= {cutoff:g}s: {len(long_toggle)} toggle, "
              f"{len(long_ptt)} ptt{tag}")


def print_gap_distribution(gap_lists):
    all_gaps = [g for gaps in gap_lists for g in gaps]
    # quantiles() needs two points, so a one-gap corpus can't be described
    if len(all_gaps) < 2:
        print(f"  too few gaps to describe ({len(all_gaps)})")
        return
    all_gaps.sort()
    qs = statistics.quantiles(all_gaps, n=100)
    p75, p90, p95 = qs[74], qs[89], qs[94]
    print(f"  n gaps: {len(all_gaps)} across {len(gap_lists)} dictations")
    print(f"  min={min(all_gaps):.2f}s  median={statistics.median(all_gaps):.2f}s  "
          f"p75={p75:.2f}s  p90={p90:.2f}s  p95={p95:.2f}s  max={max(all_gaps):.2f}s")
    print("  histogram:")
    for lo, hi in HIST_BUCKETS:
        if hi is None:
            n = sum(1 for g in all_gaps if g >= lo)
            label = f"{lo:g}s+"
        else:
            n = sum(1 for g in all_gaps if lo <= g < hi)
            label = f"{lo:g}-{hi:g}s"
        pct = n / len(all_gaps)
        print(f"    {label:>8}: {n:>5} ({pct:5.1%})")


def print_threshold_table(gap_lists):
    n_dictations = len(gap_lists)
    print(f"  {'threshold':>9} {'has_break':>10} {'mean_breaks':>12} {'median_breaks':>14}")
    for t in THRESHOLDS:
        counts = [sum(1 for g in gaps if g >= t) for gaps in gap_lists]
        has_any = sum(1 for c in counts if c > 0) / n_dictations
        mean_c = statistics.mean(counts)
        median_c = statistics.median(counts)
        print(f"  {t:>8.2f}s {has_any:>9.0%} {mean_c:>11.2f} {median_c:>13.1f}")


def print_longest_spotcheck(recs, n=8):
    no_300 = sum(1 for r in recs if "300" not in r["segs"])
    print(f"  ({no_300} of {len(recs)} records predate the \"300\" bucket and are "
          "excluded below)")
    with_300 = [r for r in recs if "300" in r["segs"]]
    longest = sorted(with_300, key=lambda r: r["audio_s"], reverse=True)[:n]
    for r in longest:
        gaps = [round(g, 2) for g in gaps_300(r)]
        print(f"  audio_s={r['audio_s']:.1f}  chars={r['chars']}  mode={r['mode']}  "
              f"ts={r['ts']}  gaps={gaps}")


def main():
    recs = load_records(BASE / "vad_shadow.log")
    if not recs:
        raise SystemExit("no shadow records - is vad_shadow.log present at the repo root?")

    print("=== corpus breakdown ===")
    print_corpus_breakdown(recs)

    long_toggle = [r for r in recs if r["mode"] == "toggle" and r["audio_s"] >= LONG_S]
    print(f"\n=== gap distribution, long toggle dictations (audio_s >= {LONG_S:g}s, "
          f"n={len(long_toggle)}) ===")
    gap_lists = [gaps_300(r) for r in long_toggle]
    print_gap_distribution(gap_lists)

    print(f"\n=== threshold candidates, long toggle dictations (n={len(long_toggle)}) ===")
    if long_toggle:
        print_threshold_table(gap_lists)
    else:
        print("  no long toggle dictations to evaluate")

    print(f"\n=== spot-check: longest dictations in the whole corpus ===")
    print_longest_spotcheck(recs)


if __name__ == "__main__":
    main()
