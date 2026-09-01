# Bug and incident history

LOG.md records every meaningful commit, decisions included. This file is
just the failures, backfilled from LOG.md plus incidents that never
produced a commit at all. Anything that crashed, hung, lost a dictation,
pasted wrong text, or had to be diagnosed and worked around belongs here.
Newest first, same as LOG.md. Each entry covers symptom, root cause, fix,
and status.

## 2026-09-01 — Clips just over 30s hallucinated a tail and stalled ~5s (root cause of the Xeon x73 case)

A dictation slightly longer than 30s could paste invented text at the end
(the 2026-08-28 "Xeon" x73 run, since backstopped by `_RUNAWAY_REPEAT`) with
its whisper time jumping from the usual ~0.6s to 5s. Root cause:
`WhisperModel.transcribe` cuts audio into fixed 30-second windows, so a 30.6s
clip ends with a 0.6s window holding no speech but still carrying the hotword
prompt. Whisper fills it with invented words, and because that output fails
the compression-ratio check it retries at five higher temperatures before
giving up, which is the 5s. Reproduced offline on the retained WAV (three
runs, 5.5-6.2s, a different invented tail each time, one of them "Merci
d'avoir regardé cette vidéo !") and on the 34.6s dictation of 2026-09-01
12:23 (4.7-5.8s, tail "Sq4"), whose live run happened to come out clean.
About 4% of dictations run past 30s. Fix: jobs now run through
faster-whisper's `BatchedInferencePipeline`, which cuts windows at VAD
silences instead. Both clips transcribe clean in 0.4-0.7s, and all 32
retained clips over 25s run in 17.9s total versus 38.3s. Fixed on the
`batched` branch (LOG.md, same date) and verified live the same day: a
68.5s toggle dictation transcribed in 1.33s with a clean tail. The
2026-08-29 regex guard stays as the backstop.

## 2026-08-31 — Repaste pill flashed on clean pastes into VS Code and eBay

Dictating into VS Code's editor pane or eBay's message compose box showed
the click-to-repaste pill even though the text pasted fine, implying a
miss that never happened. The pill decision trusted two signals, a
visible system caret or a UIA Edit control in focus, and both read False
in those apps for the same reason: Monaco and eBay's composer draw their
own caret and report as a UIA Document, not Edit, and Document is
deliberately excluded because counting it would hide the pill on
read-only web pages. Fix replaces the guess with ground truth, the same
UIA field read the continuation stitch uses: after the Ctrl+V, re-read
the focused field and only show the pill when the pasted text failed to
appear - at the field's end, or newly anywhere in it for a mid-document
paste. Fields reading empty or exposing no UIA text keep the old
heuristic, terminals and menu-blocked pastes are unchanged. Fix committed
2026-08-31 (see LOG.md same date). Live retest same day: VS Code and eBay
both clean, but CalCareers' login page (calcareers.ca.gov, Username
field) still flashes the pill on a paste that visibly landed - which part
of the check misfires there isn't known yet. A `landed miss:` diagnostic
line now logs the check's inputs whenever it fails, so the next dictation
on that page should pin down the branch. Still open.

WisprClone crashed to desktop with no Python traceback, right after a new
NVIDIA display driver installed (screen blinked black, then came back). Diagnosed from the Windows
Application Error / WER event log plus a full minidump analysis, parsed
with a pure-Python minidump parser since no Windows debugger was
installed. Root cause was the driver swap itself. The process was holding
a CUDA context from before the reinstall, a driver reinstall invalidates
that context underneath an already-running process, and the next CUDA
call failed with CUDA_ERROR_UNKNOWN, code 999. The literal string
"Returning 999 (CUDA_ERROR_UNKNOWN) from cuMemFreeAsync" was found
directly in the crash dump's memory. ctranslate2, faster-whisper's
inference engine, has no exception handler on its internal worker thread
for this failure path, so it calls std::terminate()/abort(). The WER
report labeled the crash STATUS_STACK_BUFFER_OVERRUN (0xc0000409), which
looked like memory corruption but wasn't. That status is Windows' generic
bucket for any __fastfail() call, and the dump's embedded fail-fast
reason code was 7 (FAST_FAIL_FATAL_APP_EXIT, a clean abort()), not 2 (a
real stack-cookie failure). A controlled crash, not corruption.

