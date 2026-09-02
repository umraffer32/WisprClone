"""Sentinel prompt test: qwen3.5:9b over the 548 bake-off inputs with a prompt
that lets it answer NOCHANGE instead of retyping unchanged text. Compares
against out_qwen3.5_9b.json (same model, current prompt)."""
import json, re, sys, time, statistics as st
from pathlib import Path
import requests

BASE = Path(r"C:\Users\Uriah\Projects\WisprClone")
SP = Path(__file__).parent
sys.path.insert(0, str(BASE))
import transcribe as T

SENTINEL = "NOCHANGE"
PROMPT = T.POLISH_PROMPT.replace(
    "Output only the cleaned text - no preamble, no quotes, no explanation.",
    f"If the text needs no changes at all, output exactly {SENTINEL} and nothing "
    "else. Otherwise output only the cleaned text - no preamble, no quotes, no "
    "explanation.")
assert PROMPT != T.POLISH_PROMPT
MAX_RATIO = 2.5
inputs = json.load(open(SP / "inputs.json", encoding="utf-8"))
base = {r["id"]: r for r in json.load(open(SP / "out_qwen3.5_9b.json", encoding="utf-8"))["results"]}
prog = open(SP / "sentinel_progress.log", "a", encoding="utf-8")
def note(s):
    prog.write(time.strftime("%H:%M:%S ") + s + "\n"); prog.flush()

def ask(text):
    r = requests.post("http://127.0.0.1:11434/api/generate", json={
        "model": "qwen3.5:9b", "stream": False, "keep_alive": "24h", "think": False,
        "prompt": PROMPT + text,
        "options": {"temperature": 0, "num_ctx": 8192,
                    "num_predict": max(64, int(len(text) / 4 * MAX_RATIO))}}, timeout=60)
    r.raise_for_status(); return r.json()

ask("warm up warm up warm up.")  # warm, excluded
note(f"sentinel pass started on {len(inputs)} inputs")
out = []
t0 = time.time()
for i, it in enumerate(inputs, 1):
    text = it["text"]; tw = time.time(); j = ask(text); wall = time.time() - tw
    resp = j["response"].strip()
    out.append({"id": it["id"], "text": text, "resp": resp, "wall_s": round(wall, 3),
                "eval_count": j.get("eval_count"), "prompt_eval_count": j.get("prompt_eval_count")})
    if i % 100 == 0: note(f"{i} of {len(inputs)} done, {time.time()-t0:.0f}s")
note(f"pass finished: {len(inputs)} inputs in {time.time()-t0:.0f}s")
json.dump(out, open(SP / "out_sentinel_9b.json", "w", encoding="utf-8"), indent=1)

# ---- compare ----
def norm(s): return re.sub(r"\s+", " ", s).strip()
def wdist(a, b):
    import difflib
    a, b = a.split(), b.split(); sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ch = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
    return ch / max(1, len(a))
cats = {"both_noop": [], "sentinel_but_base_edited": [], "text_same_as_input": [],
        "edited_where_base_noop": [], "both_edited_same": [], "both_edited_differ": []}
for o in out:
    b = base[o["id"]]; inp = norm(o["text"]); bo = norm(b["out"]); so = norm(o["resp"])
    base_noop = bo == inp
    is_sent = so == SENTINEL or so.strip(".!\"' ") == SENTINEL
    o["is_sentinel"] = is_sent; o["base_noop"] = base_noop; o["base_wall_s"] = b["wall_s"]
    if is_sent: cats["both_noop" if base_noop else "sentinel_but_base_edited"].append(o)
    elif so == inp: cats["text_same_as_input"].append(o)
    elif base_noop: cats["edited_where_base_noop"].append(o)
    elif so == bo: cats["both_edited_same"].append(o)
    else: cats["both_edited_differ"].append(o)
lines = [f"sentinel test, qwen3.5:9b, {len(out)} inputs", ""]
for k, v in cats.items(): lines.append(f"{k:28s} {len(v):4d}")
sent = [o for o in out if o["is_sentinel"]]
bn = [base[o["id"]]["wall_s"] for o in out if o["base_noop"]]
lines += ["", f"sentinel answers: {len(sent)} ({100*len(sent)/len(out):.0f}%); baseline no-ops: {len(bn)} ({100*len(bn)/len(out):.0f}%)",
          f"sentinel wall median {st.median(o['wall_s'] for o in sent):.2f}s, p95 {sorted(o['wall_s'] for o in sent)[int(.95*(len(sent)-1))]:.2f}s"
          f" | same inputs under current prompt: median {st.median(o['base_wall_s'] for o in sent):.2f}s",
          f"all inputs: median wall {st.median(o['wall_s'] for o in out):.2f}s vs baseline {st.median(base[o['id']]['wall_s'] for o in out):.2f}s"
          f" (baseline median excludes nothing; its first 77 ran contended)",
          f"non-sentinel outputs: median wall {st.median(o['wall_s'] for o in out if not o['is_sentinel']):.2f}s"]
miss = cats["sentinel_but_base_edited"]
if miss:
    ds = sorted(wdist(norm(o["text"]), norm(base[o["id"]]["out"])) for o in miss)
    lines += ["", f"missed edits (sentinel where the current prompt edited): {len(miss)}; word-distance of the skipped edit: median {st.median(ds):.3f}, max {max(ds):.3f}",
              f"  skipped edits over 5% of words: {sum(d > .05 for d in ds)}", "  worst 12:"]
    for o in sorted(miss, key=lambda o: -wdist(norm(o["text"]), norm(base[o["id"]]["out"])))[:12]:
        lines += [f"    in : {o['text'][:220]}", f"    cur: {base[o['id']]['out'][:220]}", ""]
new = cats["edited_where_base_noop"]
if new:
    lines += ["", f"new edits where the current prompt left text alone: {len(new)}; first 8:"]
    for o in new[:8]: lines += [f"    in : {o['text'][:220]}", f"    new: {o['resp'][:220]}", ""]
dif = cats["both_edited_differ"]
if dif:
    lines += ["", f"both edited but differently: {len(dif)}; first 6:"]
    for o in dif[:6]: lines += [f"    in : {o['text'][:200]}", f"    cur: {base[o['id']]['out'][:200]}", f"    new: {o['resp'][:200]}", ""]
odd = [o for o in out if SENTINEL in o["resp"] and not o["is_sentinel"]]
lines.append(f"\noutputs containing the sentinel word inside other text: {len(odd)}")
for o in odd[:5]: lines.append(f"    {o['resp'][:200]}")
rep = "\n".join(lines)
(SP / "sentinel_report.txt").write_text(rep, encoding="utf-8")
json.dump(out, open(SP / "out_sentinel_9b.json", "w", encoding="utf-8"), indent=1)
note("DONE: report written to sentinel_report.txt")
print(rep)
