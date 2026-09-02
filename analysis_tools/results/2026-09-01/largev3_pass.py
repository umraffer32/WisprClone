"""large-v3 through the live app path (batched pipeline, same decode opts), then
compared against whisper_results.json (large-v3-turbo, same clips, same path)."""
import json, re, sys, time, tomllib, wave, random, difflib, statistics as st
from pathlib import Path
import numpy as np
BASE = Path(r"C:\Users\Uriah\Projects\WisprClone"); SP = Path(__file__).parent
sys.path.insert(0, str(BASE))
from transcribe import Status, Transcriber
def prog(m):
    with open(SP / "largev3_progress.log", "a", encoding="utf-8") as f: f.write(time.strftime("%H:%M:%S ") + m + "\n")
def load_wav(p):
    with wave.open(str(p), "rb") as w: raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
cfg = tomllib.load(open(BASE / "config.toml", "rb"))
cfg["model"]["name"] = "large-v3"
turbo = json.load(open(SP / "whisper_results.json", encoding="utf-8"))
wavs = [p for p in sorted((BASE / cfg["retain"]["dir"]).glob("*.wav")) if p.name in turbo]
out_path = SP / "whisper_results_large-v3.json"
results = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
t = Transcriber(cfg, BASE, None, Status()); t0 = time.perf_counter(); t._load_models()
prog(f"large-v3 loaded on {t.status.device} in {time.perf_counter()-t0:.0f}s (includes download if needed)")
assert t.status.device == "cuda"
run = lambda a: list(t.pipe.transcribe(a, **t.decode_opts)[0])
run(load_wav(wavs[0])); run(load_wav(wavs[-1]))
prog(f"large-v3 pass started on {len(wavs)} clips")
p0 = time.perf_counter()
for i, p in enumerate(wavs, 1):
    if p.name in results: continue
    a = load_wav(p); t1 = time.perf_counter(); segs = run(a); dt = time.perf_counter() - t1
    kept = [s for s in segs if s.no_speech_prob < 0.6]
    results[p.name] = {"text": " ".join(s.text.strip() for s in kept), "audio_s": round(len(a) / 16000, 3), "wall_s": round(dt, 4)}
    if i % 100 == 0:
        out_path.write_text(json.dumps(results, indent=0, ensure_ascii=False), encoding="utf-8")
        prog(f"large-v3: {i} of {len(wavs)} done, {time.perf_counter()-p0:.0f}s")
out_path.write_text(json.dumps(results, indent=0, ensure_ascii=False), encoding="utf-8")
prog(f"large-v3 pass finished: {len(results)} clips in {time.perf_counter()-p0:.0f}s")

# ---- compare ----
W = lambda s: re.findall(r"[\w']+", s.lower())
def dist(a, b):
    a, b = W(a), W(b); sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(max(i2-i1, j2-j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"), len(a)
rows = []
for n, r in results.items():
    tt = turbo[n]; d, la = dist(tt["text"], r["text"])
    rows.append(dict(name=n, audio=r["audio_s"], turbo=tt["text"], v3=r["text"], wt=tt["wall_s"], wv=r["wall_s"], d=d, la=la))
L = [f"large-v3 vs large-v3-turbo, {len(rows)} clips, both via BatchedInferencePipeline with the app's decode opts", ""]
tot = sum(r["d"] for r in rows); tw = sum(r["la"] for r in rows)
L.append(f"word disagreement: {tot} of {tw} turbo words ({100*tot/tw:.1f}%); clips identical after normalizing: {sum(r['d']==0 for r in rows)} ({100*sum(r['d']==0 for r in rows)/len(rows):.0f}%)")
for lo, hi in [(0, 10), (10, 30), (30, 999)]:
    b = [r for r in rows if lo <= r["audio"] < hi]
    if b: L.append(f"  {lo:>2}-{hi if hi<999 else '+':<3}s: {len(b):3d} clips, disagreement {100*sum(r['d'] for r in b)/max(1,sum(r['la'] for r in b)):.1f}%, wall median turbo {st.median(r['wt'] for r in b):.2f}s vs v3 {st.median(r['wv'] for r in b):.2f}s")
L.append(f"wall: turbo median {st.median(r['wt'] for r in rows):.2f}s p95 {sorted(r['wt'] for r in rows)[int(.95*len(rows))]:.2f}s | v3 median {st.median(r['wv'] for r in rows):.2f}s p95 {sorted(r['wv'] for r in rows)[int(.95*len(rows))]:.2f}s; total {sum(r['wt'] for r in rows):.0f}s vs {sum(r['wv'] for r in rows):.0f}s")
L.append(f"v3 slower on {sum(r['wv']>r['wt'] for r in rows)} of {len(rows)} clips; median extra {st.median(r['wv']-r['wt'] for r in rows):.2f}s")
def show(r):
    a, b = r["turbo"].split(), r["v3"].split(); sm = difflib.SequenceMatcher(None, W(r["turbo"]), W(r["v3"]), autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal": out.append(f"[{' '.join(W(r['turbo'])[i1:i2])} -> {' '.join(W(r['v3'])[j1:j2])}]")
    return f"  {r['name']} ({r['audio']:.0f}s): " + "; ".join(out[:6])
diff = [r for r in rows if r["d"]]
L += ["", f"largest disagreements ({min(15,len(diff))} of {len(diff)}), shown as [turbo -> v3]:"]
for r in sorted(diff, key=lambda r: -r["d"]/max(1,r["la"]))[:15]: L.append(show(r))
random.seed(3); L += ["", "random 20 disagreements:"]
for r in random.sample(diff, min(20, len(diff))): L.append(show(r))
vocab = ["wisprclone", "wispr", "claude", "ollama", "fable", "soq", "xeon", "qwen", "nexus", "parakeet", "whisper", "sonnet"]
L += ["", "vocabulary hits (turbo / v3):"]
for v in vocab:
    ct = sum(W(r["turbo"]).count(v) for r in rows); cv = sum(W(r["v3"]).count(v) for r in rows)
    if ct or cv: L.append(f"  {v:12s} {ct:3d} / {cv:3d}")
rep = "\n".join(L); (SP / "largev3_report.txt").write_text(rep, encoding="utf-8"); print(rep)
prog("DONE: largev3_report.txt written")