The same driver swap also produced exactly one Ollama polish 500. A
second investigation traced it to the same root cause. Ollama's runner
process (llama-server.exe) survived the swap, unlike WisprClone, but lost
its own CUDA allocations the same way and failed one internal request.
Ollama's scheduler detected the broken runner and respawned it, so it had
already self-healed by the time the investigation finished, confirmed
working on a follow-up request. Diagnosed only, no code fix. This is a
CUDA/driver-layer failure mode outside the app's control. The operational
mitigation is to restart WisprClone after any GPU driver update, and to
expect at most one transient Ollama failure that heals itself.

## 2026-08-29 — mine_segment_polish.py UnicodeEncodeError on CJK/Cyrillic output

Surfaced during the analysis_tools/ relocation testing. When piped output
includes a transcript containing CJK or Cyrillic characters and no
console encoding is set, the script hits a UnicodeEncodeError. It's
pre-existing, not caused by the move, and was left alone per that
commit's relocation-only scope. PYTHONIOENCODING=utf-8 works around it
meanwhile. Open, low priority.

## 2026-08-29 — Moving the mining scripts broke their paths and imports

The relocation of the 7 mine_*.py scripts into analysis_tools/ broke all
of them two ways. Every script located the repo root's logs and config
via `BASE = Path(__file__).parent`, wrong the instant they moved a level
deeper, fixed to `.parent.parent`. And three scripts import `transcribe`
directly, which only works when the running script's own directory is on
sys.path, true next to transcribe.py and false after the move. The second
breakage wasn't in the initial scope and was caught in review before
merging. Fixed in b5bcde2, verified with real runs of all 7 scripts from
the new location, not just import checks.

## 2026-08-29 — Whisper repeated "Xeon" 73 times and cleanup read it as emphasis

Whisper hallucinated "Xeon" 73 times in a row, comma-separated, in one
dictation (2026-08-28). `_STUTTER` is deliberately written to skip
comma-separated repeats so real emphasis like "very, very important"
survives, which means it read the hallucination as emphasis and let all
73 through. Not a new failure mode. The 2026-08-23 "Let this happen" x4
case was the same thing, and that fix (trailing-silence trim plus
condition_on_previous_text=False) is still in place but evidently not
sufficient on its own. The corpus showed real speech tops out at 3x exact
repeats and the hallucination sits at 73x, nothing in between, so a new
`_RUNAWAY_REPEAT` regex collapses 4+ exact repeats of the same word,
comma or not, and logs a warning naming the word and count. Review caught
a real gap in the first draft before merge. It ran before
emphasis_words.txt loaded, so a protected word like "no" at 4+ repeats
would have been silently mangled despite the explicit opt-out. Fixed in
5c69461, verified against the real 73x case, the real 3x and 2x survivors,
the 4x boundary, and protected-word exemption. Update 2026-09-01: the root
cause turned out to be Whisper's fixed 30s windowing leaving a near-empty
tail window; see that date's entry. The regex stays as a backstop.

## 2026-08-28 — Desktop shortcut icon went blank

The Aug 24 cleanup pass (c9b0ae0) removed wisprclone.ico as an unused
tray-icon asset after correctly confirming the tray icon is drawn at
runtime in ui.py, not loaded from a file. But the sweep only checked the
repo, and the Desktop shortcut's IconLocation still pointed at that exact
path, a reference living outside the repo where nothing in-repo would
surface it. The desktop icon went blank until the file was restored from
git history. Fixed in 4a91572.

## 2026-08-28 — Smart App Control blocked PyAV's DLL and killed the app at startup

Windows Smart App Control started blocking `av\audio\frame.pyd` (PyAV, a
faster_whisper dependency) with no local trigger. No Windows update, no
file change, the same file had run clean for a week, and the Code
Integrity event log showed it had never been blocked before. A Fable
deep-dive confirmed this is documented Smart App Control behavior, not a
bug on Microsoft's side. Cloud reputation verdicts on unsigned binaries
are cached with an expiry and requeried around reboot/logon, so a
previously-fine file can flip to blocked with no local cause, and no
per-app allowlist exists short of turning Smart App Control off entirely.
The fix is narrower than that. `av` is only used by faster_whisper's
decode_audio(), which this app never calls (every transcribe() call
passes a numpy array, never a file path), so transcribe.py now wraps
`import av` in a try/except that stubs a placeholder module into
sys.modules when the real import fails, logging a warning with the real
exception text. Tested against the live block, which had moved to
`av\codec\codec.pyd` by commit time, confirming the block roams within
the package. Fixed in a445ba4. Known limitation, no stub is possible if
Smart App Control ever targets a load-bearing unsigned binary like
ctranslate2 or onnxruntime.

