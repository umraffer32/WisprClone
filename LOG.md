# Development log

Newest first. Decision-level: why things changed and what testing showed.
Diff-level detail lives in git history.

## 2026-08-24 — polish goes mode-agnostic, gated by duration (branch: streaming)

- The parked min_audio_s idea, implemented - but with the threshold question
  answered by preference instead of the planned timing histogram. Asked
  point-blank when he wants polish vs exact words, Uriah's answer: polish
  everything in both modes (the Wispr Flow benchmark - every dictation feels
  polished and instant), occasional stolen words are tolerable, and quick
  bursts skipping polish is explicitly fine. That reframes the gate as pure
  latency protection rather than a data-derived cutoff: polish costs
  ~0.4-0.6s on a short clip, which is what stands between PTT feeling
  instant and feeling laggy. min_audio_s = 5 in [polish]; the mode check in
  Transcriber.run() replaced with the duration check. PTT dictations >= 5s
  now get polished for the first time.
- Reversal is one config line (raise min_audio_s or disable polish), which
  matched his stated appetite: "if it turns out that we don't want
  everything polished, we can easily turn back the clock."

## 2026-08-24 — polish timeout, context window, and silent-fallback fixes (branch: streaming)

- A 183.8s dictation (2,508 chars) hit the 8s polish timeout and pasted raw;
  the silent fallback meant it read like polish did a bad job when polish
  never ran. Day one of genuinely long dictations found the ceiling: the
  measured cost model (polish_s ~ 0.15 + 0.0035/char) puts 2,500 chars at
  ~9s. Second-opinion review (Fable) before fixing.
- timeout_s 8 -> 30, derived not guessed: max_recording_s=300 bounds the
  worst dictation to ~16-18s of polish, and the fail-fast the 8s bought was
  mostly illusory on loopback (Ollama down = instant connection refusal, no
  timeout involved; the only scenario 8s helps - accepted connection then
  hang - has never occurred in the logs, while it guaranteed failure on
  post-eviction cold loads).
- Added num_ctx=8192 to the polish request (was silently running at Ollama's
  4096 default; verified live via /api/ps). The looming failure was nasty:
  past ~7,500 chars of dictation, Ollama truncates the FRONT of the prompt -
  the instructions - and the model free-runs on bare dictation text with
  nothing in the API response admitting it. ~250MB extra KV cache, trivial.
  Verified the model reloads at 8192 and responds.
- Polish failures now set flash_error (red pill flash via existing plumbing)
  so a fallback is visible instead of silent. Guard vetoes (ratio/question)
  deliberately don't flash - those mean polish ran and was overruled by
  design. A timeout also kicks a background re-warm so a cold-load failure
  heals itself for the next dictation.
- Follow-up caught live: the num_ctx fix introduced a restart tax - the
  warmup request sent no options, loading the model at Ollama's 4096
  default, so the first real polish after every restart forced a full
  reload at 8192 (measured 4.11s on a two-sentence PTT clip). Warmup now
  sends the same num_ctx as real requests.

## 2026-08-24 — Phase A streaming shadow (branch: streaming, off polish)

- Started the streaming plan shelved on 2026-08-23 (the three-model-reviewed
  Phase A). No streaming code yet: after each dictation pastes normally, a
  shadow pass runs Silero VAD over the same audio at three
  min_silence_duration_ms candidates (500/700/1000) and logs the segment
  bounds streaming *would* have cut at — one INFO headline line in
  wisprclone.log, one raw JSON record per job in vad_shadow.log (bounds,
  durations, timings, and a char count; never transcript text). A week or so
  of real dictations answers which pause threshold produces segments worth
  transcribing before the expensive build starts.
- Runs post-paste so it can't be felt, bails between passes the moment a
  real job is queued (worst-case delay to a queued dictation: one sub-second
  VAD pass), and wraps in its own except so a shadow failure can't flash
  the pill. No config knob — the branch is the switch; checking out polish
  removes the shadow entirely.
- No new dependencies: faster-whisper 1.2.1 already bundles the Silero model
  whisper's own vad_filter uses, warm in-process.
- Wrote the analysis half early (mine_streaming.py, sibling of mine_vocab.py)
  so the threshold decision is one command whenever enough records exist,
  instead of a session of work later. Per-setting table over toggle records:
  segments per dictation, merge-rule and force-cut pressure, and estimated
  felt latency from an affine cost fit (whisper_s ~ a + b*audio_s over the
  existing 344 job lines; polish_s ~ a + b*chars over shadow records) -
  affine because whisper's cost is nearly flat to ~28s, so a per-second rate
  would understate short-segment cost ~3x. Verified against synthetic data
  with hand-computed answers: both fits recover exact coefficients, every
  table column matches, torn trailing JSON lines are skipped. Real-data run
  sanity-checked too (fit predicts 0.61s polish on a 132-char dictation that
  measured 0.58s).

