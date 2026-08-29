"""Audit the polish pass's editing behavior from wisprclone.log.

Reads every "polish changed text" raw/out pair and checks each against the
POLISH_PROMPT hard rules that have actually been broken before: a dropped
question (caught live 2026-08-23), sanitized profanity (the qwen swap,
2026-08-25), dropped/merged sentences, and suspicious shrink/growth.
Summary tables only, except flagged pairs, which print in full - an audit
is useless if the suspicious cases can't be read.

Two caveats on what the data can show. The raw side is post-clean_text(),
so the filler table counts what the regexes MISSED or deliberately left
(bare "you know", comma-separated repeats below 4x), not his habits before
cleanup.
And a pair only exists when polish's output was accepted - guard vetoes and
failures fall back to raw and log no diff - so the pair audit judges only
accepted edits; the polish_status table at the end covers how often polish
engaged, succeeded, or fell back across all jobs.
"""

import re
from collections import Counter
from pathlib import Path
from statistics import median

BASE = Path(__file__).parent.parent

# Audit bounds, deliberately tighter than the live min_ratio/max_ratio
# (0.4/2.5) guard: that one catches catastrophic misbehavior at paste time,
# while filler/stammer removal on real dictations rarely cuts more than a
# third - an accepted edit shrinking past that smells like summarizing.
RATIO_LO = 0.6
RATIO_HI = 1.5

_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ")
# polish_status= is optional: lines before 2026-08-26 predate the field
_JOB = re.compile(r"job: audio=([\d.]+)s whisper=[\d.]+s polish=([\d.]+)s "
                  r"mode=(\w+)(?: polish_status=(\w+))?")

# Suffix families so "fucking"/"bullshit"/"goddamn" count toward their stem;
# "hell" stays exact so "hello"/"shell" don't. Seeded from the swears the
# qwen incident actually sanitized plus common neighbors - count-based
# comparison means a swear polish kept can never flag, so a broader list
# only costs false positives on genuinely dropped non-swear words ("ass" in
# a dropped sentence), which the sentence check would flag anyway.
SWEARS = [(stem, re.compile(rf"\b{pat}\b", re.IGNORECASE)) for stem, pat in (
    ("fuck", r"\w*fuck\w*"), ("shit", r"\w*shit\w*"), ("damn", r"(?:god)?damn\w*"),
    ("ass", r"ass(?:es|hole\w*)?"), ("bitch", r"bitch\w*"), ("bastard", r"bastard\w*"),
    ("crap", r"crap\w*"), ("piss", r"piss\w*"), ("hell", r"hell"), ("dick", r"dick\w*"),
)]

# Residual fillers polish is asked to remove. um/uh/erm/hmm surviving into
# raw means _FILLER missed them; "you know"/"like" are the ones clean_text
# leaves on purpose unless comma-marked.
FILLERS = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in (
    ("um/uh/erm/hmm", r"(?<![\w-])(?:um+|uh+|erm|hmm+)(?![\w-])"),
    ("you know", r"\byou know\b"),
    (", like,", r",\s*like,"),
)]

_BARE_REPEAT = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)   # _STUTTER's target
_COMMA_REPEAT = re.compile(r"\b(\w+),\s+\1\b", re.IGNORECASE)  # protected below 4x
# closing quotes/brackets may sit between the punctuation and the space
# ('...say "genre mix." What...') - without them that period goes uncounted
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*(?:\s|$)")


def load_pairs(paths):
    """[{ts, raw, out, mode, status}] from raw/out diff blocks. The out text
    can span lines (the model emits paragraph breaks); everything up to the
    next timestamped line belongs to it. Blocks torn by rotation or a kill
    mid-write just fail the raw/out prefix checks and are skipped."""
    pairs = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        i = 0
        while i < len(lines):
            line = lines[i]
            if not (_TS.match(line) and line.endswith("polish changed text:")):
                i += 1
                continue
            if (i + 2 >= len(lines) or not lines[i + 1].startswith("  raw: ")
                    or not lines[i + 2].startswith("  out: ")):
                i += 1
                continue
            out_lines = [lines[i + 2][7:]]
            j = i + 3
            while j < len(lines) and not _TS.match(lines[j]):
                out_lines.append(lines[j])
                j += 1
            m = _JOB.search(lines[j]) if j < len(lines) else None
            pairs.append({"ts": line[:19], "raw": lines[i + 1][7:],
                          "out": "\n".join(out_lines).strip(),
                          "mode": m.group(3) if m else "?",
                          "status": m.group(4) if m else None})
            i = j
    return pairs


def load_jobs(paths):
    """(audio_s, polish_s, mode, status_or_None) for every job line."""
    jobs = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        jobs += [(float(a), float(p), m, s or None)
                 for a, p, m, s in _JOB.findall(text)]
    return jobs


