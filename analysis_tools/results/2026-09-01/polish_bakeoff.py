"""Replay every polish input through one Ollama model with _polish's exact
prompt and options, recording output, timings, and the app's guard verdicts.

usage: python polish_bakeoff.py build            -> writes inputs.json
       python polish_bakeoff.py run <model-tag>  -> writes out_<model>.json
"""
import json, re, sys, time, tomllib, subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(r"C:\Users\Uriah\Projects\WisprClone")
SP = Path(__file__).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "analysis_tools"))
import transcribe  # noqa: E402  (real prompt, clean_text, guards)
from mine_polish import load_pairs  # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
_s = requests.Session()
_s.trust_env = False

with open(BASE / "config.toml", "rb") as f:
    CFG = tomllib.load(f)
P = CFG["polish"]


def prog(msg):
    with open(SP / "polish_progress.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S} {msg}\n")


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def fname(model):
    return SP / f"out_{model.replace(':', '_').replace('/', '_')}.json"


def build():
    inputs, seen = [], set()
    pairs = load_pairs([p for p in (BASE / "wisprclone.log.1", BASE / "wisprclone.log") if p.exists()])
    for p in pairs:
        key = norm(p["raw"]).lower()
        if key in seen:
            continue
        seen.add(key)
        inputs.append({"id": f"log_{len(inputs):04d}", "src": "log", "ts": p["ts"],
                       "text": p["raw"], "live_out": p["out"], "audio_s": None})
    wr = json.loads((SP / "whisper_results.json").read_text(encoding="utf-8"))
    n_dup = 0
    for name, v in sorted(wr.items()):
        if v["audio_s"] < P["min_audio_s"]:
            continue
        text = transcribe.clean_text(v["text"], BASE / CFG["files"]["corrections"],
                                     BASE / CFG["files"]["emphasis_words"])
        if not text:
            continue
        key = norm(text).lower()
        if key in seen:
            n_dup += 1
            continue
        seen.add(key)
        inputs.append({"id": f"wav_{len(inputs):04d}", "src": "wav", "wav": name,
                       "text": text, "live_out": None, "audio_s": v["audio_s"]})
    (SP / "inputs.json").write_text(json.dumps(inputs, indent=1, ensure_ascii=False), encoding="utf-8")
    n_log = sum(i["src"] == "log" for i in inputs)
    msg = (f"Input set built: {len(inputs)} unique polish inputs ({n_log} from wisprclone.log raw/out pairs, "
           f"{len(inputs) - n_log} from the Whisper pass clips >= {P['min_audio_s']}s after clean_text; "
           f"{n_dup} wav clips dropped as exact duplicates of a log input).")
    print(msg)
    prog(msg)


def guards(text, polished):
    """Every _polish guard evaluated independently, plus the status the app
    would actually report (first guard to trip, in _polish's order)."""
    g = {}
    ratio = len(polished) / max(1, len(text))
    g["ratio"] = round(ratio, 3)
    g["suspicious"] = (not polished) or not (P["min_ratio"] <= ratio <= P["max_ratio"])
    g["dropped_question"] = polished.count("?") < text.count("?")
    g["dropped_profanity"] = bool(Counter(transcribe._SWEARS.findall(text.lower()))
                                  - Counter(transcribe._SWEARS.findall(polished.lower())))
    lost = transcribe._lost_sentence(text, polished)
    g["dropped_sentence"] = lost is not None
    g["lost_sentence_text"] = lost
    for k in ("suspicious", "dropped_question", "dropped_profanity", "dropped_sentence"):
        if g[k]:
            g["status"] = k
            break
    else:
        g["status"] = "ok"
    return g


def show(model):
    r = _s.post(f"{OLLAMA}/api/show", json={"model": model}, timeout=30)
    r.raise_for_status()
    return r.json()


def ps():
    return _s.get(f"{OLLAMA}/api/ps", timeout=10).json().get("models", [])


def load_request(model, think):
    body = {"model": model, "keep_alive": "24h", "options": {"num_ctx": 8192}}
    if think is not None:
        body["think"] = think
    t = time.perf_counter()
    r = _s.post(f"{OLLAMA}/api/generate", json=body, timeout=300)
    r.raise_for_status()
    return time.perf_counter() - t, r.json()