## 2026-08-24 — repo cleanup pass (branch: polish)

- Asked for a redundancy/dead-weight sweep of the whole repo. Two real
  findings out of it: removed wisprclone.ico, tracked in git but unused -
  the tray icon has always come from ui.py's _icon_image(), drawn with PIL
  at runtime, not loaded from a file. Confirmed the tray icon is unaffected
  before deleting. Also de-duplicated the whisper-model reasoning that
  appeared word-for-word in both README.md and SETUP.md - README now just
  states the choice and points to SETUP.md, which keeps the actual
  reasoning, matching the intended split (README narrative, SETUP
  instructional). A third finding (two near-identical hover/flash color
  blocks in ui.py's Pill._render, for the checkmark and dismiss-X) was
  left as-is - cosmetic-only, no behavior difference, not worth the
  abstraction. Checked .gitignore/git ls-files while at it: nothing
  personal (history.log, corrections.txt, wispr_flow_history.txt) is
  actually tracked.

## 2026-08-24 — clipboard history/cloud-sync exclusion (branch: polish)

- Compared WisprClone against three other open-source Wispr Flow clones
  (drajb/whisper-local, nexos-1/localflow, zerodrive16/LocalFlow). Most of
  what they do WisprClone already does as well or better (polish guardrails,
  UIA-based paste targeting, per-pixel-alpha pill); three candidates got a
  closer look, only one held up.
- Added: dictated text now gets marked with the ExcludeClipboardContentFrom-
  MonitorProcessing / CanIncludeInClipboardHistory / CanUploadToCloudClipboard
  formats (Clipboard._mark_transient() in transcribe.py) on every clipboard
  write, including the post-paste restore of the user's own prior clipboard
  contents. Windows Clipboard History and Cloud Clipboard sync are both off
  on this machine today, so nothing was actually leaking, but Win+V is a
  one-keystroke prompt away from turning history on, and CLAUDE.md already
  commits to "no dictation audio or text leaves it." Cheap (12 lines, no new
  dependency, no added latency) defense-in-depth.
- Tested and verified: a standalone script confirmed Clipboard.set_text()
  actually sets all three exclusion formats (present=True, value=zeroed
  DWORD) on real clipboard writes. Then verified live - turned on Windows
  Clipboard History, restarted the app, dictated with a Ctrl+C'd control
  item also on the clipboard: the control item showed up in Win+V, the
  dictation did not, across two separate dictations. One stale pre-fix
  clipboard entry (dictated before the restart picked up the new code)
  showed up once on the first check and was cleared before the clean runs.
- Decision: Clipboard History stays on going forward - genuinely useful once
  discovered, and now that it's verified safe against dictations, there's no
  reason to turn it back off. This was preemptive, not reactive: nothing was
  leaking before the fix, but leaving history on afterward would have been
  the actual risk without it.
- Cloud Clipboard sync's CanUploadToCloudClipboard flag was not live-tested
  (would need a second device signed into the same Microsoft account). Left
  unverified deliberately: this machine uses a local account, and Cloud
  Clipboard sync is gated behind a Microsoft account, so the setting isn't
  just off, Windows has no account to sync through at all. Revisit only if a
  Microsoft account is ever added to this machine.
- Rejected: a hotwords-echo guard (discard a transcript that just echoes the
  hotwords bias list back) - zero occurrences across 719 logged dictations
  against a pipeline that already stacks vad_filter, the no_speech_prob
  filter, min_recording_s, and trailing-silence trim. The guard's own
  false-positive case (dictating a single hotword alone, e.g. "Ollama") is
  more likely to fire than the bug it guards against.
- Rejected: WASAPI native-rate capture + soxr resample (whisper-local's
  technique for avoiding shared-mode resampling artifacts). Doesn't apply -
  the default mic here runs via MME, not WASAPI, so whisper-local's own code
  would record at 16k anyway on this setup. Would also add real fragility to
  Recorder.reopen()'s device-following logic for a quality difference that's
  below the noise floor of a mic already benchmarked at ~19dB SNR.

## 2026-08-23 — polish revival, latency hunt, code review (branch: polish, uncommitted)

- Revived the LLM polish pass (revert of its Aug 21 removal) to test whether it
  beats the ever-growing clean_text() regex list. New model:
  qwen2.5:7b-instruct via a reinstalled Ollama, chosen for conservative
  instruction-following over raw writing quality.
- Chased polish latency, assuming it was model cost. It wasn't: `requests` to
  `localhost` on Windows tries IPv6 ::1 first and eats ~2s per call before
  falling back to IPv4 where Ollama listens. Hardcoding 127.0.0.1 cut polish
  from ~2.3-3s flat to ~0.6-1s. Biggest single win of the day.
- Whisper model settled on large-v3-turbo after testing all three: distil felt
  instant but had rough edges, large-v3 heard best but added ~1s on short PTT
  bursts, turbo gives distil-class speed with near-large-v3 accuracy.
- Rewrote the polish prompt after it turned a question into a statement:
  hard rules now forbid dropping/merging/reordering sentences, changing
  questions, and (restored after review) adding anything. Live diffs of every
  polish edit are logged for auditing.
- Fixed Whisper end-of-clip hallucination ("Let this happen" repeated four
  times): trailing-silence trim before transcription plus
  condition_on_previous_text=False.
- Added a tray Restart item (relaunches via the venv launcher after releasing
  the single-instance mutex).
- High-effort code review of the whole branch: 10 verified findings, 7 fixed
  same day — teardown now drains live recordings and in-flight jobs so
  Restart can't eat a dictation, trim threshold capped so a mouse-click pop
  can't cut quiet trailing speech, warmup got its own bare-load request and
  generous timeout, proxy env vars ignored for the Ollama call, relaunch
  guarded with a schtasks fallback, decode kwargs unified, stale discard flag
  cleared on record start. Deferred: sub-32ms edge race (streaming redesign
  will fix properly), sentence-count polish guard (needs more evidence),
  README correction (merge time).
- First log-mining pass: mine_vocab.py ranks personal-vocabulary candidates
  from dictation history (summary output only); a curated 12-term hotwords
  list in config.toml now biases Whisper toward the words it used to mishear.
  Verified live: Xeon, Ollama, SOQ, ClaudeMD all clean on first transcription
  with no corrections firing. Watch for hotword insertion (over-biasing).
- Rewrote pill rendering for smooth edges: PIL draws each frame at 4x and
  Lanczos-downsamples, pushed via UpdateLayeredWindow with per-pixel alpha.
  The old tk-canvas + color-key path couldn't antialias (color-key
  transparency is binary per pixel), which is why the edges looked ragged.
  Bonus: transparent corners are now click-through, and identical frames
  skip the redraw. Verified live across all five pill states.
