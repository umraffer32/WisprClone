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
  blocks while recording, enqueues `{"blocks", "mode"}` jobs on stop and a
  `{"warm": True}` GPU-warm sentinel on start. Also `Ducker` (lowers other
  apps' volume while the mic is hot).
- `transcribe.py` — `Transcriber` thread: normalize/trim, whisper, regex
  `clean_text()`, LLM polish (duration-gated, mode-agnostic), clipboard
  paste. Also the polish prompt and its output guards.
- `ui.py` — recording pill overlay (draggable; position persists in the
  gitignored `pill_pos.txt`) and tray icon/menu.
- `config.toml` — all knobs. The app must be restarted to pick up changes.
- `corrections.txt` — wrong=right word fixes, applied live (no restart).
- `emphasis_words.txt` — words never collapsed by the stutter-cleanup or
  runaway-repeat regexes (comma or not), one per line, applied live (no
  restart).
- `history.log` — every pasted dictation, timestamped. Seeds the tray word
  counter. `wisprclone.log` (1MB rotating) holds diagnostics and per-job
  timing lines (`job: audio=… whisper=… polish=… mode=… polish_status=…
  paste=…`) plus raw/out diffs whenever polish or clean_text changes text.
- `vad_shadow.log` — one JSON line per dictation with the Silero segment
  bounds a streaming implementation would have used (300/400/500/700/1000ms
  candidates). Gitignored via `*.log`. Real streaming is shelved (see
  LOG.md 2026-08-27) - corrected felt-latency numbers showed a ~0.14s
  median win, not worth the build. Left running rather than torn out in
  case 60s+ dictations become common enough to reopen the question.
- `retained_audio/` — real dictation audio, capped and auto-pruned
  (`[retain]` in config.toml), collected for the now-shelved streaming
  merge-rule decision. Gitignored; see that section's comment for how to
  clear it out if it's not worth keeping around unused.
- `mine_vocab.py` / `mine_streaming.py` / `mine_merge_rule.py` /
  `mine_polish.py` / `mine_polish_3b.py` / `mine_segment_polish.py` /
  `mine_ollama_parallel.py` — offline analysis scripts over the logs above:
  personal vocabulary → Whisper hotwords, the streaming pause-threshold
  pick, the merge-rule simulation, a polish-quality/filler audit, the same
  audit replayed through a smaller polish model to judge a downsize (see
  LOG.md 2026-08-28), whether polishing pause-split transcript pieces in
  isolation matches whole-transcript polish (settled no for
  segment-parallel polish - see LOG.md), and Ollama's real request
  concurrency on this machine (serializes - `OLLAMA_NUM_PARALLEL:1`). Run
  by hand, summary output only.

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
- Keep commit messages short: one imperative line by default, or up to a
  couple one-sentence bullets if a commit genuinely bundles multiple
  changes. Never a paragraph-plus-sub-bullets per file - that detail goes
  in LOG.md, not the commit body. Reinforced 2026-08-26 after drifting into
  exactly that on three same-day commits.
- Config knobs belong in config.toml; internal sanity thresholds stay in code.
- clean_text() regexes are one-quirk-per-pattern; check the polish pass before
  adding another.
- Writing or modifying code in this repo goes to a Fable agent, dispatched
  in an isolated git worktree (`isolation: "worktree"`), not written
  directly in the main chat. Main chat (Sonnet) still scopes the work,
  reviews the result, and handles git. Decided 2026-08-26 after Fable
  outperformed on both the SleepWatcher diagnosis and a code-plan critique.
  Always dispatch at max effort - there's no effort parameter on the Agent
  tool itself, so this means explicitly telling the agent in its prompt to
  work at maximum reasoning depth, not the default pass. Also pair every
  such dispatch with a ~60s Monitor heartbeat that digests real progress
  (files touched, commands run, the agent's last note-to-self - never raw
  transcript dumped verbatim) and relay each one as 2-3 plain sentences of
  what it's actually doing, not just "still working." Stop the heartbeat
  the moment the real completion notice arrives.
- Before pushing any commit, check whether it makes a doc stale - CLAUDE.md,
  README.md, SETUP.md, or a comment elsewhere - and fix it in the same push,
  not a later cleanup pass. Decided 2026-08-26 after a repo-wide review
  found 8 real doc/code mismatches that had accumulated over one day's
  commits (a new script missing from CLAUDE.md's Files list, a stale model
  name in SETUP.md, a README claim a later commit made false, etc.). The
  goal is that docs are never something Uriah has to separately worry about
  or schedule an audit for - consistency is a property of every push, not a
  periodic cleanup task.
  - Reinforced 2026-08-27: a commit adding a 4th `_polish()` guard checked
    CLAUDE.md and searched for that commit's own keywords, but missed a
    SETUP.md paragraph written days earlier that named the other three
    guards by name - the search never looked for "guard" as a category,
    only for terms specific to the new one. Grepping a commit's own diff
    for its own vocabulary isn't enough when a claim living somewhere else
    enumerates the same thing without ever mentioning the new addition by
    name. When a change adds to, removes from, or changes a set something
    is already true of (guards, models, config knobs, threads, files),
    search all three docs for the *category* word (guard, model, thread),
    not just the specific new/changed term - an old paragraph enumerating
    siblings won't contain the new one's name for a keyword search to catch.
