# WisprClone

Local push-to-talk dictation for Windows. Hold the mouse X2 button to record,
release to transcribe (faster-whisper on CUDA) and paste into the focused app.
Tap Right Ctrl to toggle long-form recording. Dictations over min_audio_s
(either mode) can also get an LLM polish pass through a local Ollama model;
that pass is switched off in config.toml since 2026-09-01 as a trial (see
LOG.md), so the app currently makes no Ollama calls at all.
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
- `transcribe.py` — `Transcriber` thread: normalize/trim, whisper, chunk
  join `join_segments()` (lowercases a capital the batched pipeline puts at
  a mid-sentence chunk cut), regex `clean_text()`, LLM polish
  (duration-gated, mode-agnostic), clipboard paste. Also the polish prompt
  and its output guards.
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
- `analysis_tools/` — `mine_vocab.py` / `mine_streaming.py` /
  `mine_merge_rule.py` / `mine_polish.py` / `mine_polish_3b.py` /
  `mine_segment_polish.py` / `mine_ollama_parallel.py`: offline analysis
  scripts over the logs above, moved into their own directory 2026-08-29 to
  separate them from the app itself. Each still finds the repo root's
  `wisprclone.log`/`config.toml`/etc. via `BASE = Path(__file__).parent.parent`,
  and the three that import `transcribe.py` add the repo root to `sys.path`
  first, since it no longer sits next to them. Personal vocabulary → the Whisper
  prompt, the streaming pause-threshold
  pick, the merge-rule simulation, a polish-quality/filler audit, the same
  audit replayed through a smaller polish model to judge a downsize (see
  LOG.md 2026-08-28), whether polishing pause-split transcript pieces in
  isolation matches whole-transcript polish (settled no for
  segment-parallel polish - see LOG.md), and Ollama's real request
  concurrency on this machine (serializes - `OLLAMA_NUM_PARALLEL:1`). Run
  by hand, summary output only.
- `analysis_tools/results/` — `README.md` is the analysis log: one entry per
  offline test (newest first, LOG.md's shape) with the full tables and the
  call, where LOG.md keeps only the decision. New test results get logged
  there. Each dated subfolder holds that day's data (reference transcripts
  of every retained clip per model, polish replay inputs and outputs,
  reports) and the scripts that made them, all gitignored since the data
  quotes dictations. A future candidate model only needs its own pass
  against the saved baselines. First set 2026-09-01.

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
- A bug or incident gets an entry in BUGS.md, whether or not it produced a
  commit. LOG.md is commit-keyed and only covers changes; BUGS.md is the
  failure history, including things diagnosed with no code fix (a driver
  update crashing the app, say). Added 2026-08-29, backfilled from all of
  LOG.md's history via two independent Fable passes (one drafts, one audits
  the draft against LOG.md and git history fresh) - keep using both passes
  for any future large rewrite of either file.
- Keep commit messages short: one imperative line by default, or up to a
  couple one-sentence bullets if a commit genuinely bundles multiple
  changes. Never a paragraph-plus-sub-bullets per file - that detail goes
  in LOG.md, not the commit body. Reinforced 2026-08-26 after drifting into
  exactly that on three same-day commits.
- Config knobs belong in config.toml; internal sanity thresholds stay in code.
- clean_text() regexes are one-quirk-per-pattern; check the polish pass before
  adding another.
- Writing or modifying code in this repo goes to an agent dispatched in an
  isolated git worktree (`isolation: "worktree"`), not written directly in
  the main chat. Main chat still scopes the work, reviews the result, and
  handles git. Which model gets the job is the main chat's call (decided
  2026-09-02): Sonnet or Opus for anything small or well-specified, Fable
  only for the genuinely hard changes - subtle concurrency, a design that
  needs pressure-testing, a bug that resisted a first pass. From 2026-08-26
  to 2026-09-02 every code change went to Fable regardless of size, after it
  outperformed on the SleepWatcher diagnosis and a code-plan critique; that
  burned usage on trivial edits. When Fable is the pick, dispatch at max
  effort - there's no effort parameter on the Agent tool itself, so this
  means explicitly telling the agent in its prompt to work at maximum
  reasoning depth, not the default pass. Also pair every
  such dispatch with a ~60s Monitor heartbeat that digests real progress
  (files touched, commands run, the agent's last note-to-self - never raw
  transcript dumped verbatim) and relay each one as 2-3 plain sentences of
  what it's actually doing, not just "still working." Stop the heartbeat
  the moment the real completion notice arrives.
- Offline measurement runs (model bake-offs, replays, environment setup,
  looping files through a model) go to a Sonnet agent, not Fable: the work
  is mechanical and Sonnet did the whole Canary-Qwen run on 2026-09-02 for
  ~3% of a usage window, where Fable at max effort on the polish bake-off
  the day before burned through the limit. The agent appends one plain
  sentence per milestone to a progress file; the main chat runs a Monitor
  on it and reads each line against what should be true at that point
  (this caught NeMo swapping the CUDA torch for a CPU wheel). Fable stays
  for app code changes and for judgment-heavy reads such as blind
  side-by-side quality calls; the main chat can do the read itself from
  saved JSON when it's small. Tell agents not to wake on every progress
  milestone, only on a pass finishing or failing.
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
