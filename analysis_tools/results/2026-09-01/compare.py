"""Whisper vs Parakeet over the retained dictation audio. Neither side is
ground truth; the metric is word-level disagreement (difflib alignment on
lowercased, punctuation-stripped words, normalized by Whisper's word count,
same as mine_merge_rule.score). history.log text is a rough third reference:
it is Whisper's own output after clean_text and (for >=8s clips) LLM polish,
so it can't judge a Whisper mishearing. corrections.txt is the user's own
record of Whisper mishearings, so its wrong-forms are counted per model."""
import difflib, json, random, re, sys, tomllib
from collections import Counter
from pathlib import Path
import numpy as np

SP = Path(__file__).parent
BASE = Path(r"C:\Users\Uriah\Projects\WisprClone")
sys.path.insert(0, str(BASE))
from transcribe import clean_text, _STUTTER, _RUNAWAY_REPEAT, _FILLER  # noqa: E402

W = json.loads((SP / "whisper_results.json").read_text(encoding="utf-8"))
P = json.loads((SP / "parakeet_results.json").read_text(encoding="utf-8"))
J = json.loads((SP / "join.json").read_text(encoding="utf-8"))
names = sorted(set(W) & set(P))
print(f"clips: whisper {len(W)}, parakeet {len(P)}, both {len(names)}")
with open(BASE / "config.toml", "rb") as f:
    cfg = tomllib.load(f)
CORR = BASE / cfg["files"]["corrections"]
EMPH = BASE / cfg["files"]["emphasis_words"]


def norm_words(text):
    out = []
    for tok in text.split():
        t = re.sub(r"[^\w']+", "", tok).lower().strip("'")
        if t:
            out.append(t)
    return out


def diverge(ref, hyp):
    """(edit distance in words, ref len, opcodes, ref words, hyp words)."""
    r, h = norm_words(ref), norm_words(hyp)
    sm = difflib.SequenceMatcher(None, r, h, autojunk=False)
    ops = [o for o in sm.get_opcodes() if o[0] != "equal"]
    dist = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in ops)
    return dist, len(r), ops, r, h


def bucket(s):
    return "<10s" if s < 10 else ("10-30s" if s <= 30 else ">30s")


rows = []
for n in names:
    w, p = W[n], P[n]
    dist, rlen, ops, r, h = diverge(w["text"], p["text"])
    j = J.get(n, {})
    rows.append({"name": n, "audio_s": w["audio_s"], "bucket": bucket(w["audio_s"]),
                 "w": w["text"], "p": p["text"], "dist": dist, "wlen": rlen, "plen": len(h),
                 "div": dist / max(1, rlen), "w_s": w["wall_s"], "p_s": p["wall_s"],
                 "hist": j.get("history"), "mode": j.get("mode"),
                 "polished": bool(j.get("polish_raw"))})


def pct(a, q):
    return float(np.percentile(a, q))


def stats(rs, label):
    d = np.array([r["div"] for r in rs])
    ws = np.array([r["w_s"] for r in rs])
    ps = np.array([r["p_s"] for r in rs])
    audio = sum(r["audio_s"] for r in rs)
    print(f"  {label:<8} n={len(rs):>3}  disagree med {np.median(d):.3f} mean {d.mean():.3f} "
          f"p90 {pct(d, 90):.3f}  identical {np.mean(d == 0) * 100:.0f}%  |  "
          f"whisper med {np.median(ws):.2f}s p95 {pct(ws, 95):.2f}s  "
          f"parakeet med {np.median(ps):.2f}s p95 {pct(ps, 95):.2f}s  |  audio {audio:.0f}s")


print("\n== Disagreement and latency (disagreement normalized by Whisper word count) ==")
stats(rows, "all")
for b in ("<10s", "10-30s", ">30s"):
    stats([r for r in rows if r["bucket"] == b], b)
tw = sum(r["w_s"] for r in rows)
tp = sum(r["p_s"] for r in rows)
ta = sum(r["audio_s"] for r in rows)
print(f"  total per-clip time: whisper {tw:.0f}s, parakeet {tp:.0f}s over {ta:.0f}s of audio "
      f"(RTF whisper {tw / ta:.3f}, parakeet {tp / ta:.3f})")