## 2026-08-27 — Stray leading space on nearly every paste

Dictated text almost always started with a space, invisible in a chat box
that trims on send but genuinely there. The leading-space decision was
keyed off `self.last_hwnd is None`, true exactly once per app run, rather
than off whether the destination field had anything in it, so every
dictation after the first got a space prepended unconditionally. The fix
reuses the continuation-stitch logic's UI Automation read of the focused
field's real text, no space for an empty or whitespace-terminated field,
a space when there's real trailing content. The old special case was also
subtly wrong the other way, skipping the space when the first dictation
of a session landed in a field with prior content. Unreadable fields and
terminals keep the old always-space behavior deliberately. Fixed in
e5405bf.

## 2026-08-27 — A context menu opening mid-PTT swallowed the whole dictation

Holding PTT and accidentally right-clicking a context menu open made the
entire dictation vanish. No paste, no repaste pill, nothing logged. An
open menu (native Win32 modal loop or a Chromium/Firefox popup) eats the
injected Ctrl+V instead of the text field, the 300ms clipboard restore
then wipes the only copy, and the existing landed-check (caret_visible())
stayed quiet because the field behind the menu never lost its caret.
Deterministic, every affected dictation failed the same way. A competing
theory (the right-click physically disturbing the held PTT button) was
ruled out because the mouse hook filter ignores non-PTT buttons and that
mechanism would predict a truncated paste, not total silence. Fix:
paste_blocked() detects an open menu three ways (menu-mode flags,
mouse-capture ownership, UIA focused-menu element) and skips the
keystroke, forcing the repaste pill instead, with continuation state left
intact (782fefd). Live testing the same day showed the first version's
behavior (poll up to 5s, auto-paste the instant the menu cleared) was
wrong for real usage, where the interrupted dictation should be something
to click on, not text landing unprompted. The poll was deleted in favor
of one instantaneous check, which also makes the pill appear immediately
(ae64bfa). Fixed. A menu that opens and fully closes before transcription
finishes is still invisible to this, a much narrower window.

## 2026-08-27 — idle_close_s raised over a clipping theory, reverted same day

A latency review flagged the 10s mic idle-close as a first-syllable
clipping risk. At 10s the mic suspended between nearly every pair of
dictations, so almost every dictation started with a 50-300ms device
reopen and an empty pre-roll, and the value was raised to 300s (7b135b1).
Direct feedback the same day established the diagnosis was theoretical.
Across the entire life of the 10s value, before and after the change, not
a single felt clip of a first word ever happened, while the mic indicator
staying lit for up to 5 minutes was a real, felt cost. The mistaken
change is the bug here. Fixed by reverting in 8b2cf38.

## 2026-08-27 — mine_streaming.py overstated streaming's latency win about 4x

The felt-latency model in mine_streaming.py charged polish only against
the final chunk's share of characters, quietly assuming per-segment
polish already worked, the exact assumption the segment-polish experiment
ruled out the same day. The correction was described in prose in that
entry but never actually made in the script. Once fixed (polish charged
in full on both sides, plus an explicit win column), the re-run over 160
real toggle records showed a 0.14s median win at the 500ms front-runner
where the old table implied roughly 4x that. The corrected numbers drove
the decision to shelve the streaming build. Fixed in 5f551a7.

## 2026-08-27 — qwen silently dropped whole sentences, uncaught by any guard

Replaying all 405 corpus cases against the live qwen2.5:7b-instruct
config showed 3 dictations (0.7%) lost a whole sentence or
meaning-bearing clause outright, caught by none of the ratio, question,
or profanity guards. Real content loss, distinct from the profanity-drop
mode already fixed. Fix: `_lost_sentence()` in `_polish()`. A sentence
counts as surviving only if at least half its content words appear
anywhere in the output, with stopwords, filler, and digits excluded so
legitimate edits can't trip it, and the first failing sentence rejects
the polish result and ships raw text (polish_status=dropped_sentence).
Verified with exact reproduction of all 3 real drops and zero false fires
across the 405 replayed cases. Fixed in 9c01ad1.

## 2026-08-27 — dolphin-mistral runaway generation with no length cap

