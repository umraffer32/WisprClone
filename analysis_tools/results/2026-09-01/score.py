"""Score the out_<model>.json files from polish_bakeoff.py. Re-runnable without
touching Ollama. Writes score_summary.json and prints the tables."""
import difflib, json, random, re, statistics, sys
from collections import Counter
from pathlib import Path

import numpy as np

BASE = Path(r"C:\Users\Uriah\Projects\WisprClone")
SP = Path(__file__).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "analysis_tools"))
import transcribe  # noqa: E402
from mine_polish import FILLERS, sentences  # noqa: E402

MODELS = ["qwen2.5:7b-instruct", "qwen3.5:4b", "qwen3.5:9b", "gemma4:e4b"]
HOTWORDS = {w.strip().lower() for w in "WisprClone, Wispr Flow, Claude, ClaudeMD, Fable, Ollama, SOQ".split(",")}
TECH = re.compile(r"[A-Za-z]+\d+\w*|\d+[A-Za-z]+\w*|\w+\.\w+|\w+_\w+|[A-Z]{2,}s?\b")
SOFTENED = re.compile(r"\b(?:f\*+\w*|s\*+t|\w\*{2,}\w*|freaking|frickin\w*|effing|heck|darn|dang|shoot|screw(?:ed|ing)?)\b", re.I)
WORD = re.compile(r"[A-Za-z0-9']+")
# stammers clean_text leaves alone on purpose: comma-separated word repeats
# below 4x ("the, the") and repeated short phrases ("I wanna, I wanna")
STAMMER = re.compile(r"\b([\w']+(?: [\w']+){0,2}),\s+\1\b", re.IGNORECASE)


def norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def words(s):
    return [w.lower().strip("'") for w in WORD.findall(s or "")]


def word_edit_distance(a, b):
    """Levenshtein over word tokens, normalized by the longer sequence."""
    if not a and not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1] / max(len(a), len(b))


def deleted_content_words(inp, out):
    """Words the model removed that are neither fillers/stop words nor a
    stammer duplicate (i.e. they don't survive anywhere in the output)."""
    a, b = words(inp), words(out)
    bset = set(b)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    gone = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            for w in a[i1:i2]:
                if w and w not in transcribe._GUARD_STOP and w not in bset and len(w) > 2:
                    gone.append(w)
    return gone


def added_content_words(inp, out):
    """Words in the output that appear nowhere in the input (the prompt's
    'add nothing' rule), ignoring stop words and short tokens."""
    aset = set(words(inp))
    return [w for w in words(out) if w and w not in aset and w not in transcribe._GUARD_STOP and len(w) > 2]


def load():
    inputs = {i["id"]: i for i in json.loads((SP / "inputs.json").read_text(encoding="utf-8"))}
    runs = {}
    for m in MODELS:
        p = SP / f"out_{m.replace(':', '_')}.json"
        if p.exists():
            runs[m] = json.loads(p.read_text(encoding="utf-8"))
    return inputs, runs


def pct(n, d):
    return f"{n}/{d} ({100 * n / max(1, d):.1f}%)"


