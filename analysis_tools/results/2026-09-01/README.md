# Bake-off results, 2026-09-01

Baselines for future model comparisons, produced the day the batched
pipeline landed. Everything ran over the retained dictation audio (886 WAVs
at the time) or over polish inputs derived from it, so a new candidate only
needs its own pass; the reference passes are here.

Data (gitignored, local only, since it contains full dictation transcripts):

- `whisper_results.json` - large-v3-turbo through the live batched path, text
  and per-clip timing, keyed by WAV name. The Whisper baseline.
- `whisper_results_large-v3.json` - same clips, large-v3. Rejected (LOG.md).
- `parakeet_results.json` - same clips, Parakeet TDT 0.6B v2 via onnx-asr on
  CUDA. Rejected (LOG.md).
- `inputs.json` - the 548 polish inputs (log raw lines + Whisper-pass clips
  over 8s through clean_text). `out_<model>.json` - each polish model's
  outputs with Ollama timings and guard verdicts; `out_sentinel_9b.json` -
  the NOCHANGE-prompt variant. `score_summary.json`, `compare_rows.json`,
  `join.json`, `blind_key.json`, `cold_load.json` - scoring intermediates.
- `*_report.md`, `*_report.txt`, `side_by_side.md`, `compare.out` - the
  written results. Quote dictations, so gitignored with the data.

Scripts (committed, reference only): written for a session scratchpad, so
`SP`/`BASE` paths and output filenames need pointing at this directory
before they run again. `whisper_pass.py` and `largev3_pass.py` transcribe
through `Transcriber.pipe`; `parakeet_pass.py` needs its own venv with
onnxruntime-gpu (CUDA 12 build from ORT's own index, not PyPI's);
`polish_bakeoff.py` + `score.py` + `cold_load.py` + `report_tables.py` are
the polish replay; `sentinel_test.py` the no-change-sentinel test;
`compare.py` the Whisper-vs-Parakeet scorer. Decisions and numbers are in
LOG.md's 2026-09-01 entries.