for k, lab in (("w_s", "whisper"), ("p_s", "parakeet")):
    x = np.array([r["audio_s"] for r in rows])
    y = np.array([r[k] for r in rows])
    b, a = np.polyfit(x, y, 1)
    print(f"  {lab}: wall ~= {a:.3f}s + {b:.4f}s per audio second (least squares)")
tot_w = sum(r["wlen"] for r in rows)
tot_p = sum(r["plen"] for r in rows)
tot_d = sum(r["dist"] for r in rows)
print(f"  word totals: whisper {tot_w}, parakeet {tot_p}; corpus-level disagreement {tot_d / tot_w:.3f}")
print(f"  empty outputs: whisper {sum(1 for r in rows if not r['w'].strip())}, "
      f"parakeet {sum(1 for r in rows if not r['p'].strip())}")
print(f"  whisper segments dropped by no_speech_prob>=0.6: {sum(W[n]['n_dropped'] for n in names)} "
      f"across {sum(1 for n in names if W[n]['n_dropped'])} clips")
faster = sum(1 for r in rows if r["p_s"] < r["w_s"])
print(f"  parakeet faster than whisper on {faster} of {len(rows)} clips")

print("\n== Formatting: punctuation and casing ==")


def has(rx, texts):
    return sum(1 for t in texts if re.search(rx, t))


ptexts = [r["p"] for r in rows if r["p"].strip()]
wtexts = [r["w"] for r in rows if r["w"].strip()]
for lab, rx in (("ends with .!?", r"[.!?]$"), ("any comma", r","), ("any capital", r"[A-Z]"),
                ("digit", r"\d"), ("question mark", r"\?"), ("ellipsis", r"\.\.\."),
                ("dollar sign", r"\$"), ("percent", r"%"), ("hyphenated", r"\w-\w")):
    print(f"  {lab:<14} whisper {has(rx, wtexts):>4}/{len(wtexts)}  parakeet {has(rx, ptexts):>4}/{len(ptexts)}")

print("\n== Third reference: history.log (Whisper output after clean_text + polish; Whisper-derived) ==")
for sub, lab in ((lambda r: True, "all"), (lambda r: not r["polished"], "no polish edit"),
                 (lambda r: r["polished"], "polish edited")):
    rs = [r for r in rows if r["hist"] and sub(r)]
    dw = [diverge(r["hist"], r["w"])[0] / max(1, len(norm_words(r["hist"]))) for r in rs]
    dp = [diverge(r["hist"], r["p"])[0] / max(1, len(norm_words(r["hist"]))) for r in rs]
    print(f"  {lab:<15} n={len(rs):>3}  whisper-vs-history med {np.median(dw):.3f} mean {np.mean(dw):.3f}   "
          f"parakeet-vs-history med {np.median(dp):.3f} mean {np.mean(dp):.3f}")
floor = [(n, J[n]["clean_raw"]) for n in names if J.get(n, {}).get("clean_raw")]
if floor:
    fd = [diverge(raw, W[n]["text"])[0] / max(1, len(norm_words(raw))) for n, raw in floor]
    print(f"  noise floor, live Whisper raw vs this offline Whisper pass: n={len(floor)} "
          f"med {np.median(fd):.3f} mean {np.mean(fd):.3f} identical {np.mean(np.array(fd) == 0) * 100:.0f}%")