def score_model(m, run, inputs):
    res = [r for r in run["results"] if r["id"] in inputs]
    ok = [r for r in res if not r["error"]]
    s = {"model": m, "n": len(res), "errors": Counter(r["error"] for r in res if r["error"]),
         "think": run.get("think"), "caps": run.get("capabilities"), "details": run.get("details"),
         "cold_load_s": run.get("cold_load_s"), "vram": run.get("vram_at_start"), "pass_wall_s": run.get("pass_wall_s")}
    s["thinking_leak"] = sum(1 for r in ok if r.get("thinking") or r.get("has_think_tag"))
    s["done_length"] = sum(1 for r in ok if r.get("done_reason") == "length")
    # guards
    g = Counter()
    for r in ok:
        for k in ("suspicious", "dropped_question", "dropped_profanity", "dropped_sentence"):
            g[k] += bool(r["guards"][k])
    s["guards_each"] = dict(g)
    s["status"] = Counter(r["guards"]["status"] for r in ok)
    s["rejected_examples"] = [
        {"id": r["id"], "status": r["guards"]["status"], "in": inputs[r["id"]]["text"], "out": r["out"],
         "lost": r["guards"].get("lost_sentence_text")}
        for r in ok if r["guards"]["status"] != "ok"]
    accepted = [r for r in ok if r["guards"]["status"] == "ok"]
    # no-op and edit distance (on accepted outputs = what would actually paste)
    s["noop"] = sum(1 for r in accepted if norm(r["out"]) == norm(inputs[r["id"]]["text"]))
    s["noop_ci"] = sum(1 for r in accepted if norm(r["out"]).lower() == norm(inputs[r["id"]]["text"]).lower())
    dists, sent_delta, num_chg, pn_chg, tech_chg = [], [], [], [], []
    delw = Counter()
    n_delw_inputs = 0
    addw = Counter()
    n_addw_inputs = 0
    emdash_added = semicolon_added = newline_added = 0
    filler_in = filler_removed = 0
    stammer_in = stammer_removed = 0
    for r in accepted:
        t, o = inputs[r["id"]]["text"], r["out"]
        d = word_edit_distance(words(t), words(o))
        r["_wed"] = d
        dists.append(d)
        sent_delta.append(sentences(o) - sentences(t))
        nin, nout = Counter(re.findall(r"\d+", t)), Counter(re.findall(r"\d+", o))
        if nin != nout:
            num_chg.append({"id": r["id"], "in": sorted((nin - nout).elements()), "out": sorted((nout - nin).elements())})
        # proper nouns: capitalized words not at sentence start
        pn = set()
        for sent in transcribe._SENT_SPLIT.split(t):
            for w in WORD.findall(sent)[1:]:
                if w[0].isupper() and w.lower() not in transcribe._GUARD_STOP and w != "I" and not w.startswith("I'"):
                    pn.add(w)
        lost_pn = [w for w in pn if w.lower() not in set(words(o))]
        if lost_pn:
            pn_chg.append({"id": r["id"], "lost": lost_pn})
        tech = {w for w in TECH.findall(t)} | {w for w in WORD.findall(t) if w.lower() in HOTWORDS}
        ol = o.lower()
        lost_t = [w for w in tech if w.lower() not in ol]
        if lost_t:
            tech_chg.append({"id": r["id"], "lost": lost_t})
        gone = deleted_content_words(t, o)
        if gone:
            n_delw_inputs += 1
            delw.update(gone)
            r["_gone"] = gone
        added = added_content_words(t, o)
        if added:
            n_addw_inputs += 1
            addw.update(added)
            r["_added"] = added
        emdash_added += (chr(0x2014) in o or " - " in o) and not (chr(0x2014) in t or " - " in t)
        semicolon_added += o.count(";") > t.count(";")
        newline_added += chr(10) in o
        si, so = len(STAMMER.findall(t)), len(STAMMER.findall(o))
        stammer_in += si
        stammer_removed += max(0, si - so)
        fi = sum(len(rx.findall(t)) for _, rx in FILLERS)
        fo = sum(len(rx.findall(o)) for _, rx in FILLERS)
        filler_in += fi
        filler_removed += max(0, fi - fo)
    s["accepted"] = len(accepted)
    if dists:
        s["wed"] = {"median": statistics.median(dists), "mean": statistics.mean(dists),
                    "p90": float(np.percentile(dists, 90)), "max": max(dists),
                    "gt_0.10": sum(d > 0.10 for d in dists), "gt_0.25": sum(d > 0.25 for d in dists)}
    s["sent_fewer"] = sum(1 for d in sent_delta if d < 0)
    s["sent_more"] = sum(1 for d in sent_delta if d > 0)
    s["num_changed"] = num_chg
    s["proper_noun_lost"] = pn_chg
    s["tech_lost"] = tech_chg
    s["deleted_content_words_inputs"] = n_delw_inputs
    s["deleted_content_words_top"] = delw.most_common(25)
    s["added_content_words_inputs"] = n_addw_inputs
    s["added_content_words_top"] = addw.most_common(25)
    s["emdash_added"] = emdash_added
    s["semicolon_added"] = semicolon_added
    s["newline_added"] = newline_added
    s["stammer_in"] = stammer_in
    s["stammer_removed"] = stammer_removed
    s["filler_in"] = filler_in
    s["filler_removed"] = filler_removed
    # profanity (all ok outputs, not just accepted - the guard is one of the scores)
    prof = [r for r in ok if transcribe._SWEARS.search(inputs[r["id"]]["text"].lower())]
    kept = [r for r in prof if not r["guards"]["dropped_profanity"]]
    verbatim = [r for r in kept if Counter(transcribe._SWEARS.findall(inputs[r["id"]]["text"].lower()))
                == Counter(transcribe._SWEARS.findall(r["out"].lower()))]
    soft = [r for r in prof if SOFTENED.search(r["out"]) and not SOFTENED.search(inputs[r["id"]]["text"])]
    s["profanity"] = {"inputs_with_swears": len(prof), "kept_all_counts": len(kept), "exact_same_counts": len(verbatim),
                      "softened_or_asterisked": len(soft),
                      "dropped_examples": [{"id": r["id"], "in": inputs[r["id"]]["text"], "out": r["out"]}
                                           for r in prof if r["guards"]["dropped_profanity"]]}
    # latency (exclude the first request of the pass: cache/load effects)
    # qwen3.5:9b's first 76 requests ran with three models resident and the GPU
    # at 15.9/16.4 GB (19 tok/s vs 44 after the unload) - contaminated latency,
    # outputs kept. Index 77 is the first request timed under normal conditions.
    skip = {"qwen3.5:9b": 77}.get(m, 1)
    s["latency_excluded_first_n"] = skip
    lat = [r for r in ok[skip:]]
    walls = [r["wall_s"] for r in lat]
    s["latency"] = {
        "n": len(walls), "median": statistics.median(walls), "p95": float(np.percentile(walls, 95)),
        "mean": statistics.mean(walls), "max": max(walls),
        "first_request_wall_s": ok[0]["wall_s"] if ok else None,
        "first_request_load_s": (ok[0].get("load_ns") or 0) / 1e9 if ok else None,
        "gen_tok_s": sum(r["eval_count"] for r in lat) / (sum(r["eval_ns"] for r in lat) / 1e9),
        "prompt_tok_s": sum(r["prompt_eval_count"] for r in lat) / max(1e-9, sum(r["prompt_eval_ns"] for r in lat) / 1e9),
        "median_prompt_eval_count": statistics.median(r["prompt_eval_count"] for r in lat),
        "median_eval_count": statistics.median(r["eval_count"] for r in lat),
        "loads_mid_pass": sum(1 for r in lat if (r.get("load_ns") or 0) > 0.5e9),
    }
    chars = np.array([len(inputs[r["id"]]["text"]) for r in lat])
    w = np.array(walls)
    a, b = np.polyfit(chars, w, 1)
    s["latency"]["fit_chars"] = {"slope_s_per_100chars": a * 100, "intercept_s": b,
                                 "r2": float(np.corrcoef(chars, w)[0, 1] ** 2),
                                 "pred_170ch": a * 170 + b, "pred_350ch": a * 350 + b, "pred_1000ch": a * 1000 + b}
    aud = [(inputs[r["id"]]["audio_s"], r["wall_s"]) for r in lat if inputs[r["id"]]["audio_s"]]
    if len(aud) > 10:
        x = np.array([q[0] for q in aud]); y = np.array([q[1] for q in aud])
        a2, b2 = np.polyfit(x, y, 1)
        s["latency"]["fit_audio"] = {"slope_s_per_10s_audio": a2 * 10, "intercept_s": b2,
                                     "r2": float(np.corrcoef(x, y)[0, 1] ** 2),
                                     "pred_10s": a2 * 10 + b2, "pred_30s": a2 * 30 + b2, "pred_60s": a2 * 60 + b2}
    return s


