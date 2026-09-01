"""Answer the streaming merge-rule question from retained dictation audio.

Replays the chunked pipeline streaming would run - Silero bounds from
vad_shadow.log at the front-runner min_silence setting, over each record
whose WAV survives in retained_audio/ - under three strategies. Mid-stream
they share one rule (a chunk keeps absorbing following segments until it
holds SHORT_S of speech); they differ only on a short trailing chunk, the
one case on the felt-latency path:

  fwd-accum     the tail is transcribed alone, bare. The baseline.
  tail-prompt   the tail gets the previous chunk's text as initial_prompt.
  tail-remerge  the tail is re-transcribed together with the previous
                chunk's audio.

Each strategy's concatenated output is scored by word-level divergence
(difflib alignment) against the full-clip transcript of the same WAV,
regenerated here with the same model and decode options so both sides share
whisper version and nondeterminism conditions. Raw whisper text on both
sides - no clean_text, no polish - so cleanup can't contaminate the
segmentation measurement. Parity with today's one-shot pipeline is the
metric, not absolute truth. Single-chunk records can't diverge from
chunking, so they're reported separately as the noise floor (padding + GPU
nondeterminism).
"""

import difflib
import json
import re
import sys
import time
import tomllib
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))  # transcribe.py lives in the repo root
from transcribe import Status, Transcriber

BASE = Path(__file__).parent.parent
SILENCE_MS = "500"  # front-runner per mine_streaming.py; must be a shadow candidate
SR = 16000
PAD_S = 0.2    # speech padding streaming would apply, so "alone" chunks aren't
               # handicapped by hard-cut onsets. 0.2 per side fits inside a
               # 0.5s min-silence gap without overlapping the neighbor.
SHORT_S = 2.0  # a chunk under this much speech is "short": mid-stream it keeps
               # absorbing, at the tail it's the case the strategies differ on
               # (matches mine_streaming's MERGE_S)
GAP_CAP_S = 1.0  # silence inside a chunk longer than this is elided: a
                 # multi-second dead stretch inside a transcription window is
                 # the hallucination bait trim_trailing_silence exists to cut,
                 # and real streaming would not ship it to the model either

STRATEGIES = ("fwd-accum", "tail-prompt", "tail-remerge")


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


def build_chunks(segs):
    """Forward-accumulate: a chunk absorbs following segments until it holds
    >= SHORT_S of speech. Only the final chunk can come up short - that's
    the tail case. Each chunk is a list of (start_s, end_s) segment spans."""
    chunks, cur, dur = [], [], 0.0
    for s, e in segs:
        cur.append((s, e))
        dur += e - s
        if dur >= SHORT_S:
            chunks.append(cur)
            cur, dur = [], 0.0
    if cur:
        chunks.append(cur)
    return chunks


def cut(audio, spans):
    """Chunk audio: padded segment slices, with internal gaps up to
    GAP_CAP_S kept as real audio and longer ones elided (each side still
    keeps its PAD_S, so an elided join retains ~0.4s of genuine room tone
    as a pause). After coalescing, remaining gaps exceed 2*PAD_S, so padded
    slices never overlap."""
    runs = [list(spans[0])]
    for s, e in spans[1:]:
        if s - runs[-1][1] <= GAP_CAP_S:
            runs[-1][1] = e
        else:
            runs.append([s, e])
    return np.concatenate(
        [audio[max(0, int((s - PAD_S) * SR)):min(len(audio), int((e + PAD_S) * SR))]
         for s, e in runs])


def whisper_text(model, opts, audio, prompt=None):
    segs, _ = model.transcribe(audio, initial_prompt=prompt, **opts)
    # same segment filter as the live pipeline
    return " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.6)


def simulate(model, opts, audio, chunks):
    """{strategy: [chunk texts]} plus whether the tail was the short case."""
    texts = [whisper_text(model, opts, cut(audio, c)) for c in chunks]
    tail_short = (len(chunks) > 1
                  and sum(e - s for s, e in chunks[-1]) < SHORT_S)
    out = {"fwd-accum": texts, "tail-prompt": texts, "tail-remerge": texts}
    if tail_short:
        out["tail-prompt"] = texts[:-1] + [
            whisper_text(model, opts, cut(audio, chunks[-1]), texts[-2] or None)]
        out["tail-remerge"] = texts[:-2] + [
            whisper_text(model, opts, cut(audio, chunks[-2] + chunks[-1]))]
    return out, tail_short


def _norm(tok):
    t = re.sub(r"[^\w']+", "", tok).lower()
    return t or tok