- Slimmed the recording pill 25% (76px -> 57px, bars 16 -> 10 to keep their
  spacing) after it sat over the Claude Code status bar text. Compared against
  the real Wispr Flow first: its bar fully blocks text underneath, so the
  clone's translucent pill just needed to be smaller, not moved.
- Separately diagnosed why the app stopped auto-starting at boot: Windows
  silently skips Startup-folder shortcuts whose target has the RUNASADMIN
  flag. Replaced the shortcut with a Task Scheduler task (RunLevel Highest).
- Designed but did not start a segment-streaming architecture (transcribe and
  polish during natural pauses while still speaking; ~1s felt latency at any
  length). A second-opinion architect review refined the plan: offline Silero
  VAD shadow phase first, then streaming with sentence-holdback polish as one
  milestone. Parked until the current setup's feel is proven insufficient.
- Added SETUP.md (first-person walkthrough for building your own copy of
  this instead of subscribing to Wispr Flow) and corrections.txt.example.
  Fixed two stale README claims while at it: "Things I tried and reversed"
  still said the polish pass was removed entirely, and Scope still framed
  this as a portfolio piece not meant to be run. Doing this now rather than
  waiting for merge - publishing SETUP.md next to a known-false claim two
  sections up didn't make sense.

## 2026-08-22

- Normalized quiet recordings toward a target peak before transcription; the
  mic sits 2-2.5 ft away and was capturing ~-17 dBFS, hurting accuracy.
- clean_text() grew: immediate word stutters, comma-flanked "you know",
  leading "and". This growth is what reopened the LLM-polish question.
- Repaste popup suppressed when pasting into a terminal; tray hover-highlight
  and frozen word-count fixes.

## 2026-08-21 — baseline

- Working local Wispr Flow replacement: mouse-button PTT, CUDA whisper,
  clipboard paste with save/restore, tray icon, pill overlay, pre-roll,
  audio ducking, single-instance mutex.
- First LLM polish pass (llama3.2:3b) added for toggle mode, then disabled,
  then removed entirely the same day: latency plus word-substitution risk
  outweighed the benefit at the time. Punctuation bridging between
  consecutive dictations met the same fate (false positives in chat use).
- Model switched to distil-large-v3. Tray word counter, result-pill
  interactions, corrections.txt regex-template fix, startup mic guard.