def main():
    inputs, runs = load()
    summary = {}
    for m, run in runs.items():
        summary[m] = score_model(m, run, inputs)
    # cross-model agreement and disagreement ranking
    by_id = {}
    for m, run in runs.items():
        for r in run["results"]:
            by_id.setdefault(r["id"], {})[m] = r
    base = MODELS[0]
    for m in runs:
        if m == base:
            continue
        same = sum(1 for i, d in by_id.items() if base in d and m in d and d[base].get("out") is not None
                   and norm(d[base]["out"]) == norm(d[m].get("out")))
        n = sum(1 for d in by_id.values() if base in d and m in d)
        summary[m]["same_as_baseline"] = f"{same}/{n}"
    disagreement = []
    for i, d in by_id.items():
        outs = [norm(d[m]["out"]) for m in runs if m in d and d[m].get("out")]
        if len(outs) < len(runs) or len(outs) < 2:
            continue
        ws = [words(o) for o in outs]
        dmax = max(word_edit_distance(ws[a], ws[b]) for a in range(len(ws)) for b in range(a + 1, len(ws)))
        disagreement.append((dmax, i))
    disagreement.sort(reverse=True)
    top = [i for _, i in disagreement[:20]]
    rest = [i for _, i in disagreement[20:]]
    random.Random(7).shuffle(rest)
    sample_ids = top + rest[:10]
    labels = list(runs)
    random.Random(2026).shuffle(labels)
    key = dict(zip("ABCD", labels))
    lines = []
    for n, i in enumerate(sample_ids, 1):
        inp = inputs[i]
        src = f"{inp['src']}" + (f", {inp['audio_s']:.0f}s audio" if inp["audio_s"] else f", live-polished {inp['ts'][:10]}")
        lines.append(f"### Sample {n} ({i}, {src}, disagreement {dict(disagreement)[i] if False else next(dm for dm, ii in disagreement if ii == i):.2f})\n")
        lines.append(f"**Input:** {inp['text']}\n")
        for L in sorted(key):
            m = key[L]
            r = by_id[i][m]
            flag = "" if r["guards"]["status"] == "ok" else f" [guard: {r['guards']['status']}]"
            same = " (= input)" if norm(r["out"]) == norm(inp["text"]) else ""
            lines.append(f"- **{L}**{flag}{same}: {r['out']}")
        lines.append("")
    (SP / "side_by_side.md").write_text("\n".join(lines) + "\n\nKey: " + ", ".join(f"{L} = {m}" for L, m in key.items()) + "\n", encoding="utf-8")
    (SP / "score_summary.json").write_text(json.dumps(summary, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    (SP / "blind_key.json").write_text(json.dumps(key), encoding="utf-8")

    # print tables
    def row(label, f):
        print(f"{label:<34}" + "".join(f"{str(f(summary[m])):>22}" for m in runs))
    print(f"{'':<34}" + "".join(f"{m:>22}" for m in runs))
    row("n / errors", lambda s: f"{s['n']} / {sum(s['errors'].values())}")
    row("thinking leaked", lambda s: s["thinking_leak"])
    row("hit num_predict (length)", lambda s: s["done_length"])
    for k in ("suspicious", "dropped_question", "dropped_profanity", "dropped_sentence"):
        row(f"guard {k}", lambda s, k=k: s["guards_each"].get(k, 0))
    row("rejected (any guard)", lambda s: s["n"] - sum(s["errors"].values()) - s["accepted"])
    row("accepted", lambda s: s["accepted"])
    row("no-op (exact, ws-normalized)", lambda s: s["noop"])
    row("no-op (case-insensitive)", lambda s: s["noop_ci"])
    row("word edit dist median", lambda s: f"{s['wed']['median']:.3f}")
    row("word edit dist mean", lambda s: f"{s['wed']['mean']:.3f}")
    row("word edit dist p90", lambda s: f"{s['wed']['p90']:.3f}")
    row("word edit dist max", lambda s: f"{s['wed']['max']:.3f}")
    row("edits > 0.10", lambda s: s["wed"]["gt_0.10"])
    row("edits > 0.25", lambda s: s["wed"]["gt_0.25"])
    row("fewer sentences (accepted)", lambda s: s["sent_fewer"])
    row("more sentences (accepted)", lambda s: s["sent_more"])
    row("number changed", lambda s: len(s["num_changed"]))
    row("proper noun lost", lambda s: len(s["proper_noun_lost"]))
    row("tech term lost", lambda s: len(s["tech_lost"]))
    row("inputs w/ deleted content words", lambda s: s["deleted_content_words_inputs"])
    row("inputs w/ added content words", lambda s: s["added_content_words_inputs"])
    row("em dash added", lambda s: s["emdash_added"])
    row("semicolon added", lambda s: s["semicolon_added"])
    row("paragraph breaks added", lambda s: s["newline_added"])
    row("comma stammers in / removed", lambda s: f"{s['stammer_in']} / {s['stammer_removed']}")
    row("residual fillers in / removed", lambda s: f"{s['filler_in']} / {s['filler_removed']}")
    row("swear inputs", lambda s: s["profanity"]["inputs_with_swears"])
    row("  kept all counts", lambda s: s["profanity"]["kept_all_counts"])
    row("  exact same counts", lambda s: s["profanity"]["exact_same_counts"])
    row("  softened/asterisked", lambda s: s["profanity"]["softened_or_asterisked"])
    row("same output as baseline", lambda s: s.get("same_as_baseline", "-"))
    row("wall median s", lambda s: f"{s['latency']['median']:.2f}")
    row("wall p95 s", lambda s: f"{s['latency']['p95']:.2f}")
    row("wall max s", lambda s: f"{s['latency']['max']:.2f}")
    row("gen tok/s", lambda s: f"{s['latency']['gen_tok_s']:.1f}")
    row("prompt tok/s", lambda s: f"{s['latency']['prompt_tok_s']:.0f}")
    row("median prompt tokens", lambda s: s["latency"]["median_prompt_eval_count"])
    row("median gen tokens", lambda s: s["latency"]["median_eval_count"])
    row("latency: first N excluded", lambda s: s["latency_excluded_first_n"])
    row("mid-pass reloads", lambda s: s["latency"]["loads_mid_pass"])
    row("fit: s per 100 chars", lambda s: f"{s['latency']['fit_chars']['slope_s_per_100chars']:.3f}")
    row("fit: intercept s", lambda s: f"{s['latency']['fit_chars']['intercept_s']:.2f}")
    row("fit r2", lambda s: f"{s['latency']['fit_chars']['r2']:.2f}")
    row("pred @170ch/350ch/1000ch", lambda s: "/".join(f"{s['latency']['fit_chars'][k]:.1f}" for k in ("pred_170ch", "pred_350ch", "pred_1000ch")))
    row("pred @10s/30s/60s audio", lambda s: "/".join(f"{s['latency']['fit_audio'][k]:.1f}" for k in ("pred_10s", "pred_30s", "pred_60s")) if "fit_audio" in s["latency"] else "-")
    row("cold load s", lambda s: s["cold_load_s"])
    row("first req wall / load s", lambda s: f"{s['latency']['first_request_wall_s']:.1f} / {s['latency']['first_request_load_s']:.1f}")
    row("pass wall s", lambda s: s["pass_wall_s"])
    print("\nVRAM at pass start (/api/ps):")
    for m in runs:
        print(f"  {m}: " + ", ".join(f"{v['name']}={v['size_vram'] / 2 ** 30:.2f}GB (size {v['size'] / 2 ** 30:.2f}GB)" for v in summary[m]["vram"] or []))
    print("\nblind key:", key)


if __name__ == "__main__":
    main()
