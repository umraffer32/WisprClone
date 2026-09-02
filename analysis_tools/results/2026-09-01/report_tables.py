"""Markdown tables for polish_report.md from score_summary.json and cold_load.json."""
import json
from pathlib import Path

SP = Path(__file__).parent
S = json.loads((SP / "score_summary.json").read_text(encoding="utf-8"))
C = json.loads((SP / "cold_load.json").read_text(encoding="utf-8")) if (SP / "cold_load.json").exists() else {}
M = [m for m in ["qwen2.5:7b-instruct", "qwen3.5:4b", "qwen3.5:9b", "gemma4:e4b"] if m in S]

def table(title, rows):
    print(f"\n{title}\n")
    print("| | " + " | ".join(M) + " |")
    print("|---|" + "---:|" * len(M))
    for label, f in rows:
        print(f"| {label} | " + " | ".join(str(f(S[m])) for m in M) + " |")

def p(n, d):
    return f"{n} ({100 * n / max(1, d):.0f}%)"

table("Guards (of 548 inputs)", [
    ("errors / timeouts", lambda s: sum(s["errors"].values())),
    ("suspicious length", lambda s: s["guards_each"].get("suspicious", 0)),
    ("dropped question", lambda s: s["guards_each"].get("dropped_question", 0)),
    ("dropped profanity", lambda s: s["guards_each"].get("dropped_profanity", 0)),
    ("dropped sentence", lambda s: s["guards_each"].get("dropped_sentence", 0)),
    ("rejected, any guard", lambda s: p(548 - sum(s["errors"].values()) - s["accepted"], 548)),
])
table("Over-editing (accepted outputs only)", [
    ("accepted", lambda s: s["accepted"]),
    ("no-op (identical to input)", lambda s: p(s["noop"], s["accepted"])),
    ("word edit distance, mean", lambda s: f"{s['wed']['mean']:.3f}"),
    ("word edit distance, p90", lambda s: f"{s['wed']['p90']:.3f}"),
    ("edits changing >10% of words", lambda s: s["wed"]["gt_0.10"]),
    ("edits changing >25% of words", lambda s: s["wed"]["gt_0.25"]),
    ("deleted a content word", lambda s: p(s["deleted_content_words_inputs"], s["accepted"])),
    ("added a content word", lambda s: p(s["added_content_words_inputs"], s["accepted"])),
    ("fewer sentences than input", lambda s: s["sent_fewer"]),
    ("more sentences than input", lambda s: s["sent_more"]),
    ("changed a number", lambda s: len(s["num_changed"])),
    ("lost a proper noun", lambda s: len(s["proper_noun_lost"])),
    ("lost a technical token", lambda s: len(s["tech_lost"])),
    ("added an em dash", lambda s: s["emdash_added"]),
    ("added a semicolon", lambda s: s["semicolon_added"]),
    ("added paragraph breaks", lambda s: s["newline_added"]),
    ("same output as qwen2.5:7b", lambda s: s.get("same_as_baseline", "-")),
])
table("Cleanup the regexes missed", [
    ("comma stammers present / removed", lambda s: f"{s['stammer_in']} / {s['stammer_removed']}"),
    ("residual fillers present / removed", lambda s: f"{s['filler_in']} / {s['filler_removed']}"),
])
table("Profanity (inputs containing a swear)", [
    ("inputs with swears", lambda s: s["profanity"]["inputs_with_swears"]),
    ("every swear kept verbatim", lambda s: p(s["profanity"]["exact_same_counts"], s["profanity"]["inputs_with_swears"])),
    ("softened or asterisked", lambda s: s["profanity"]["softened_or_asterisked"]),
])
table("Latency during the full pass (first request excluded; 9b: first 77 excluded)", [
    ("wall median s", lambda s: f"{s['latency']['median']:.2f}"),
    ("wall p95 s", lambda s: f"{s['latency']['p95']:.2f}"),
    ("wall max s", lambda s: f"{s['latency']['max']:.2f}"),
    ("generation tok/s", lambda s: f"{s['latency']['gen_tok_s']:.0f}"),
    ("median generated tokens", lambda s: s["latency"]["median_eval_count"]),
    ("fit: s per 100 input chars", lambda s: f"{s['latency']['fit_chars']['slope_s_per_100chars']:.2f}"),
    ("fit: intercept s", lambda s: f"{s['latency']['fit_chars']['intercept_s']:.2f}"),
    ("fit r2", lambda s: f"{s['latency']['fit_chars']['r2']:.2f}"),
    ("predicted at 170 / 350 / 1000 chars", lambda s: " / ".join(f"{s['latency']['fit_chars'][k]:.1f}" for k in ("pred_170ch", "pred_350ch", "pred_1000ch"))),
    ("predicted at 10s / 30s / 60s audio", lambda s: " / ".join(f"{s['latency']['fit_audio'][k]:.1f}" for k in ("pred_10s", "pred_30s", "pred_60s"))),
    ("thinking text leaked", lambda s: s["thinking_leak"]),
])
if C:
    print("\nCold start and footprint (model alone on the GPU, 3 trials)\n")
    print("| | " + " | ".join(M) + " |")
    print("|---|" + "---:|" * len(M))
    def solo_tok(m):
        so = C[m]["solo"]
        return sum(x["eval_count"] for x in so) / (sum(x["eval_ns"] for x in so) / 1e9)
    import statistics
    rows = [
        ("load after ollama stop, s", lambda m: " / ".join(f"{t['load_wall_s']:.1f}" for t in C[m]["trials"] if "load_wall_s" in t)),
        ("first polish after load, s", lambda m: " / ".join(f"{t['first_polish_wall_s']:.1f}" for t in C[m]["trials"] if "first_polish_wall_s" in t)),
        ("same polish again, s", lambda m: " / ".join(f"{t['second_polish_wall_s']:.1f}" for t in C[m]["trials"] if "second_polish_wall_s" in t)),
        ("VRAM per /api/ps, GB", lambda m: f"{C[m]['trials'][0]['size_vram_gb']:.2f}"),
        ("disk, GB", lambda m: {"qwen2.5:7b-instruct": 4.7, "qwen3.5:4b": 3.4, "qwen3.5:9b": 6.6, "gemma4:e4b": 9.6}[m]),
        ("solo generation tok/s", lambda m: f"{solo_tok(m):.0f}"),
        ("solo median wall s (30 inputs)", lambda m: f"{statistics.median(x['wall_s'] for x in C[m]['solo']):.2f}"),
    ]
    for label, f in rows:
        print(f"| {label} | " + " | ".join(str(f(m)) for m in M) + " |")