Two inputs under 3.5s of audio sent dolphin-mistral into runaway
generation during the replay, 170s producing 25k+ characters ("Damn" was
one input) before the ratio guard saw the result. The guard only patched
over the root cause, which was that the Ollama request had no
num_predict, so nothing bounded generation length at all. Fix: a cap
derived from the input, max(64, len(text)/4 * max_ratio), reusing the
existing [polish] max_ratio so it scales with the dictation. Verified
against the real "Damn" case, dolphin still misbehaves (fabricates a fake
dictation) but stops at the 64-token floor in ~2.6s, the ratio guard
still rejects it, and raw text still ships. Harmless in live use
(both inputs sat under the polish gate) but real. Fixed in 856c6c3.

## 2026-08-27 — Polish silently stopped editing after the dolphin swap

The 8/25 swap to dolphin-mistral traded a real problem for a worse one,
and nothing logged it until the new diff logging made it visible. A
dictation came back completely unpunctuated despite polish_status=ok,
reproduced byte-identical across three prompt variants, and across every
polished dictation since the swap 77% came back with zero edits. A
405-case offline replay of both models showed dolphin no-ops 71% of
dictations vs qwen's 31%, worst exactly in the 300-600 char range polish
exists for. The latency reasoning behind the original swap was also
wrong. The "qwen is 2.5x slower" reading was a cold-model-load artifact,
and warmed medians are a wash (0.97s vs 0.95s). Swapped back to qwen, and
since the replay showed prompt-based swear preservation doesn't hold
reliably for either model, the prompt approach was replaced with a
deterministic swear-count guard. Count profanity in vs out, reject and
ship raw on any drop (polish_status=dropped_profanity). Fixed in 1aa1eee.

## 2026-08-27 — _STUTTER silently corrupted every legitimate "that that"

The pre-WisprClone Wispr Flow corpus (2,291 dictations) contains 12
legitimate "that that" uses ("now that that's out of the way").
history.log since WisprClone went live contains zero, because _STUTTER
had been silently collapsing every one, corrupting the grammar roughly
once every day or two at current dictation volume. The bug stayed
invisible for a week because the regex layer left no log trail for
mine_polish to audit. Fixed by adding "that" to emphasis_words.txt
(applies live), and the observability hole was closed separately.
clean_text() now logs the same raw/out diff block as polish whenever it
changes text (296ddea). Fixed.

## 2026-08-27 — ~0.25s of nearly every dictation was GPU idle clock ramp

A fresh-eyes latency review explained a standing discrepancy. The live
PTT whisper median was 0.52s while back-to-back benchmarks measured
0.33s, and the gap was the GPU idling its clocks down between dictations,
which sit minutes apart in real use. The same 7s clip measured 0.56-0.61s
after 45s of GPU idle vs 0.30-0.33s repeated immediately. Fix: a warm
sentinel enqueued at record start transcribes 1s of zeros and discards
it, so the clock ramp happens while the user is still talking (bfad38d).
Live verification then showed the idle-bench assumption wrong under real
desktop load. The boost held only ~2s after the warm, not the 4-5s the
bench suggested, leaving ~4s holds a coin flip. A bounded re-warm loop
(every 2s while recording, giving up 15s in) closed that (e3cdc71).
Verified from clock-gated cold states, warmed dictations land at the
0.26-0.28s hot floor. Fixed.

## 2026-08-26 — Repo docs had 9 real mismatches, one wrong since the first commit

A Sonnet review pass plus an independent Fable verification checked all
tracked files against actual code, config, and git history and found 9
real doc/code mismatches. Stale CLAUDE.md file lists and format strings,
a SETUP.md model recommendation whose reasoning no longer applied, stale
README claims including "no network calls," and one Fable-only catch:
README and SETUP.md both said "double-tap Right Ctrl" for toggle mode,
wrong since the very first commit, it has always been a single tap. All
fixed in 582f29f, and the incident produced the standing convention that
doc consistency is checked on every push.

## 2026-08-26 — The swear-preservation fix wasn't actually holding

The first real run of mine_polish.py (180 polish diff pairs) found the
2026-08-25 profanity fix, previously considered resolved, was still
leaking. 4 dropped "fuck", 4 dropped "shit", 1 dropped "damn", including
one the same morning the audit ran. The earlier 3-dictation live
spot-check that looked clean simply wasn't representative of the real
rate. Found in 9103b01 and deliberately not chased at the time (low
frequency, plenty of real swears surviving). Closed for good the next day
by the deterministic swear-count guard in 1aa1eee, which rejects any
polish result that drops a profanity word.

## 2026-08-26 — Silent polish failures looked identical to genuine no-ops

