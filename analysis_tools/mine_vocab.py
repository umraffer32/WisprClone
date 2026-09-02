"""Rank personal-vocabulary candidates from dictation history.

Reads history.log (and wispr_flow_history.txt if present), prints summary
tables only - no full transcripts - so the output is safe to share. The
curated result goes into the [model] prompt sentences in config.toml to bias Whisper
toward words it would otherwise mishear.
"""

import re
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).parent.parent

STOP = set("""
a about above actually after again all almost also always am an and any are
as at back bad be because been before being best better between big both but
by came can cannot come could day did different do does doing don done down
each end even every feel few find first for from get gets getting give go
goes going good got had has have having he her here hers him his how i if in
into is it its just keep kind know last left let like littlell long look
looking lot m made make makes making many may maybe me mean might more most
much must my need never new next no not now of off oh okay on one only or
other our out over own part people probably put re really right s said same
say see seems she should since so some something sort still such sure t take
than that the their them then there these they thing things think this those
through time to too try trying two up us use used using ve very want was way
we well went were what when where which while who why will with without won
work would yeah year years yes yet you your
""".split())


def words(text):
    return re.findall(r"[A-Za-z][A-Za-z0-9']*|\d[\w']*", text)


def mine(paths):
    cased = Counter()      # seen capitalized/mixed-case mid-sentence, or has digits
    lower = Counter()      # everything, case-folded
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            text = re.sub(r"^\[[^\]]*\]\s*", "", line).strip()
            prev_end = True  # sentence start: capitalization carries no signal
            for w in words(text):
                lower[w.lower()] += 1
                if (not prev_end and w[0].isupper()) or any(c.isdigit() for c in w) \
                        or (w[1:] != w[1:].lower() and len(w) > 2):
                    cased[w] += 1
                prev_end = w[-1:] in ".!?" or text.endswith(w)
            prev_end = True
    return cased, lower


def main():
    paths = [p for p in (BASE / "history.log", BASE / "wispr_flow_history.txt")
             if p.exists()]
    if not paths:
        sys.exit("no history files found")
    cased, lower = mine(paths)

    print(f"sources: {', '.join(p.name for p in paths)}")
    print(f"total words: {sum(lower.values())}, unique: {len(lower)}\n")

    print("cased/technical candidates (mid-sentence caps, digits, mixed case):")
    for w, n in cased.most_common(50):
        if n >= 2:
            print(f"  {n:4d}  {w}")

    print("\nfrequent lowercase non-common words:")
    shown = 0
    for w, n in lower.most_common():
        if w in STOP or len(w) < 4 or n < 4:
            continue
        print(f"  {n:4d}  {w}")
        shown += 1
        if shown >= 40:
            break


if __name__ == "__main__":
    main()
