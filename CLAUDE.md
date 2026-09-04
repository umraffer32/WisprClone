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
  `{"warm": True}` GPU-warm sentinel on start; also mutes the start of the
  buffer for the start cue's own duration (`set_start_mute_seconds`) so the
  cue's sound can't bleed into the mic (see LOG.md/BUGS.md 2026-09-02: the
  first cue attempt had no mute and corrupted the first word). Also
  `Ducker` (lowers other apps' volume while the mic is hot) and `Cue`
  (plays the sounds/ clips via winsound on record start/stop).
- `transcribe.py` — `Transcriber` thread: normalize/trim, whisper, chunk
  join `join_segments()` (lowercases a capital the batched pipeline puts at
  a mid-sentence chunk cut), regex `clean_text()`, LLM polish
  (duration-gated, mode-agnostic), clipboard paste. Also the polish prompt
  and its output guards.
- `ui.py` — recording pill overlay (draggable; position persists in the
  gitignored `pill_pos.txt`; `Anchor` thread re-centers it under the
  Claude Code compose box via UI Automation while that app is in front)
  and tray icon/menu.
- `config.toml` — all knobs. The app must be restarted to pick up changes.
- `corrections.txt` — wrong=right word fixes, applied live (no restart).
- `emphasis_words.txt` — words never collapsed by the stutter-cleanup or
  runaway-repeat regexes (comma or not), one per line, applied live (no
  restart).
- `sounds/` — `dictation-start.wav` / `dictation-stop.wav`, Wispr Flow's own
  cue clips (see SETUP.md for where to get them). Gitignored: not
  redistributable. A missing or unreadable clip disables the cue and logs a
  warning instead of raising.
- `history.log` — every pasted dictation, timestamped. Seeds the tray word
  counter. `wisprclone.log` (1MB rotating) holds diagnostics and per-job
  timing lines (`job: audio=… whisper=… polish=… mode=… polish_status=…
  paste=…`) plus raw/out diffs whenever polish or clean_text changes text.
- `vad_shadow.log` — one JSON line per dictation with the Silero segment
  bounds a streaming implementation would have used. Gitignored via `*.log`.
  Real streaming is shelved (LOG.md 2026-08-27) — the felt-latency win
  wasn't worth the build. Left running in case that changes.
- `retained_audio/` — real dictation audio, capped and auto-pruned
  (`[retain]` in config.toml), collected for the shelved streaming
  merge-rule decision. Gitignored; see that config section for how to
  clear it out if it's not worth keeping unused.
- `analysis_tools/` — `mine_vocab.py` / `mine_streaming.py` /
  `mine_merge_rule.py` / `mine_polish.py` / `mine_polish_3b.py` /
  `mine_segment_polish.py` / `mine_ollama_parallel.py`: offline analysis
  scripts over the logs above, run by hand, summary output only. Each
  finds the repo root's `wisprclone.log`/`config.toml` via
  `BASE = Path(__file__).parent.parent`; the three that import
  `transcribe.py` add the repo root to `sys.path` first.
- `analysis_tools/results/` — `README.md` is the analysis log: one entry
  per offline test (newest first) with the full tables, where LOG.md keeps
  only the decision. Each dated subfolder holds that day's data and
  scripts, gitignored since the data quotes dictations.

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

- LOG.md gets an entry for any meaningful commit: what changed and why,
  including testing and reversals. Decision-level, not diff narration.
- BUGS.md gets an entry for any bug or incident, whether or not it produced
  a commit — including things diagnosed with no code fix. LOG.md is
  commit-keyed and covers changes only; BUGS.md is the failure history.
  Any future large rewrite of either file: two independent passes (one
  drafts, one audits fresh against history), not one.
- Commit messages: one imperative line by default, a couple one-sentence
  bullets if a commit bundles multiple changes — never a paragraph or
  sub-bullets per file. That detail belongs in LOG.md.
- Commit a code change only after it's confirmed working live (tray
  Restart, a real dictation), not right after it passes offline checks
  alone. Once confirmed, commit promptly anyway — don't leave it
  uncommitted while later edits pile on top (this environment has no
  interactive git to untangle a mixed diff afterward). Pushing is separate
  and only happens on request.
- Config knobs belong in config.toml; internal sanity thresholds stay in code.
- clean_text() regexes are one-quirk-per-pattern; check the polish pass
  before adding another.
- Docs (CLAUDE.md, README.md, SETUP.md) must stay consistent with the code
  on every push — check before pushing, fix in the same commit, never a
  later cleanup pass. This has failed twice: once by searching a commit's
  own vocabulary instead of the *category* it belongs to (search "guard,"
  "model," "thread," not just the new term — an old paragraph enumerating
  siblings won't name the new one); once by skipping the check entirely
  because a one-line config flip didn't look big enough to matter. Size is
  not a valid signal — run this unconditionally, on every push, unasked.

## Agent dispatch

- Code changes go to a worktree-isolated agent (`isolation: "worktree"`)
  by default, not written directly in main chat. Exception: a genuinely
  trivial edit (a handful of constants, one config value, a comment fix)
  with no real logic to get wrong — main chat edits that directly. Main
  chat always scopes the work, reviews the result, and handles git.
- Model choice is main chat's call: Sonnet or Opus for anything small or
  well-specified; Fable only for genuinely hard problems — subtle
  concurrency, a design worth pressure-testing, a bug that resisted a
  first pass. Not a default: an earlier "everything goes to Fable" rule
  burned usage on trivial edits and was dropped.
- Offline/mechanical runs (bake-offs, replays, environment setup) go to a
  Sonnet agent with a progress file, not Fable — cheaper and just as
  reliable for mechanical work. Fable stays for real app code and
  judgment-heavy reads (blind quality comparisons).
- Dispatching Fable: state "maximum reasoning depth" explicitly in the
  prompt — the Agent tool has no effort parameter of its own.
- Every dispatch: pair with a ~60s Monitor heartbeat, relayed as 2-3 plain
  sentences of real progress (never "still working"), stopped the moment
  the real completion notice arrives.
- After a worktree-isolated dispatch reports done, verify it actually tore
  down (`git worktree list`, `git branch -a | grep worktree`, `ListAgents`)
  before moving on — a dispatched agent can nest its own sub-agent or
  monitor without being asked, and nothing else checks that scaffolding
  is gone. Clean up anything stray found, don't wait to be asked (2026-09-04,
  BUGS.md same date).