A question about one dictation's missing commas exposed a real log
ambiguity. A job where polish ran and decided no edit was needed looked
identical to one where polish silently failed and fell back to raw, both
just showed raw == text. Mining the full log (687 jobs, 272 with polish
gated in) found two real hidden failures among the ten longest zero-edit
dictations, a 183.8s dictation that hit an unhandled exception and a
106.1s one that timed out at the 30s ceiling. Fix: `_polish()` returns
(text, status) and the job line carries polish_status= (ok, skipped,
suspicious, dropped_question, timeout, error). Fixed in 839c21c.

## 2026-08-25 — Diverging-restart cleanup rule unreliable at temperature 0

A polish prompt rule meant to catch a diverging-wording restart ("I want
to grab, I want to get the keys" -> "I want to get the keys") tested
clean 3/3 offline on short text but failed on a real, longer live
dictation, and re-running that exact sentence 4 more times gave a third,
different, still-wrong result each time. Root cause is GPU float
non-determinism. Temperature 0 isn't truly deterministic on near-tied
logits, and this sentence lands exactly there, the model's confidence
split three ways with no reliable winner. Not harmful (worst case is a
no-op or partial edit, never corruption), just not solid enough to ship.
The rule was reverted out of POLISH_PROMPT. Open.

## 2026-08-25 — _STUTTER collapsed intentional emphasis

The stutter-collapse regex used to collapse any doubled word
unconditionally and silently, so "very, very important" pasted as "very
important" with no log trail. Fixed by making it comma-aware (a comma is
Whisper's own signal the speaker paused and repeated on purpose) plus a
new emphasis_words.txt for the fast no-pause emphasis the comma heuristic
can't cover. Verified live, "saw saw" still collapses and "very, very
important" survives. Fixed in b0556cf. The non-comma legitimate-repeat
gap resurfaced later with "that that" (see the 2026-08-27 entry).

## 2026-08-25 — qwen sanitized profanity despite the prompt

qwen2.5:7b-instruct was censoring dictated speech despite the polish
prompt's keep-the-speaker's-wording rule ("dog shit" -> "rejected",
"weird shit" -> "weird"), inconsistently within the same session.
Temperature 0 vs 0.1 A/B testing showed output still varied at temp 0
(GPU float non-determinism on near-tied logits), proving the model was
confidently choosing to censor, not randomly guessing. Fixed at the time
by swapping to dolphin-mistral with an explicit preserve-all-profanity
prompt rule (4608676). That fix later proved both leaky (see 2026-08-26)
and a bad trade overall (see the dolphin no-op entry). The durable fix is
the deterministic swear-count guard in 1aa1eee.

## 2026-08-24 — A 183.8s dictation silently pasted raw after an 8s polish timeout

Day one of genuinely long dictations found the ceiling. A 183.8s
dictation (2,508 chars) hit the 8s polish timeout and pasted raw, and the
silent fallback made it read like polish did a bad job when polish never
ran. The measured cost model (polish_s ~ 0.15 + 0.0035/char) puts 2,500
chars at ~9s, so the timeout guaranteed failure on long input. Fix:
timeout raised to 30s, derived from max_recording_s bounding the worst
case (the fail-fast the 8s bought was mostly illusory on loopback, since
Ollama being down refuses instantly). Polish failures now flash the pill
red instead of failing silently, and a timeout kicks a background re-warm
so a cold-load failure heals itself for the next dictation. Fixed in
faec567.

## 2026-08-24 — Polish was running at Ollama's 4096-token default context

The polish request sent no num_ctx, so it silently ran at Ollama's 4096
default (verified live via /api/ps). The looming failure was nasty. Past
~7,500 chars of dictation, Ollama truncates the front of the prompt,
which is the instructions, and the model free-runs on bare dictation text
with nothing in the API response admitting it. Never actually triggered,
fixed preemptively with num_ctx=8192 (faec567). The fix introduced its
own regression, caught live. The warmup request sent no options, loading
the model at 4096, so the first real polish after every restart forced a
full reload at 8192, measured at 4.11s on a two-sentence clip. Warmup now
sends the same num_ctx as real requests (05affcd). Fixed.

## 2026-08-24 — The pill is invisible to every GDI screen capture

Found while trying to screenshot the pill. PrtScn, PIL ImageGrab, and
BitBlt even with CAPTUREBLT all come back without it, because
UpdateLayeredWindow content just isn't there on the GDI capture path.
Diagnosed only, platform behavior, no fix. Pictures of the pill come from
rendering Pill._render offline and compositing at _ALPHA.

## 2026-08-23 — requests to localhost ate ~2s of every polish call

Polish latency was assumed to be model cost. It wasn't. `requests` to
`localhost` on Windows tries IPv6 ::1 first and burns ~2s per call before
falling back to IPv4 where Ollama actually listens. Hardcoding 127.0.0.1
cut polish from a flat ~2.3-3s to ~0.6-1s, the biggest single win of the
day. Fixed in 9d4bb65.

## 2026-08-23 — Polish turned a question into a statement

The polish pass rewrote a dictated question as a statement. The prompt
was rewritten with hard rules forbidding dropping, merging, or reordering
sentences, changing questions, and (restored after review) adding
anything, and live diffs of every polish edit are now logged for
auditing. Fixed in 9d4bb65. A deterministic dropped-question guard backs
this up in code.

## 2026-08-23 — Whisper end-of-clip hallucination, "Let this happen" x4

Whisper hallucinated "Let this happen" repeated four times at the end of
a clip. Fixed with trailing-silence trim before transcription plus
condition_on_previous_text=False (9d4bb65). Both are still in place, but
the 2026-08-29 "Xeon" x73 case proved they aren't sufficient on their own
for this failure mode, which is now also backstopped by the
_RUNAWAY_REPEAT guard (5c69461). Fixed.

## 2026-08-23 — Seven latent defects fixed out of a full-branch code review

A high-effort review of the whole branch produced 10 verified findings, 7
fixed the same day. The standouts were data-loss and audio-corruption
risks. Teardown didn't drain live recordings or in-flight jobs, so a tray
Restart could eat a dictation. The trailing-silence trim threshold was
uncapped, so a mouse-click pop could cut off quiet trailing speech. The
rest were warmup getting its own bare-load request and generous timeout,
proxy environment variables ignored for the Ollama call, the relaunch
guarded with a schtasks fallback, decode kwargs unified, and a stale
discard flag cleared on record start so a fast double-tap couldn't eat
the next recording. Fixed across 0ceffee, 240ab9b, and 9d4bb65. Deferred
at the time were a sub-32ms edge race (still deferred, the streaming
redesign that would have fixed it properly was later shelved) and a
sentence-count polish guard, which eventually landed as `_lost_sentence()`
in 9c01ad1.

## 2026-08-23 — Ragged pill edges

The pill's edges rendered ragged because the old tk-canvas path used
color-key transparency, which is binary per pixel and can't antialias.
Rewritten so PIL draws each frame at 4x, Lanczos-downsamples, and pushes
via UpdateLayeredWindow with per-pixel alpha. Transparent corners became
click-through as a bonus. Fixed in f5b1e86.

## 2026-08-23 — App stopped auto-starting at boot

The app quietly stopped launching at logon. Windows silently skips
Startup-folder shortcuts whose target carries the RUNASADMIN compat flag,
which this exe needs so dictation reaches elevated windows. No error, no
log, the shortcut just never runs. Fixed by replacing the shortcut with a
Task Scheduler task (RunLevel Highest), a system change with no code
commit.

## 2026-08-22 — Quiet mic capture was hurting accuracy

The mic sits 2-2.5 ft away and was capturing around -17 dBFS, hurting
transcription accuracy. Fixed by normalizing quiet recordings toward a
target peak before transcription (f457c5e).

## 2026-08-22 — Small UI fixes

Three small defects fixed in one day's pass. The tray word counter could
freeze, the tray menu's hover-highlight misbehaved, and the repaste popup
appeared even when pasting into a terminal, where it shouldn't. All
fixed 2026-08-22.

## 2026-08-21 — Startup mic guard

Day one's baseline entry names a "startup mic guard" fix with no further
detail in LOG.md itself; the commit (a77577b) is titled "Guard the startup
mic open so a missing device can't kill the app." A missing or unavailable
mic device at startup could kill the app before anything else initialized.
Fixed by guarding the startup mic open.

## 2026-08-21 — Day-one polish and punctuation bridging pulled for bad output

The first LLM polish pass (llama3.2:3b) was added, disabled, and removed
entirely the same day. Latency plus word-substitution risk outweighed the
benefit. Punctuation bridging between consecutive dictations met the same
fate after false positives in chat use. Both removals were the fix at the
time, and both ideas were later rebuilt properly, polish revived
2026-08-23 with guards and logging, bridging replaced 2026-08-25 by UI
Automation continuation stitching that reads the field instead of
guessing from a timer. A corrections.txt regex-template fix also landed
the same day. Fixed.