print("\n== Personal vocabulary (count of clips containing each form) ==")
vocab = {
    "WisprClone": r"wispr ?clone", "whisper clone (miss)": r"whisper ?clone",
    "Wispr Flow": r"wispr ?flow", "whisper flow (miss)": r"whisper ?flow",
    "Claude": r"\bclaude\b", "clawed/clod/claud (miss)": r"\bclawed\b|\bclod\b|\bclaud\b|\bcloud\b",
    "ClaudeMD": r"claude ?md|clod ?md",
    "Ollama": r"\bollama\b", "olama/o lama/llama (miss)": r"\bolama\b|\bo lama\b|\bolamma\b|\ballama\b|\bo llama\b|\bllama\b",
    "Fable": r"\bfable\b", "SOQ": r"\bsoq\b|\bs\.o\.q\b", "so q/esoq (miss)": r"\bso ?q\b|\bs o q\b|\besoq\b|\bso queue\b",
    "Xeon": r"\bxeon\b", "Xion/Zion (miss)": r"\bxion\b|\bzion\b", "Codex": r"\bcodex\b",
    "Whisper": r"\bwhisper\b(?! ?(clone|flow))", "Hugging Face": r"hugging ?face", "GitHub": r"git ?hub",
    "Python": r"\bpython\b", "readme": r"\bread ?me\b", "CalCareers": r"cal ?careers", "Handy": r"\bhandy\b",
    "Sonnet": r"\bsonnet\b", "Opus": r"\bopus\b", "Grok": r"\bgrok\b|\bgrock\b", "hydralisk": r"hydralis",
    "Parakeet": r"parakeet", "VRAM": r"\bvram\b|\bv ram\b", "GPU": r"\bgpu\b", "LLM": r"\bllm\b|\bllms\b",
    "Nvidia": r"nvidia", "faster-whisper": r"faster.whisper", "Silero": r"silero", "onnx": r"onnx",
}
print(f"  {'form':<26} {'whisper':>8} {'parakeet':>9}")
for lab, rx in vocab.items():
    cw = sum(1 for r in rows if re.search(rx, r["w"], re.I))
    cp = sum(1 for r in rows if re.search(rx, r["p"], re.I))
    if cw or cp:
        print(f"  {lab:<26} {cw:>8} {cp:>9}")

print("\n== corrections.txt wrong-forms present in each model's raw output (clip counts) ==")
corr = []
for line in CORR.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        corr.append(line.split("=", 1)[0].strip())
for wrong in corr:
    rx = rf"\b{re.escape(wrong)}\b"
    cw = sum(1 for r in rows if re.search(rx, r["w"], re.I))
    cp = sum(1 for r in rows if re.search(rx, r["p"], re.I))
    if cw or cp:
        print(f"  {wrong:<22} whisper {cw:>4}  parakeet {cp:>4}")

print("\n== Profanity (clip counts; 'masked' = f***, f-word and the like) ==")
prof = {"fuck": r"\bfuck\w*", "shit": r"\bshit\w*", "damn": r"\bdamn\w*|\bgoddamn", "ass": r"\bass\b|\basshole",
        "bitch": r"\bbitch\w*", "hell": r"\bhell\b", "crap": r"\bcrap\w*", "dick": r"\bdick\b", "bastard": r"\bbastard",
        "masked": r"\b[a-z]\*{2,}|\bf-word|\bs-word|\bfrick|\beffing\b|\[bleep\]"}
for lab, rx in prof.items():
    cw = sum(1 for r in rows if re.search(rx, r["w"], re.I))
    cp = sum(1 for r in rows if re.search(rx, r["p"], re.I))
    if cw or cp:
        print(f"  {lab:<8} whisper {cw:>4}  parakeet {cp:>4}")
print("  clips where one model has profanity and the other doesn't:")
anyprof = "|".join(v for k, v in prof.items() if k != "masked")
for r in rows:
    a, b = bool(re.search(anyprof, r["w"], re.I)), bool(re.search(anyprof, r["p"], re.I))
    if a != b:
        print(f"    {r['name']} ({r['audio_s']:.1f}s)\n      W: {r['w']}\n      P: {r['p']}")

print("\n== Fillers and stammers (clip counts, raw output) ==")
fill = {"um/uh/hmm (_FILLER)": _FILLER, "you know": re.compile(r"\byou know\b", re.I),
        "like,": re.compile(r"\blike,", re.I), "stutter (_STUTTER)": _STUTTER,
        "runaway 4+ (_RUNAWAY_REPEAT)": _RUNAWAY_REPEAT, "so,/and,/but, opener": re.compile(r"^(so|and|but),", re.I)}
for lab, rx in fill.items():
    cw = sum(1 for r in rows if rx.search(r["w"]))
    cp = sum(1 for r in rows if rx.search(r["p"]))
    print(f"  {lab:<30} whisper {cw:>4}  parakeet {cp:>4}")