def score(ref_text, chunk_texts):
    """(divergence, joins, punct, onset, clip) for one strategy's output.
    Divergence is word-level edit distance over the difflib alignment,
    normalized by reference length, on case/punctuation-stripped words -
    so the artifact counts, not the divergence, are where join punctuation
    shows up. Per join: punct = the chunk ends with sentence punctuation
    the aligned full-clip word lacks (the "...went to. The store" artifact);
    onset = the first word after the join found no match (onset clipping or
    a join-induced mishear); clip = the last word before it found no match."""
    ref_raw = ref_text.split()
    hyp_chunks = [c.split() for c in chunk_texts]
    hyp_raw = [w for c in hyp_chunks for w in c]
    ref_n = [_norm(w) for w in ref_raw]
    hyp_n = [_norm(w) for w in hyp_raw]
    # autojunk would discard common words ("the") as junk on long dictations
    sm = difflib.SequenceMatcher(None, ref_n, hyp_n, autojunk=False)
    dist = sum(max(i2 - i1, j2 - j1)
               for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
    div = dist / max(1, len(ref_n))
    amap = {}  # hyp word index -> aligned ref word index
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            amap[j + k] = i + k
    joins = punct = onset = clip = 0
    pos = 0
    for c in hyp_chunks[:-1]:
        pos += len(c)
        if not c:
            continue  # chunk transcribed to nothing; no join to inspect
        joins += 1
        last, first = pos - 1, pos
        if last not in amap:
            clip += 1
        elif hyp_raw[last][-1] in ".!?" and ref_raw[amap[last]][-1] not in ".!?":
            punct += 1
        if first < len(hyp_raw) and first not in amap:
            onset += 1
    return div, joins, punct, onset, clip


def table(rows, label):
    print(f"\n{label} (n={len(rows)}):")
    print(f"  {'strategy':<12} {'med':>6} {'mean':>6} {'p90':>6} "
          f"{'joins':>6} {'punct':>6} {'onset':>6} {'clip':>5}")
    for s in STRATEGIES:
        divs = [r[s][0] for r in rows]
        j, p, o, c = (sum(r[s][i] for r in rows) for i in (1, 2, 3, 4))
        print(f"  {s:<12} {np.median(divs):>6.3f} {np.mean(divs):>6.3f} "
              f"{np.percentile(divs, 90):>6.3f} {j:>6} {p:>6} {o:>6} {c:>5}")


def main():
    with open(BASE / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    rdir = BASE / cfg["retain"]["dir"]
    recs = load_records(BASE / "vad_shadow.log")
    usable = [r for r in recs
              if r.get("wav") and (rdir / r["wav"]).exists()
              and r["segs"].get(SILENCE_MS)]
    if not usable:
        raise SystemExit("no shadow records with retained audio yet - "
                         "is [retain] enabled and the app running?")
    print(f"usable records: {len(usable)} of {len(recs)} shadow records")

    t = Transcriber(cfg, BASE, None, Status())
    t._load_models()
    model, opts = t.pipe, t.decode_opts  # the live path, so parity means parity
    print(f"model={cfg['model']['name']} device={t.status.device} "
          f"setting={SILENCE_MS}ms pad={PAD_S}s short<{SHORT_S}s gap-cap={GAP_CAP_S}s")

    t0 = time.monotonic()
    rows, empty_ref = [], 0
    for i, rec in enumerate(usable, 1):
        print(f"\r{i}/{len(usable)}", end="", flush=True)
        audio = load_wav(rdir / rec["wav"])
        ref = whisper_text(model, opts, audio)
        if not ref:
            empty_ref += 1
            continue
        chunks = build_chunks(rec["segs"][SILENCE_MS])
        sims, tail_short = simulate(model, opts, audio, chunks)
        row = {"n_chunks": len(chunks), "tail_short": tail_short}
        for name, texts in sims.items():
            row[name] = score(ref, texts)
        rows.append(row)
    print(f"\r{len(rows)} scored ({empty_ref} empty full-clip transcripts skipped) "
          f"in {time.monotonic() - t0:.0f}s")

    floor = [r for r in rows if r["n_chunks"] == 1]
    multi = [r for r in rows if r["n_chunks"] > 1]
    if floor:
        divs = [r["fwd-accum"][0] for r in floor]
        print(f"\nsingle-chunk noise floor (padding + GPU nondeterminism, "
              f"no joins; n={len(floor)}): med {np.median(divs):.3f} "
              f"mean {np.mean(divs):.3f}")
    if multi:
        table(multi, "multi-chunk records")
    tail = [r for r in multi if r["tail_short"]]
    if tail:
        table(tail, f"short-tail subset - the only case where the "
                    f"strategies actually differ")
    else:
        print("\nno short-tail records yet - the b-vs-c comparison needs them")


if __name__ == "__main__":
    main()
