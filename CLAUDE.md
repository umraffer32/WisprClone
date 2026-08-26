# WisprClone

Local push-to-talk dictation for Windows. Hold the mouse X2 button to record,
release to transcribe (faster-whisper on CUDA) and paste into the focused app.
Tap Right Ctrl to toggle long-form recording. Dictations over min_audio_s
(either mode) also get an LLM polish pass through a local Ollama model.
Everything runs on this machine; no dictation audio or text leaves it.

Personal tool, not a planned public release (see README.md's Scope section —
weighed against Handy, the most popular free Wispr Flow alternative, and
decided against chasing packaging/cross-platform/installer work). Treat this
as a portfolio piece: don't propose packaging, installers, or general-audience
onboarding work unless explicitly asked.

## Files

- `wisprclone.py` — entry point: single-instance mutex, StateMachine, input
  hooks, tk main loop (`tick()` every 33ms), teardown and tray-Restart relaunch.
- `audio.py` — `Recorder`: sounddevice callback, 250ms pre-roll, buffers
  blocks while recording, enqueues `{"blocks", "mode"}` jobs on stop. Also
  `Ducker` (lowers other apps' volume while the mic is hot).
- `transcribe.py` — `Transcriber` thread: normalize/trim, whisper, regex
  `clean_text()`, LLM polish (duration-gated, mode-agnostic), clipboard
  paste. Also the polish prompt and its output guards.
- `ui.py` — recording pill overlay (draggable; position persists in the
  gitignored `pill_pos.txt`) and tray icon/menu.
- `config.toml` — all knobs. The app must be restarted to pick up changes.
- `corrections.txt` — wrong=right word fixes, applied live (no restart).
- `history.log` — every pasted dictation, timestamped. Seeds the tray word
  counter. `wisprclone.log` (1MB rotating) holds diagnostics and per-job
  timing lines (`job: audio=… whisper=… polish=… mode=…`) plus raw/polished
  diffs whenever polish changes text.
- `vad_shadow.log` — streaming-branch diagnostic only: one JSON line per
  dictation with the Silero segment bounds streaming would have used
  (500/700/1000ms candidates). Gitignored via `*.log`; goes away with the
  shadow when real streaming lands.

## Launching and restarting — read before touching a running instance

- The app auto-starts via the Task Scheduler task `WisprClone` (at logon,
  RunLevel Highest). NOT the Startup folder: the exe carries a RUNASADMIN
  compat flag (needed so dictation reaches elevated windows), and Windows
  silently skips elevated Startup-folder shortcuts.
- To restart after code changes: tray icon → Restart, or
  `Start-ScheduledTask -TaskName WisprClone` after killing the old process.
- Never launch bare `pythonw.exe wisprclone.py` — it skips the venv wiring
  and dies with ModuleNotFoundError. The launcher is
  `.venv\Scripts\WisprClone.exe` with `wisprclone.py` as the argument and the
  project root as cwd.
- The running process is elevated; a non-elevated shell cannot kill it.
- Single instance is enforced by a named mutex; the old process must be gone
  before a new launch does anything.

## External dependencies not in requirements.txt

Ollama must be running at 127.0.0.1:11434 with the model named in
`[polish]` pulled, or polish silently falls back to raw text (by design —
a failed polish must never lose a dictation). Always address it as
127.0.0.1, never localhost: Windows eats ~2s per request trying IPv6 first.

## Conventions

- Append an entry to LOG.md with any meaningful commit: what changed and why,
  including testing results and reversals. Decision-level, not diff narration.
- Config knobs belong in config.toml; internal sanity thresholds stay in code.
- clean_text() regexes are one-quirk-per-pattern; check the polish pass before
  adding another.
