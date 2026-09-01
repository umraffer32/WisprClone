"""Does polishing pause-split pieces in isolation match whole-transcript polish?

Both latency bets lean on the answer: mine_streaming.py's felt-latency model
already books polish per-segment (each chunk polished while later speech
continues), and the segment-parallel-polish idea needs it outright. The
concrete risk: POLISH_PROMPT's restart-stammer rule ("there were, there
was") fixes text that straddles exactly the pause a splitter cuts at, and a
piece polished blind to the other side can't make that edit.

Replays each retained WAV at or over polish's min_audio_s through the real
pipeline both ways. Whisper's own segment timestamps mark pause boundaries
at GAP_S (the signal a live implementation would use - no separate Silero
pass); pieces forward-merge until they hold SHORT_S of speech, matching
mine_merge_rule.py's chunking. Control: clean_text per piece, join, one
_polish over the joined text. Treatment: the same cleaned pieces, _polish
each alone, join the outputs. Identical input text on both sides, so any
diff is polish scope, not segmentation. clean_text's own boundary artifacts
(capitalized piece starts, short-fragment period strips) are reported
separately against clean_text of the unsplit transcript. Word-level diffs
are localized to piece joins, guard fallbacks (min_ratio and friends) are
counted by piece length, and every diverging record gets the control
re-polished once so temp-0 GPU nondeterminism has its own measured floor.
Per-call timings feed the felt-latency correction. Optional argv[1] points
at a checkout holding config.toml + vad_shadow.log + retained_audio/
(default: the repo root).
"""

import difflib
import json
import re
import sys
import time
import tomllib
import wave
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))  # transcribe.py lives in the repo root
from transcribe import Status, Transcriber, clean_text

BASE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent
GAP_S = 0.5    # pause that opens a piece boundary; the mining front-runner
SHORT_S = 2.0  # a piece keeps absorbing until it holds this much speech
               # (mine_streaming's MERGE_S / mine_merge_rule's SHORT_S)
JOIN_WIN = 3   # a diff within this many words of a join is boundary-local
EXAMPLES = 12  # full-text examples printed; every flagged record still gets
               # its compact diff regions


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


def load_wav(path):
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def build_pieces(segs):
    """Whisper segments -> list of pieces (each a list of segments): split
    where the inter-segment gap reaches GAP_S, then forward-merge groups
    until a piece holds SHORT_S of speech. Only the tail can come up short."""
    groups, cur = [], [segs[0]]
    for s in segs[1:]:
        if s.start - cur[-1].end >= GAP_S:
            groups.append(cur)
            cur = [s]
        else:
            cur.append(s)
    groups.append(cur)
    pieces, cur, dur = [], [], 0.0
    for g in groups:
        cur += g
        dur += sum(s.end - s.start for s in g)
        if dur >= SHORT_S:
            pieces.append(cur)
            cur, dur = [], 0.0
    if cur:
        pieces.append(cur)
    return pieces


def _norm(tok):
    t = re.sub(r"[^\w']+", "", tok).lower()
    return t or tok


def diff_stats(ref_text, hyp_text, joins=()):
    """(divergence, opcodes, ref_words, hyp_words) - divergence is word-level
    edit distance over the difflib alignment, normalized by reference length,
    on case/punctuation-stripped words. opcodes keep only non-equal spans,
    each tagged boundary-local if it lands within JOIN_WIN words of a join
    position (a word index into hyp)."""
    ref_raw, hyp_raw = ref_text.split(), hyp_text.split()
    ref_n = [_norm(w) for w in ref_raw]
    hyp_n = [_norm(w) for w in hyp_raw]
    sm = difflib.SequenceMatcher(None, ref_n, hyp_n, autojunk=False)
    dist, ops = 0, []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        dist += max(i2 - i1, j2 - j1)
        local = any(j1 - JOIN_WIN <= p <= j2 + JOIN_WIN for p in joins)
        ops.append((tag, i1, i2, j1, j2, local))
    return dist / max(1, len(ref_n)), ops, ref_raw, hyp_raw


def window(words, a, b, pad=5):
    return " ".join(words[max(0, a - pad):b + pad])


def char_bucket(n):
    return "<50" if n < 50 else "50-149" if n < 150 else ">=150"