def run(model):
    inputs = json.loads((SP / "inputs.json").read_text(encoding="utf-8"))
    info = show(model)
    caps = info.get("capabilities", [])
    think = False if "thinking" in caps else None
    details = info.get("details", {})
    minfo = {k: v for k, v in info.get("model_info", {}).items()
             if any(x in k for x in ("parameter_count", "context_length", "architecture",
                                     "embedding_length", "block_count"))}
    print(f"{model}: caps={caps} details={details} think={think}", flush=True)

    # cold load: evict, then time a bare load (twice; second is page-cache warm)
    subprocess.run(["ollama", "stop", model], capture_output=True)
    time.sleep(1)
    cold1, _ = load_request(model, think)
    subprocess.run(["ollama", "stop", model], capture_output=True)
    time.sleep(1)
    cold2, _ = load_request(model, think)
    vram = [{"name": m["name"], "size": m["size"], "size_vram": m.get("size_vram"),
             "context_length": m.get("context_length")} for m in ps()]
    prog(f"{model}: pass started on {len(inputs)} inputs; cold load {cold1:.1f}s then {cold2:.1f}s "
         f"(after ollama stop, num_ctx 8192); VRAM per /api/ps: "
         + ", ".join(f"{m['name']}={m['size_vram'] / 2 ** 30:.2f}GB" for m in vram))

    results = []
    done_ids = set()
    if fname(model).exists():  # resume a killed pass from its last partial save
        prev = json.loads(fname(model).read_text(encoding="utf-8"))
        if prev.get("partial"):
            results = prev["results"]
            done_ids = {r["id"] for r in results}
            prog(f"{model}: resuming, {len(results)} results already saved.")
    t_pass = time.perf_counter()
    for i, inp in enumerate(inputs, 1):
        if inp["id"] in done_ids:
            continue
        text = inp["text"]
        body = {"model": model, "stream": False, "keep_alive": "24h",
                "prompt": transcribe.POLISH_PROMPT + text,
                "options": {"temperature": 0, "num_ctx": 8192,
                            "num_predict": max(64, int(len(text) / 4 * P["max_ratio"]))}}
        if think is not None:
            body["think"] = think
        rec = {"id": inp["id"], "model": model}
        t = time.perf_counter()
        try:
            r = _s.post(f"{OLLAMA}/api/generate", json=body, timeout=P["timeout_s"])
            wall = time.perf_counter() - t
            r.raise_for_status()
            j = r.json()
            polished = j.get("response", "").strip()
            rec.update({
                "out": polished, "wall_s": round(wall, 4), "error": None,
                "thinking": j.get("thinking"),
                "has_think_tag": "<think>" in j.get("response", ""),
                "done_reason": j.get("done_reason"),
                "total_ns": j.get("total_duration"), "load_ns": j.get("load_duration"),
                "prompt_eval_count": j.get("prompt_eval_count"),
                "prompt_eval_ns": j.get("prompt_eval_duration"),
                "eval_count": j.get("eval_count"), "eval_ns": j.get("eval_duration"),
                "guards": guards(text, polished),
            })
        except requests.Timeout:
            rec.update({"out": None, "wall_s": round(time.perf_counter() - t, 4), "error": "timeout"})
        except Exception as e:
            rec.update({"out": None, "wall_s": round(time.perf_counter() - t, 4),
                        "error": f"{type(e).__name__}: {e}"[:300]})
        results.append(rec)
        if i % 100 == 0:
            prog(f"{model}: {i} of {len(inputs)} inputs done, {time.perf_counter() - t_pass:.0f}s elapsed.")
            fname(model).write_text(json.dumps({"model": model, "partial": True, "results": results},
                                               indent=0, ensure_ascii=False), encoding="utf-8")
    total = time.perf_counter() - t_pass
    vram_after = [{"name": m["name"], "size": m["size"], "size_vram": m.get("size_vram")} for m in ps()]
    out = {"model": model, "think": think, "capabilities": caps, "details": details, "model_info": minfo,
           "cold_load_s": [round(cold1, 2), round(cold2, 2)], "vram_at_start": vram,
           "vram_at_end": vram_after, "pass_wall_s": round(total, 1), "n": len(results),
           "results": results}
    fname(model).write_text(json.dumps(out, indent=0, ensure_ascii=False), encoding="utf-8")
    n_err = sum(1 for r in results if r["error"])
    n_ok = sum(1 for r in results if not r["error"] and r["guards"]["status"] == "ok")
    prog(f"{model}: pass finished, {len(results)} inputs in {total:.0f}s wall ({n_err} errors/timeouts, "
         f"{n_ok} accepted by all guards).")
    print("done", model, total, flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build()
    else:
        run(sys.argv[2])
