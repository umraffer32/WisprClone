"""Replay the polish corpus through qwen2.5:3b-instruct to judge a downsize.

Every raw/out pair mine_polish.load_pairs() finds in wisprclone.log is a
dictation the live model (qwen2.5:7b through 8/24, dolphin-mistral 8/25-8/26,
qwen back from 8/27) actually edited. This re-sends each raw text to the 3b
model with transcribe._polish's exact prompt and options and scores it the
way the dolphin-vs-qwen replay was scored: no-op rate first (the number that
killed dolphin - 71% returned-unchanged vs qwen 7b's 31%), then
mine_polish.check()'s flag rules on every edit, then per-call latency, since
speed is the only reason to consider the smaller model at all.

max_ratio and timeout_s come from config.toml so they can't drift from the
live values. Per-call failures are counted and skipped, never fatal.
"""

import sys
import time
import tomllib
from collections import Counter
from pathlib import Path
from statistics import median

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))  # polish.py lives in the repo root
from mine_polish import check, load_pairs
from polish import POLISH_PROMPT

BASE = Path(__file__).parent.parent
MODEL = "qwen2.5:3b-instruct"

_s = requests.Session()
_s.trust_env = False


def main():
    with open(BASE / "config.toml", "rb") as f:
        p = tomllib.load(f)["polish"]
    paths = [q for q in (BASE / "wisprclone.log.1", BASE / "wisprclone.log")
             if q.exists()]
    if not paths:
        raise SystemExit("no wisprclone.log found in the repo root")
    pairs = load_pairs(paths)
    if not pairs:
        raise SystemExit("no polish pairs in the logs")
    print(f"sources: {', '.join(q.name for q in paths)}")
    print(f"corpus: {len(pairs)} raw dictations (accepted polish edits)")
    print(f"replaying through {MODEL} with _polish's prompt and options "
          f"(temp 0, num_ctx 8192, num_predict from max_ratio={p['max_ratio']}, "
          f"timeout {p['timeout_s']}s)")

    # cold load off disk takes far longer than a warm generation and would
    # count as a bogus timeout, so warm first the way _warm_polish does
    t0 = time.monotonic()
    _s.post("http://127.0.0.1:11434/api/generate", json={
        "model": MODEL, "stream": False, "keep_alive": "24h",
        "options": {"num_ctx": 8192},
    }, timeout=120).raise_for_status()
    print(f"model warm in {time.monotonic() - t0:.1f}s")

    failures = Counter()
    noops = 0
    edits = []  # (pair, out, flags)
    latencies = []
    t_run = time.monotonic()
    for i, pair in enumerate(pairs, 1):
        raw = pair["raw"]
        t = time.monotonic()
        try:
            r = _s.post("http://127.0.0.1:11434/api/generate", json={
                "model": MODEL, "stream": False, "keep_alive": "24h",
                "prompt": POLISH_PROMPT + raw,
                "options": {"temperature": 0, "num_ctx": 8192,
                            "num_predict": max(64, int(len(raw) / 4 * p["max_ratio"]))},
            }, timeout=p["timeout_s"])
            r.raise_for_status()
            out = r.json()["response"].strip()
        except requests.Timeout:
            failures["timeout"] += 1
            continue
        except requests.ConnectionError:
            failures["connection error"] += 1
            continue
        except requests.HTTPError as e:
            failures[f"http {e.response.status_code}"] += 1
            continue
        except Exception as e:
            failures[type(e).__name__] += 1
            continue
        latencies.append(time.monotonic() - t)
        if out == raw:
            noops += 1
        else:
            edits.append((pair, out, check({"raw": raw, "out": out})))
        if i % 50 == 0:
            print(f"  {i}/{len(pairs)} ({sum(failures.values())} failed, "
                  f"{noops} no-ops so far)")
    wall = time.monotonic() - t_run

    ok = len(latencies)
    print(f"\nattempted: {len(pairs)}, failed: {sum(failures.values())}")
    for reason, n in failures.most_common():
        print(f"  {n:>4}  {reason}")
    print(f"no-ops (output == input): {noops} of {ok} ({noops / max(1, ok):.0%})")

    flagged = [fs for *_, fs in edits if fs]
    by_rule = Counter(f.split(" (")[0] for fs in flagged for f in fs)
    print(f"\nflagged edits (mine_polish.check rules): {len(flagged)} of "
          f"{len(edits)} ({len(flagged) / max(1, len(edits)):.0%})")
    for rule, n in by_rule.most_common():
        print(f"  {n:>4}  {rule}")

    ratios = [len(out) / max(1, len(pair["raw"])) for pair, out, _ in edits]
    if ratios:
        print(f"\nlength ratio out/raw (edits only): median {median(ratios):.2f} "
              f"min {min(ratios):.2f} max {max(ratios):.2f}")
    if latencies:
        print(f"replay wall clock: {wall:.0f}s for {ok} calls - "
              f"mean {sum(latencies) / ok:.2f}s, median {median(latencies):.2f}s")

    print(f"\nbaseline: qwen2.5:7b-instruct no-opped 31% of the 405-case "
          f"2026-08-27 replay; {MODEL} here: {noops / max(1, ok):.0%}. 7b's "
          "whole-sentence-loss rate was 0.7% (3 of 405) by _lost_sentence's "
          "content-word check - the 'fewer sentences' count above is check()'s "
          "cruder punctuation-count flag, so compare it loosely, not one-to-one.")


if __name__ == "__main__":
    main()