chg_w = sum(1 for r in rows if clean_text(r["w"], CORR, EMPH) != r["w"])
chg_p = sum(1 for r in rows if clean_text(r["p"], CORR, EMPH) != r["p"])
print(f"  clean_text() changes text on: whisper {chg_w} clips, parakeet {chg_p} clips")
print("  clips where whisper has a filler and parakeet has none (first 8):")
shown = 0
for r in rows:
    if _FILLER.search(r["w"]) and not _FILLER.search(r["p"]) and shown < 8:
        shown += 1
        print(f"    {r['name']}\n      W: {r['w']}\n      P: {r['p']}")

print("\n== Numbers, dollar amounts, times (clips where either output has a digit) ==")
num = [r for r in rows if re.search(r"\d", r["w"] + r["p"])]
wo = sum(1 for r in num if re.search(r"\d", r["w"]) and not re.search(r"\d", r["p"]))
po = sum(1 for r in num if re.search(r"\d", r["p"]) and not re.search(r"\d", r["w"]))
print(f"  {len(num)} clips; digits in whisper only {wo}, parakeet only {po}, both {len(num) - wo - po}")


def numeric_tokens(t):
    return re.findall(r"\$?\d[\d,.:]*\s?(?:%|am|pm|a\.m\.|p\.m\.|k|gb|mb|s|ms)?", t, re.I)


print("  differing numeric spans (whisper -> parakeet), up to 30:")
shown = 0
for r in num:
    a = [x.strip().lower() for x in numeric_tokens(r["w"])]
    b = [x.strip().lower() for x in numeric_tokens(r["p"])]
    if a != b and shown < 30:
        shown += 1
        print(f"    {r['name']}: {a} -> {b}")

print("\n== Hallucination-prone clips ==")
for n in ("20260828_160631.wav", "20260901_122359.wav"):
    if n in W and n in P:
        print(f"  {n} ({W[n]['audio_s']:.1f}s)  whisper {W[n]['wall_s']:.2f}s  parakeet {P[n]['wall_s']:.2f}s  "
              f"segs={W[n]['n_segs']} dropped={W[n]['n_dropped']}")
        print(f"    W: {W[n]['text']}\n    P: {P[n]['text']}\n    H: {J.get(n, {}).get('history')}")

print("\n== Largest disagreements (by words) ==")
top = sorted(rows, key=lambda r: -r["dist"])[:15]
for r in top:
    print(f"  {r['name']} ({r['audio_s']:.1f}s, {r['mode']}) dist={r['dist']} div={r['div']:.2f}"
          f"\n    W: {r['w']}\n    P: {r['p']}\n    H: {r['hist']}")
print("\n== Random sample of clips with any disagreement ==")
random.seed(7)
diff = [r for r in rows if r["dist"] > 0 and r not in top]
for r in random.sample(diff, min(12, len(diff))):
    print(f"  {r['name']} ({r['audio_s']:.1f}s, {r['mode']}) dist={r['dist']} div={r['div']:.2f}"
          f"\n    W: {r['w']}\n    P: {r['p']}\n    H: {r['hist']}")
print("\n== Random sample of identical clips ==")
same = [r for r in rows if r["dist"] == 0]
for r in random.sample(same, min(4, len(same))):
    print(f"  {r['name']} ({r['audio_s']:.1f}s)\n    W: {r['w']}\n    P: {r['p']}")

print("\n== Most common word substitutions (whisper -> parakeet) ==")
subs = Counter()
for r in rows:
    _, _, ops, rw, hw = diverge(r["w"], r["p"])
    for tag, i1, i2, j1, j2 in ops:
        if tag == "replace" and i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                subs[(rw[i1 + k], hw[j1 + k])] += 1
        elif tag == "delete":
            for k in range(i1, i2):
                subs[(rw[k], "<missing>")] += 1
        elif tag == "insert":
            for k in range(j1, j2):
                subs[("<missing>", hw[k])] += 1
for (a, b), c in subs.most_common(45):
    print(f"  {c:>3}  {a} -> {b}")
(SP / "compare_rows.json").write_text(json.dumps(rows, indent=0, ensure_ascii=False), encoding="utf-8")
