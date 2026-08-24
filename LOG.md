# Development log

Newest first. Decision-level: why things changed and what testing showed.
Diff-level detail lives in git history.

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