def sentences(text):
    n = len(_SENTENCE_END.findall(text))
    # a trailing fragment with no closing punctuation is still a sentence
    if text and text.rstrip("\"')]")[-1:] not in (".", "!", "?"):
        n += 1
    return n


def check(pair):
    """Flag strings for one pair, empty when it looks clean."""
    raw, out = pair["raw"], pair["out"]
    flags = []
    if out.count("?") < raw.count("?"):
        flags.append(f"dropped question ({raw.count('?')} -> {out.count('?')})")
    for stem, rx in SWEARS:
        r, o = len(rx.findall(raw)), len(rx.findall(out))
        if o < r:
            flags.append(f"dropped swear '{stem}' ({r} -> {o})")
    sr, so = sentences(raw), sentences(out)
    if so < sr:
        flags.append(f"fewer sentences ({sr} -> {so})")
    ratio = len(out) / max(1, len(raw))
    if not RATIO_LO <= ratio <= RATIO_HI:
        flags.append(f"length ratio {ratio:.2f} outside [{RATIO_LO}, {RATIO_HI}]")
    return flags


def main():
    # .log.1 first (older) so pairs stay chronological once rotation happens
    paths = [p for p in (BASE / "wisprclone.log.1", BASE / "wisprclone.log")
             if p.exists()]
    if not paths:
        raise SystemExit("no wisprclone.log found in the repo root")
    pairs = load_pairs(paths)
    jobs = load_jobs(paths)
    print(f"sources: {', '.join(p.name for p in paths)}")
    print(f"raw/out pairs (accepted polish edits): {len(pairs)}")
    if not pairs:
        raise SystemExit("nothing to audit yet")

    flagged = [(p, check(p)) for p in pairs]
    flagged = [(p, f) for p, f in flagged if f]

    days = Counter(p["ts"][:10] for p in pairs)
    fdays = Counter(p["ts"][:10] for p, _ in flagged)
    print("\npairs per day (model: qwen through 8/24, dolphin-mistral 8/25-8/26, "
          "back to qwen 8/27+):")
    for day in sorted(days):
        print(f"  {day}  {days[day]:>4}  flagged {fdays.get(day, 0)}")

    by_rule = Counter(f.split(" (")[0] for _, fs in flagged for f in fs)
    print(f"\nflagged pairs: {len(flagged)} of {len(pairs)}")
    for rule, n in by_rule.most_common():
        print(f"  {n:>4}  {rule}")
    for p, fs in flagged:
        print(f"\n[{p['ts']}] mode={p['mode']}  " + "; ".join(fs))
        print(f"  raw: {p['raw']}")
        print(f"  out: {p['out']}")

    ratios = [len(p["out"]) / max(1, len(p["raw"])) for p in pairs]
    multiline = sum("\n" in p["out"] for p in pairs)
    print(f"\nlength ratio out/raw: median {median(ratios):.2f} "
          f"min {min(ratios):.2f} max {max(ratios):.2f}")
    print(f"outputs with model-added line breaks: {multiline} "
          "(pasted as-is - worth eyeballing if it grows)")

    print("\nresidual filler reaching polish (raw side is post-clean_text, "
          "so these are what the regexes missed or deliberately left):")
    for name, rx in FILLERS:
        n = sum(len(rx.findall(p["raw"])) for p in pairs)
        in_n = sum(bool(rx.search(p["raw"])) for p in pairs)
        print(f"  {n:>4}  {name}  (in {in_n} pairs)")
    bare = Counter(m.group(1).lower() for p in pairs
                   for m in _BARE_REPEAT.finditer(p["raw"]))
    comma = Counter(m.group(1).lower() for p in pairs
                    for m in _COMMA_REPEAT.finditer(p["raw"]))
    print("  bare word-repeats (_STUTTER should have caught these unless "
          "emphasis-protected):")
    for w, n in bare.most_common(10):
        print(f"    {n:>4}  {w} {w}")
    print("  comma-separated repeats (left alone below 4x by design; candidates "
          "for emphasis_words.txt or the polish stammer rule):")
    for w, n in comma.most_common(10):
        print(f"    {n:>4}  {w}, {w}")

    print(f"\npolish_status across all {len(jobs)} jobs:")
    tagged = Counter(s for *_, s in jobs if s)
    for status, n in tagged.most_common():
        print(f"  {n:>4}  {status}")
    old = [(a, p) for a, p, _, s in jobs if s is None]
    if old:
        ran = sum(p > 0 for _, p in old)
        print(f"  {len(old):>4}  (predate polish_status logging; "
              f"{ran} of those show polish time > 0)")


if __name__ == "__main__":
    main()
