"""Whisper reference pass: every retained WAV through the live app path
(Transcriber.pipe.transcribe with Transcriber.decode_opts). Raw joined text,
same no_speech_prob filter as run(), per-clip wall time. Output JSON keyed by
filename. Warm-up call excluded from timing."""
import json, sys, time, tomllib, wave
from datetime import datetime
from pathlib import Path
import numpy as np

BASE = Path(r"C:\Users\Uriah\Projects\WisprClone")
SP = Path(__file__).parent
sys.path.insert(0, str(BASE))
from transcribe import Status, Transcriber  # noqa: E402

def prog(msg):
    with open(SP / "parakeet_progress.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")

def load_wav(p):
    with wave.open(str(p), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

with open(BASE / "config.toml", "rb") as f:
    cfg = tomllib.load(f)
wavs = sorted((BASE / cfg["retain"]["dir"]).glob("*.wav"))
out_path = SP / "whisper_results.json"
results = json.loads(out_path.read_text()) if out_path.exists() else {}

t = Transcriber(cfg, BASE, None, Status())
t0 = time.perf_counter()
t._load_models()
print(f"loaded on {t.status.device} in {time.perf_counter()-t0:.1f}s; opts={t.decode_opts}", flush=True)
assert t.status.device == "cuda", "whisper not on cuda"

def run(audio):
    segs, info = t.pipe.transcribe(audio, **t.decode_opts)
    segs = list(segs)
    return segs, info

# warm-up on a real clip, untimed
run(load_wav(wavs[0]))
run(load_wav(wavs[-1]))
prog(f"Whisper pass started on {len(wavs)} WAVs ({len(results)} already done from a previous run).")

pass_t0 = time.perf_counter()
done = 0
for i, p in enumerate(wavs, 1):
    if p.name in results:
        continue
    audio = load_wav(p)
    t1 = time.perf_counter()
    segs, info = run(audio)
    dt = time.perf_counter() - t1
    kept = [s for s in segs if s.no_speech_prob < 0.6]
    results[p.name] = {
        "text": " ".join(s.text.strip() for s in kept),
        "text_unfiltered": " ".join(s.text.strip() for s in segs),
        "n_segs": len(segs), "n_dropped": len(segs) - len(kept),
        "audio_s": round(len(audio) / 16000, 3), "wall_s": round(dt, 4),
        "avg_logprob": [round(s.avg_logprob, 3) for s in segs],
        "no_speech_prob": [round(s.no_speech_prob, 3) for s in segs],
        "compression_ratio": [round(s.compression_ratio, 3) for s in segs],
    }
    done += 1
    if done % 50 == 0:
        out_path.write_text(json.dumps(results, indent=0, ensure_ascii=False), encoding="utf-8")
    if i % 100 == 0:
        prog(f"Whisper pass: {i} of {len(wavs)} clips done, {time.perf_counter()-pass_t0:.0f}s elapsed.")
        print(i, flush=True)
out_path.write_text(json.dumps(results, indent=0, ensure_ascii=False), encoding="utf-8")
total = time.perf_counter() - pass_t0
prog(f"Whisper pass finished: {len(results)} clips in {total:.0f}s wall (sum of per-clip times {sum(r['wall_s'] for r in results.values()):.0f}s).")
print("done", total, flush=True)