def stammer_boundaries(piece_texts):
    """Boundary indexes where the last word before the split reappears in
    the first three words after it - the cross-boundary restart shape the
    whole-polish prompt exists to catch."""
    hits = []
    for i in range(len(piece_texts) - 1):
        left = [_norm(w) for w in piece_texts[i].split()][-1:]
        right = [_norm(w) for w in piece_texts[i + 1].split()][:3]
        if left and left[0] in right:
            hits.append(i)
    return hits


def main():
    with open(BASE / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    rdir = BASE / cfg["retain"]["dir"]
    min_audio = cfg["polish"]["min_audio_s"]
    recs = load_records(BASE / "vad_shadow.log")
    usable = [r for r in recs
              if r.get("wav") and (rdir / r["wav"]).exists()
              and r["audio_s"] >= min_audio]
    if not usable:
        raise SystemExit("no retained WAVs at or over min_audio_s")
    print(f"candidates: {len(usable)} of {len(recs)} shadow records "
          f"(wav on disk, audio >= {min_audio}s)")

    t = Transcriber(cfg, BASE, None, Status())
    t._load_models()
    print(f"whisper={cfg['model']['name']} on {t.status.device}, "
          f"polish={cfg['polish']['model']}, gap>={GAP_S}s merge<{SHORT_S}s")

    timings = []  # (kind, chars_in, seconds, status)

    def polish(text, kind):
        t0 = time.monotonic()
        out, status = t._polish(text)
        timings.append((kind, len(text), time.monotonic() - t0, status))
        return out, status

    t0 = time.monotonic()
    rows, single_piece = [], 0
    for i, rec in enumerate(usable, 1):
        print(f"\r{i}/{len(usable)}", end="", flush=True)
        audio = load_wav(rdir / rec["wav"])
        # t.model, not t.pipe: this needs Whisper's own sentence-level segment
        # timestamps for the pause split; the live batched pipeline only
        # yields one segment per VAD chunk
        segs, _ = t.model.transcribe(audio, **t.decode_opts)
        segs = [s for s in segs if s.no_speech_prob < 0.6]
        if not segs:
            continue
        pieces = build_pieces(segs)
        piece_texts = [clean_text(" ".join(s.text.strip() for s in p),
                                  t.corrections, t.emphasis_words)
                       for p in pieces]
        piece_texts = [p for p in piece_texts if p]
        if len(piece_texts) < 2:
            single_piece += 1
            continue
        whole_clean = clean_text(" ".join(s.text.strip() for s in segs),
                                 t.corrections, t.emphasis_words)
        control_in = " ".join(piece_texts)
        clean_div = diff_stats(whole_clean, control_in)[0]

        ctrl, ctrl_status = polish(control_in, "whole")
        outs, statuses = [], []
        for p in piece_texts:
            out, status = polish(p, "piece")
            outs.append(out)
            statuses.append(status)
        seg = " ".join(outs)
        joins, pos = [], 0
        for out in outs[:-1]:
            pos += len(out.split())
            joins.append(pos)
        div, ops, ref_raw, hyp_raw = diff_stats(ctrl, seg, joins)
        noise = None
        if div > 0:
            ctrl2, _ = polish(control_in, "whole")
            noise = diff_stats(ctrl, ctrl2)[0]
        rows.append({
            "wav": rec["wav"], "audio_s": rec["audio_s"],
            "pieces": piece_texts, "control_in": control_in,
            "clean_div": clean_div, "ctrl": ctrl, "ctrl_status": ctrl_status,
            "outs": outs, "statuses": statuses, "div": div, "ops": ops,
            "ref_raw": ref_raw, "hyp_raw": hyp_raw, "joins": joins,
            "noise": noise, "stammers": stammer_boundaries(piece_texts)})
    print(f"\r{len(rows)} multi-piece records scored ({single_piece} single-piece "
          f"skipped) in {time.monotonic() - t0:.0f}s")
    if not rows:
        raise SystemExit("nothing to compare")

    n_pieces = [len(r["pieces"]) for r in rows]
    p_chars = [len(p) for r in rows for p in r["pieces"]]
    print(f"\npieces/record med {np.median(n_pieces):.0f} max {max(n_pieces)}; "
          f"piece chars med {np.median(p_chars):.0f} "
          f"p10 {np.percentile(p_chars, 10):.0f} p90 {np.percentile(p_chars, 90):.0f}")

    clean_divs = [r["clean_div"] for r in rows]
    print(f"clean_text boundary artifacts (split-clean vs whole-clean): "
          f"{np.mean([d > 0 for d in clean_divs]):.0%} of records differ, "
          f"med div {np.median(clean_divs):.3f} mean {np.mean(clean_divs):.3f}")

    diffed = [r for r in rows if r["div"] > 0]
    noises = [r["noise"] for r in diffed if r["noise"] is not None]
    print(f"\npolish scope (identical input both sides):")
    print(f"  records with any whole-vs-segment diff: {len(diffed)}/{len(rows)} "
          f"({len(diffed) / len(rows):.0%}), med div among them "
          f"{np.median([r['div'] for r in diffed]):.3f}" if diffed else
          "  no records diverged at all")
    if noises:
        print(f"  temp-0 rerun noise floor on those records: "
              f"{np.mean([n > 0 for n in noises]):.0%} nonzero, "
              f"med {np.median(noises):.3f} mean {np.mean(noises):.3f}")
    bl = [r for r in diffed
          if any(op[5] for op in r["ops"])]
    print(f"  records with a boundary-local diff: {len(bl)}/{len(rows)}")

    st_bounds = sum(len(r["stammers"]) for r in rows)
    st_diffed = sum(1 for r in rows for si in r["stammers"]
                    if any(op[5] and abs(op[3] - r["joins"][si]) <= JOIN_WIN + 2
                           for op in r["ops"]))
    all_joins = sum(len(r["joins"]) for r in rows)
    print(f"  cross-boundary repeat candidates: {st_bounds} of {all_joins} "
          f"joins; {st_diffed} of them sit inside a whole-vs-segment diff")

    whole_st = Counter(r["ctrl_status"] for r in rows)
    piece_st = Counter(s for r in rows for s in r["statuses"])
    print(f"\nguard fallbacks - whole: {dict(whole_st)}")
    print(f"guard fallbacks - piece: {dict(piece_st)}")
    bucket = Counter()
    bucket_n = Counter()
    for r in rows:
        for p, s in zip(r["pieces"], r["statuses"]):
            b = char_bucket(len(p))
            bucket_n[b] += 1
            if s not in ("ok",):
                bucket[b] += 1
    for b in ("<50", "50-149", ">=150"):
        if bucket_n[b]:
            print(f"  piece fallback rate {b} chars: "
                  f"{bucket[b]}/{bucket_n[b]} ({bucket[b] / bucket_n[b]:.0%})")

    for kind in ("whole", "piece"):
        pts = [(c, s) for k, c, s, st in timings if k == kind]
        if len(pts) >= 5:
            b, a = np.polyfit([c for c, _ in pts], [s for _, s in pts], 1)
            print(f"polish cost fit ({kind}, n={len(pts)}): "
                  f"{a:.2f}s + {b * 1000:.2f}ms/char, "
                  f"med {np.median([s for _, s in pts]):.2f}s")

    flagged = sorted(diffed, key=lambda r: -r["div"])
    print(f"\ndiff regions for all {len(flagged)} diverging records "
          f"(whole -> segmented; || marks a piece join in full texts):")
    for k, r in enumerate(flagged):
        print(f"\n[{r['wav']} audio={r['audio_s']}s pieces={len(r['pieces'])} "
              f"div={r['div']:.3f} noise={r['noise']} statuses={r['statuses']}]")
        for tag, i1, i2, j1, j2, local in r["ops"]:
            print(f"  {tag}{' @join' if local else '':<7} "
                  f"whole: ...{window(r['ref_raw'], i1, i2)}...\n"
                  f"  {'':<12}seg:   ...{window(r['hyp_raw'], j1, j2)}...")
        if k < EXAMPLES:
            print(f"  in:    {' || '.join(r['pieces'])}")
            print(f"  whole: {r['ctrl']}")
            print(f"  seg:   {' || '.join(r['outs'])}")


if __name__ == "__main__":
    main()
