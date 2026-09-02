"""Parakeet TDT 0.6B v2 pass (onnx-asr, fp32 ONNX, CUDA EP) over the same
retained WAVs. Text plus per-clip wall time, warm-up excluded. Verifies the
CUDA provider is actually the one running: session provider list, per-process
GPU memory via nvidia-smi, and a CPU-vs-CUDA timing check on one clip."""
import json, os, subprocess, sys, time, wave
from datetime import datetime
from pathlib import Path
import numpy as np
import onnxruntime as ort

SP = Path(__file__).parent
BASE = Path(r"C:\Users\Uriah\Projects\WisprClone")
wavs = sorted((BASE / "retained_audio").glob("*.wav"))

def prog(msg):
    with open(SP / "parakeet_progress.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")

def load_wav(p):
    with wave.open(str(p), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

def gpu_mem_used():
    # per-process memory is [N/A] under Windows WDDM, so use the whole-GPU figure
    # and report deltas around this process's own load/warm-up
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0])

MEM0 = gpu_mem_used()
def gpu_mem_this_process():
    return gpu_mem_used() - MEM0

ort.preload_dlls()  # pulls cudart/cublas/cudnn from the nvidia-* pip wheels
import onnx_asr

t0 = time.perf_counter()
model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v2",
                            providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
load_s = time.perf_counter() - t0
asr = model.asr
enc, dec = asr._encoder, asr._decoder_joint
assert enc.get_providers()[0] == "CUDAExecutionProvider", enc.get_providers()
print("ort", ort.__version__, "build cuda", getattr(ort, "print_debug_info", None) and "see below")
print("preprocessor providers:", getattr(asr, "_preprocessor", None) and asr._preprocessor._preprocessor.get_providers() if hasattr(getattr(asr, "_preprocessor", None), "_preprocessor") else "n/a")
print("load", round(load_s, 1), "s")
print("encoder providers:", enc.get_providers(), "| decoder providers:", dec.get_providers())
print("encoder provider options:", enc.get_provider_options().get("CUDAExecutionProvider"))
print("gpu mem this pid after load (MiB):", gpu_mem_this_process())

# warm-up, untimed
sample = load_wav(wavs[0]); long = max(wavs, key=lambda p: p.stat().st_size)
print("warmup:", model.recognize(sample, sample_rate=16000))
model.recognize(load_wav(long), sample_rate=16000)
t1 = time.perf_counter(); model.recognize(load_wav(long), sample_rate=16000); cuda_s = time.perf_counter() - t1
print("gpu mem this pid after warmup (MiB):", gpu_mem_this_process())

# CPU control on the same long clip so the CUDA claim rests on timing too
cpu_model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v2", providers=["CPUExecutionProvider"])
cpu_model.recognize(sample, sample_rate=16000)
t1 = time.perf_counter(); cpu_model.recognize(load_wav(long), sample_rate=16000); cpu_s = time.perf_counter() - t1
print(f"control on {long.name} ({long.stat().st_size/32000:.1f}s audio): CUDA {cuda_s:.2f}s vs CPU {cpu_s:.2f}s")
del cpu_model
prog(f"Parakeet CUDA provider confirmed: encoder session providers {enc.get_providers()}, "
     f"{gpu_mem_this_process()} MiB GPU memory held by this process, longest clip {long.stat().st_size/32000:.1f}s "
     f"took {cuda_s:.2f}s on CUDA vs {cpu_s:.2f}s on CPU. Model load {load_s:.1f}s.")

out_path = SP / "parakeet_results.json"
results = {}
prog(f"Parakeet pass started on {len(wavs)} WAVs.")
pass_t0 = time.perf_counter()
for i, p in enumerate(wavs, 1):
    audio = load_wav(p)
    t1 = time.perf_counter()
    text = model.recognize(audio, sample_rate=16000)
    dt = time.perf_counter() - t1
    results[p.name] = {"text": text, "audio_s": round(len(audio) / 16000, 3), "wall_s": round(dt, 4)}
    if i % 50 == 0:
        out_path.write_text(json.dumps(results, indent=0, ensure_ascii=False), encoding="utf-8")
    if i % 100 == 0:
        prog(f"Parakeet pass: {i} of {len(wavs)} clips done, {time.perf_counter()-pass_t0:.0f}s elapsed.")
        print(i, flush=True)
out_path.write_text(json.dumps(results, indent=0, ensure_ascii=False), encoding="utf-8")
total = time.perf_counter() - pass_t0
prog(f"Parakeet pass finished: {len(results)} clips in {total:.0f}s wall (sum of per-clip times {sum(r['wall_s'] for r in results.values()):.0f}s).")
print("done", total, flush=True)
