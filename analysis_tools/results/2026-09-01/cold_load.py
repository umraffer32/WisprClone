"""Cold-start cost per model: stop it, confirm /api/ps no longer lists it,
then time a bare load request with the app's num_ctx (8192). Three trials
each. Also records the first real polish request after the load (the
prompt-prefix cache is empty then) versus a second identical request."""
import json, subprocess, sys, time
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(r"C:\Users\Uriah\Projects\WisprClone")
SP = Path(__file__).parent
sys.path.insert(0, str(BASE))
import transcribe  # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
_s = requests.Session()
_s.trust_env = False
MODELS = ["qwen2.5:7b-instruct", "qwen3.5:4b", "qwen3.5:9b", "gemma4:e4b"]
TEXT = ("Okay, so I think what we should do is go ahead and test the polish pass again, "
        "because last time it dropped a sentence and I want to see whether that happens again "
        "with the new model. Does that make sense?")


def loaded():
    return {m["name"]: m for m in _s.get(f"{OLLAMA}/api/ps", timeout=10).json().get("models", [])}


def gen(model, think, prompt=None):
    body = {"model": model, "keep_alive": "24h", "stream": False, "options": {"num_ctx": 8192}}
    if prompt is not None:
        body["prompt"] = prompt
        body["options"].update({"temperature": 0, "num_predict": max(64, int(len(TEXT) / 4 * 2.5))})
    if think is not None:
        body["think"] = think
    t = time.perf_counter()
    r = _s.post(f"{OLLAMA}/api/generate", json=body, timeout=300)
    r.raise_for_status()
    return time.perf_counter() - t, r.json()


def main():
    out = {}
    for m in MODELS:
        caps = _s.post(f"{OLLAMA}/api/show", json={"model": m}, timeout=30).json().get("capabilities", [])
        think = False if "thinking" in caps else None
        trials = []
        for _ in range(3):
            # every model out, not just this one: with others resident the GPU
            # oversubscribes next to Whisper and generation drops to ~18 tok/s
            # while /api/ps still claims 100% GPU (seen 14:58 on the 9b)
            for other in MODELS:
                subprocess.run(["ollama", "stop", other], capture_output=True)
            for _ in range(50):
                if not any(x in loaded() for x in MODELS):
                    break
                time.sleep(0.2)
            else:
                trials.append({"error": "still loaded after stop"})
                continue
            time.sleep(1)
            w_load, j = gen(m, think)
            ps = loaded().get(m, {})
            w1, j1 = gen(m, think, transcribe.POLISH_PROMPT + TEXT)
            w2, j2 = gen(m, think, transcribe.POLISH_PROMPT + TEXT)
            trials.append({"load_wall_s": round(w_load, 2), "load_ns": j.get("load_duration"),
                           "size_vram_gb": round(ps.get("size_vram", 0) / 2 ** 30, 2),
                           "size_gb": round(ps.get("size", 0) / 2 ** 30, 2),
                           "first_polish_wall_s": round(w1, 2), "first_prompt_eval": j1.get("prompt_eval_count"),
                           "second_polish_wall_s": round(w2, 2), "second_prompt_eval": j2.get("prompt_eval_count"),
                           "out1": j1.get("response", "").strip(), "out2": j2.get("response", "").strip()})
            print(m, trials[-1], flush=True)
        # solo latency: 30 fixed inputs spanning the length range, only this
        # model resident (plus the app's Whisper), so tok/s is uncontended
        inputs = json.loads((SP / "inputs.json").read_text(encoding="utf-8"))
        inputs.sort(key=lambda i: len(i["text"]))
        sample = inputs[::max(1, len(inputs) // 30)][:30]
        solo = []
        for inp in sample:
            w, j = gen(m, think, transcribe.POLISH_PROMPT + inp["text"])
            solo.append({"id": inp["id"], "chars": len(inp["text"]), "wall_s": round(w, 3),
                         "eval_count": j.get("eval_count"), "eval_ns": j.get("eval_duration"),
                         "prompt_eval_count": j.get("prompt_eval_count"), "prompt_eval_ns": j.get("prompt_eval_duration")})
        out[m] = {"think": think, "trials": trials, "solo": solo}
        toks = sum(x["eval_count"] for x in solo) / (sum(x["eval_ns"] for x in solo) / 1e9)
        print(m, "solo gen tok/s", round(toks, 1), flush=True)
    (SP / "cold_load.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    # leave the app's model resident again
    gen("qwen2.5:7b-instruct", None)
    with open(SP / "polish_progress.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S} Cold-load measurement done: " + "; ".join(
            f"{m} load " + "/".join(f"{t['load_wall_s']:.1f}" for t in out[m]['trials'] if 'load_wall_s' in t) + "s"
            for m in MODELS) + " (3 trials each, after ollama stop). qwen2.5:7b re-warmed for the app.\n")


if __name__ == "__main__":
    main()
