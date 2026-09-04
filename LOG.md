# Development log

Newest first. Decision-level: why things changed and what testing showed.
Diff-level detail lives in git history.

## 2026-09-04 — Moonshine v2 (Base) smoke test: rejected on latency, not accuracy

Same day as the Granite 4.1 round, tested Moonshine (Useful Sensors), which
markets itself on latency rather than leaderboard WER - the real question
wasn't whether it hears well, it was whether it's actually faster than the
already-optimized turbo path. Dispatched with an explicit instruction not
to nest a sub-agent or set up its own monitor this time, after the Granite
4.1 round left one running for an hour past completion (see the entry
below and BUGS.md same date).

Smoke test (23 clips, duration-spread plus targeted digit/hotword clips)
cleared the accuracy bar every non-Whisper candidate before it had failed:
real sentence case and punctuation on 22 of 23 clips (Granite 3.3 managed
0 of 990), and it kept digits as digits where every other candidate wrote
them as words. Still fails the WisprClone hotword the same way as
everyone else (0 of 3), and Moonshine has no hotword-biasing mechanism at
all to fix that with.

What actually killed it was the thing being tested. turbo's near-fixed
~0.17s overhead beats Moonshine's steeper per-second cost past about 2
seconds of audio, and the corpus median dictation is 5.7s. Projected
across all 1,274 real clips, turbo finishes the whole set in about 349s
to Moonshine's 671s - roughly 1.9x slower overall, worse on longer clips
(3.5x on a 24s clip). Stopped at the smoke test rather than running a full
pass on a question the fit already answered. Rejected. Six candidates now
tested (Parakeet, Canary-Qwen, Granite 3.3, Granite 4.1, large-v3,
Moonshine); this is the first to actually clear the punctuation/digit bar,
and the first rejected purely on speed rather than accuracy or casing.
Tables in analysis_tools/results/README.md; data in
analysis_tools/results/2026-09-04/ (gitignored).

## 2026-09-04 — Granite Speech 4.1 2B bake-off: stays on turbo

Curiosity-driven, not a specific complaint about turbo. A web search for
what's shipped since the 9/2 round of bake-offs turned up Granite Speech
4.1 2B, IBM's follow-up to the 3.3 2B rejected that day for emitting
all-lowercase, unpunctuated text - 4.1's release notes claim a fix
("punctuation and truecasing... with a simple prompt change"), a specific
reason to retest rather than just a new leaderboard number.

Sonnet agent, same method as the 3.3/Canary/Parakeet rounds, run against
the full current 1,272-clip retained_audio corpus (up from 886-990 in the
prior rounds) with a fresh turbo baseline generated under the current
Whisper prompt rather than reusing the stale 9/1 one. The fix is real:
switching to the model card's documented punctuation-prompt row (3.3's
basic-ASR prompt still returns normalized text on 4.1 too) produces real
sentence case and punctuation on 99%+ of clips, up from 0% on 3.3. Word
accuracy also improved to 2.9% disagreement, the best of any non-Whisper
candidate tested. But the pattern that killed every prior candidate held
anyway: 0 of 19 correct on the WisprClone hotword (always "Whisper Clone,"
two words), digits still written as words, and latency about 5x turbo's
median, over 11x at p95. Stays on turbo. Four non-Whisper candidates now
tested against this corpus (Parakeet, Canary-Qwen, Granite 3.3, Granite
4.1) plus large-v3 itself, all rejected on some flavor of the same gap:
leaderboard WER doesn't predict hotword/digit/latency fit. Tables in
analysis_tools/results/README.md; data and scripts in
analysis_tools/results/2026-09-04/ (gitignored).

## 2026-09-04 — Polish trial closed out in README; a stale 21%->14% figure fixed

README's "Things I tried and reversed" no longer leaves the polish trial
open-ended - closed out with the three-way explanation (the rewritten
Whisper prompt, corrections.txt, and clean_text()'s regex layer) for what
replaced it without polish's latency or meaning-inversion risk. Also fixed
config.toml's `[polish]` comment and the 2026-09-03 results entry below,
which both misquoted the 548-dictation study's punctuation-only share as
21% instead of the source table's 14%.

## 2026-09-03 — Second confirmation polish should stay off: interview dictations replayed offline

Offline replay of the 9b (`_polish`'s real prompt/options/guards) against
27 dictations from an unrelated session (building an About Me file) - 89%
no-op, only 3 edits accepted, one of which inverted a live self-correction's
actual meaning ("most of my childhood, no, all of my childhood" -> "most of
my childhood", the wrong value kept) with none of the four guards catching
it. A second independent sample landing on the same call as the 9/1
decision. Tables in analysis_tools/results/README.md.

## 2026-09-02 — Start/stop cue, second attempt: mute the buffer instead of tuning volume

Second build of the start/stop cue reverted earlier the same day (see the
two entries below and BUGS.md same date) after it bled the start clip's
own sound into the mic and turned a dictated "pill" into "pale." That
build's own writeup had already named the fix it didn't try: mute a known
window of the recorded buffer while the clip plays, instead of routing
the sound away from the input path or just tuning volume down. This
attempt builds that mute.

`audio.py`'s `Recorder` gained `set_start_mute_seconds()`: called once at
startup with the start clip's own measured duration (`Cue.start_duration_s`,
read from the WAV header, not guessed), it sets a sample count
(`mute_samples`) that `_callback()` zeroes out of the buffer starting at
the rising edge - the same edge that already pulls in the pre-roll and
pins `_job_mode`. Only the start cue needs this: it plays the instant
`start_recording()` is called, which is also when real speech might
begin, where the stop cue plays after speech has already ended and never
overlaps the buffer at all. A `CUE_MUTE_MARGIN_S` constant (50ms) pads the
measured clip duration, because `cue.play()` fires from `tick()`'s
rec-state edge check same as the `Ducker`, not the button-press instant
itself, so it can lag the real press by up to one 33ms tick, plus a bit
more for `winsound.PlaySound`'s own async dispatch before sound actually
reaches the speakers - 3685 samples (230ms) total for the real
`dictation-start.wav` (180ms clip + 50ms margin). Muting zeroes samples
in place rather than dropping them, so recording duration/sample count is
unaffected; a 5ms linear taper at each edge of the window (`_mute_gain()`)
avoids leaving a hard on/off discontinuity that would itself click if
this stretch were ever played back. Everything else carries over from the
first attempt unchanged: winsound temp-file playback (`SND_MEMORY |
SND_ASYNC` still isn't supported), the `cue`/`cue_volume` knobs, not
ducked (`PlaySound` goes straight to the default device).

Tested offline (can't launch the real elevated app from here): `Cue`
built and loaded against the real clips, measuring `dictation-start.wav`
at 0.1803s; a standalone test drove `Recorder._callback()` with synthetic
blocks around a `start_recording()` call and confirmed 3685 samples
muted, buffer sample count unchanged, blocks fully after the window
untouched byte-for-byte, and the fade edges monotonic and landing near
1.0/0.0 as designed. Also checked cue-disabled and a mute-window-inside-
one-block case. Still needs a live check: tray Restart, then a real PTT
dictation starting the instant the button's pressed, to confirm the
first word actually comes through clean this time.

## 2026-09-02 — Pill centers on the Claude Code compose box, not the screen

Uriah noticed the pill sat dead-center on screen regardless of where he was
actually typing, and asked whether it could follow the Claude Code app's
chat box instead, specifically accounting for that app's own internal
terminal/file side panel shifting the compose box left without moving or
resizing the app's own window (so anchoring to the window's bounds alone
wouldn't have worked). Handed to a Fable agent at max effort to investigate
feasibility before building anything, since it was a real unknown, not a
routine change.

Verdict: feasible, and the app's accessibility tree turned out to be well
authored, not an opaque web surface. Its compose box is a named `Edit`
element (`name='Prompt'`) with a live bounding rectangle that sits inside
a chat-column group distinct from the side panel and the splitter between
them. New `Anchor` thread in ui.py polls `GetForegroundWindow` every
100ms; when the Claude Code exe is in front, it finds that Edit element
via UI Automation (`comtypes`, the same library transcribe.py already
uses for reading focused fields) and publishes its center x, throttled to
one tree search per second and a cheap rect read every cycle after that.
`Pill._place()` uses that center when available, falling back to the old
screen-center behavior for every other app or when UI Automation fails.
Dragging still works but only moves the pill vertically now; release
snaps it back to the anchor's x.

Live-verified after the app auto-restarted from a PC reboot (Task
Scheduler picked up the uncommitted change automatically): the pill
tracked the compose box, including shifting position when Uriah closed
the terminal side panel while a recording was held. wisprclone.log shows
no "UI Automation unavailable" or "anchor read failed" lines. Multi-monitor
DPI scaling is untested (Claude Code only ran on the primary monitor during
testing); the compose box's `Prompt` name and the `claude.exe` process
name are both app-version strings that would need updating if either
changes upstream, but a mismatch degrades to the old screen-center
behavior rather than crashing.

## 2026-09-02 — Start/stop cue reverted: the start clip bled into the mic

Built earlier the same day (Wispr Flow's own dictation-start/stop clips,
played through winsound on the recording state edge), and reverted a few
hours later. The build's own writeup already flagged the risk: the start
clip plays at the same instant the mic goes hot, and the mic is close
enough with no echo cancellation that the clip's own audio could bleed
into the recorded buffer and land on top of the first word. It did.
Uriah's own dictation of "pill" came back "pale" immediately after
pressing the button and starting to speak right away.

No threshold or volume tweak attempted - reverted outright (`git revert`
of the cue commit) rather than lowering `cue_volume` or trying to mute a
guard window in the recorded audio, since the ask was to remove the sound,
not tune it. `sounds/dictation-start.wav` and `dictation-stop.wav` deleted
locally too. A future revisit would need a way to keep the cue audible
without it reaching the mic, e.g. routing it away from the input path
entirely or muting a known window of the recording during playback -
nothing here explored either.

## 2026-09-02 — Chunk joins no longer capitalize a continued sentence

Built the fix the calibration entry below proposed. transcribe.py's
segment join is now `join_segments()`: when the previous chunk of the
batched pipeline didn't end with . ! ? (a trailing "..." counts as a cut
and is dropped), the next chunk's first word is lowercased, unless it's I
or I'm-style, a capitalized word from the Whisper prompt or a
corrections.txt target, a word with a capital past its first letter
(YouTube, GPU, CLAUDE.md), or a word Whisper capitalized mid-sentence
anywhere else in the same dictation (Windows). Tested before wiring in:
17 unit cases, a replay over the 586 joins from the 8s-chunk calibration
with the whole-clip transcript's case as truth (clips under 30s, where
that truth is clean: 156 fixed, 2 real regressions, "Spanish" and
"Thursday" lowercased), and production chunking over the 16 retained
clips that split into two or more chunks (18 joins, 11 changed, every
change a continuation that now reads as one sentence). Joins only exist
in dictations over about 30s, so the exposure is small either way. The
mirror artifact, a period Whisper puts at a cut chunk's end mid-sentence,
is left alone: nothing in the text distinguishes it from a real sentence
end. Tables in analysis_tools/results/README.md.

## 2026-09-02 — Whisper prompt switched from a hotword list to three sentences

Applied the prompt-style result (entry below). config.toml's `hotwords`
knob is now `prompt`, and transcribe.py passes it to faster-whisper as
`initial_prompt` instead of `hotwords`; the library ignores hotwords once
an initial prompt is set, so the vocabulary has to live in the sentences.
The prompt is Uriah's own three sentences from the chat, which scored best
on vocabulary of the three tested (WisprClone right in 15 clips against
the list's 9), plus "and the CLAUDE.md file" folded into the second
sentence to carry ClaudeMD; that addition wasn't in the tested text.
mine_merge_rule.py's tail-prompt helper now overrides the app prompt
rather than passing initial_prompt twice. Live check after restart.
corrections.txt also gained the "<name> MD" to "<name>.md" lines the same
day (no general rule: AMD is a real word here).

## 2026-09-02 — Whisper prompt style test: a punctuated prompt fixes the missing final period

Uriah asked whether Whisper could do the polish itself. Not as a second
pass, but its prompt shapes its style, and the app's prompt was a bare
comma list of hotwords. Rewrapping the same hotwords as one punctuated
sentence and running turbo over the 981 baseline clips cut the clips that
end without punctuation from 291 to 19 (real sentences of 6+ words: 198 to
7), with 0.7% word disagreement, hotword recall slightly up (WisprClone 9
to 13), fillers unchanged, and no latency change. That's the dominant quirk
of the polish-off trial (one paste in four last night) removed by a config
string. The trade is a few spelling conventions shifting: "alright" comes
out "all right" under the formal prompt, and "grock" becomes "grok". Tables
in analysis_tools/results/README.md. Not yet applied to the app.

## 2026-09-02 — Granite Speech 3.3 2B bake-off: stays on turbo

Second candidate of the day, run the same way (Sonnet agent, same baseline,
tables in analysis_tools/results/README.md). Rejected on the smoke test's
first line before the pass even finished: Granite emits normalized text,
all lowercase, no capital in any of 990 clips and sentence punctuation in
a fifth of them. That's a transcript for scoring word error rate, not for
pasting into a chat box, and fixing it would mean a second model in the
pipeline. The rest matched Canary: 5.0% word disagreement, numbers as
words on 26 clips, the WisprClone hotword never, 1.3s median against
0.24s, and the 76s clip came back empty. Five candidates against one
baseline now; the pattern holds and the queue is empty.

## 2026-09-02 — Canary-Qwen 2.5B bake-off: stays on turbo

Top of the Open ASR Leaderboard for English (5.6% WER against large-v3's
7.4%) and, unlike Parakeet, built on a language-model decoder, so it looked
like the one candidate that might write digits and skip fillers on its own.
It doesn't. Over 981 retained clips through the same method as the Parakeet
and large-v3 runs (tables in analysis_tools/results/README.md), it agreed
with turbo on 96.5% of words, and the disagreements were the familiar
ones: numbers as words on 22 clips, three times the filler words kept, the
WisprClone hotword never once ("Whisperclone" in all 14), compounds split
("wi fi", "screen shot"). Hearing differences were rare and went both ways.
Two new problems on top: it dropped the second half of the longest
dictation (76s, 235 words to 131) and a sentence from a 69s one, and it is
slow, 1.2s median against 0.24s, nearly 3s on a typical toggle dictation
and 7s over 30s, at 10 GB of VRAM. Rejected. That's three non-Whisper and
one Whisper candidate tested against the same baseline in two days, all
rejected on the same pattern: leaderboard accuracy measured on normalized
text says nothing about digits, fillers, hotwords, or long-clip behavior,
which is what this app actually depends on.

Process note, now in CLAUDE.md: the run went to a Sonnet agent with a
progress file and the main chat supervising, and cost about 3% of a usage
window against the Fable max-effort polish run the day before that got cut
off by the limit. The supervision earned its keep once: NeMo's install
quietly replaced the CUDA torch wheel with a CPU one, and the milestone
line that said so is what stopped a two-hour CPU pass with junk timings.

## 2026-09-01 — large-v3 re-tested against large-v3-turbo on the batched path: stays on turbo

With polish off and the batched pipeline making Whisper cheaper, the
original "large-v3 hears best but costs a second" call was worth re-checking
on real data. Both models ran the 886 retained clips through the live path
(`Transcriber.pipe`, the app's decode options and hotwords). No ground
truth, so this is disagreement plus a read of what the disagreements are.

The two agree on 95% of words and 81% of clips outright. Most of the rest
is style, not hearing: large-v3 writes "going to" and "want to" where turbo
writes what was said ("gonna", "wanna"), fuses compounds ("gitignore",
"venv", "onto", "servicenow"), and keeps fillers turbo drops ("you know",
"uh", "I mean"), which the regexes then remove anyway. Real hearing
differences went both ways and were rare: v3 got "grok" where turbo wrote
"grock", turbo got "fuck Jordan" where v3 wrote "photjourn". v3 also ignored
the WisprClone hotword in all nine clips that say it ("WhisprClone",
"WhisperClone"), while turbo got it right.

Two costs decided it. Latency: v3 was slower on 886 of 886 clips, median
0.49s against 0.23s, 0.98s against 0.34s on 10-30s clips, 1.9s against
0.64s over 30s. And a silent one: v3 returned nothing on 18 clips (2%)
that it had in fact transcribed correctly, because it scores confident
speech with no_speech_prob 0.60-0.86 and the app's segment filter drops
anything over 0.6. Turbo never crosses that line on real speech. A switch
would have meant retuning that filter or losing one dictation in fifty
outright. Turbo stays. Tables in analysis_tools/results/README.md; data and scripts in
analysis_tools/results/2026-09-01/ (gitignored); the method is the same as
the Parakeet entry above.

## 2026-09-01 — Polish switched off as a trial

Same day as the model swap, after reading what the 9b's edits actually
were. Across the 548 replay inputs the polish returned the text untouched
69% of the time, changed only punctuation or capitalization in 14%, added
just a final period in 7%, and touched words in 10%: stammer restarts
removed in 6%, fillers in 2%, a word changed or added in 2%. Whisper already
punctuates, and the regexes already catch single-word stutters, so the pass
was costing about a second on every dictation over 8s (40% of them) for
word-level work on one in ten of those. Uriah's read: rarely used, and not
substantial when it is.

config.toml `[polish] enabled = false`; nothing else changed. With the flag
off the app never contacts Ollama (the warm-up is gated on it too), so the
9b drops out of VRAM on its own after its 24h keep-alive. The trial is
judged from history.log: pasted dictations that end without a period, and
phrase-level stammers ("would then would that") the regexes can't catch,
are the two things the polish was actually fixing. If they show up often
enough to notice, the fix is one config line and a restart; the model and
the guards stay in place.

## 2026-09-01 — Polish "no change" sentinel tested and dropped

With 69% of the 9b's polishes returning the text untouched, each one still
pays the full retype (0.87s median on those inputs, 2.4s on a live 25s
dictation). Three prompt variants tried over the same 548 replay inputs,
all on qwen3.5:9b, no code changed:

- Current prompt plus "if the text needs no changes, output exactly
  NOCHANGE": 388 of 548 outputs were identical to the input, and the model
  used the sentinel on 4 of them. It retypes by habit. When it did fire the
  polish took 0.30s instead of 0.87s, so the saving is real, just never
  claimed.
- Sentinel instruction moved to the front with an example, probed on 40
  no-op plus 20 edited inputs: fired on 16 of the 40 no-ops and wrongly on
  7 of the 20 edited ones, skipping edits. Worse both ways.
- A separate YES/NO "does this need cleanup" question (0.22s) before the
  full polish: said NO to 30 of 40 no-ops but also to 8 of 20 edited
  inputs, and the edits it would have skipped were the missing final
  periods and commas the polish mostly exists to add. Net: about half of
  polishes save ~0.65s, a third pay 0.22s more, and one in eight loses its
  punctuation fix. Not worth it.

Dropped. The remaining latency levers are the model itself (gemma4:e4b at
0.69s median, with its number rewriting) or a higher min_audio_s; neither
taken today.

## 2026-09-01 — Polish model switched to qwen3.5:9b after a four-model replay

The 7b was two model generations old, so the same day's review asked
whether a newer small model does the polish job better. Four models ran the
exact `_polish()` request over 548 real inputs (317 from the log's polish
diffs, 231 from the day's Whisper pass through clean_text, clips over 8s):
qwen2.5:7b-instruct as the baseline, qwen3.5:4b, qwen3.5:9b, gemma4:e4b.
Scored on the real guards imported from transcribe.py, word-level edit
distance, deleted and added content words, changed numbers, swears kept,
residual fillers removed, and latency, plus thirty outputs read blind.

The baseline lost. The 7b deleted a real word from 122 of its 534 accepted
outputs (23%), added one to 52, tripped the guards 14 times, and in the
blind read was the model that rewrites: "Wait, no, fuck that" became "What
I'd like you to do is fuck that", "no fucking idea does anyway" became "no
fucking idea why", and half the samples came back shorter and tidier than
what was said. The prompt forbids all of it and no guard can see it. That is
the "rougher" the 2026-08-31 entry pinned on the 3b; the 7b does it too with
better grammar. The 9b deleted a word in 10 outputs (2%), added one in 5,
tripped one guard, changed no numbers, kept every swear in all 44 swearing
inputs, and returned 69% of inputs untouched. Cost: median 1.05s against
0.76s alone on the GPU (about 2.1s vs 1.6s on a 30s dictation), 4.1s cold
load against 2.6s, 5.2 GB VRAM against 4.8. The 4b was only 0.08s faster
than the 7b, spelled digits out as words in 12 outputs and added semicolons
in 17, so no downsize. Gemma matched the 7b's speed with far less damage and
the best filler cleanup, but reformatted numbers ("830" to "8:30") and loads
in 5.7s; it's the fallback if the 9b's latency grates.

Two code lines: `"think": False` in both Ollama request bodies. Qwen 3.5 and
Gemma 4 reason by default and spent the whole num_predict budget on it,
returning empty. It's a top-level field, not an option, and qwen2.5 accepts
and ignores it, so the rollback is the config line alone. The replay was
Ollama's raw-prompt shape throughout, which these models' templates pass
through unchanged. Verified the real `_polish()`/`_warm_polish()` path
against the 9b offline before restart; live check after. The bake-off
agent was also the first to hit the 16 GB card's limit: three resident
models next to Whisper dropped the 9b to 19 tok/s, so after the switch
`ollama stop qwen2.5:7b-instruct` once rather than leaving both loaded.
The 7b and 3b stay pulled. Still open from the review: the "no change"
sentinel, less compelling now that 69% of polishes are no-ops that cost
about 0.5s each.

## 2026-09-01 — Parakeet bake-off: stays on Whisper

Last open item from the review. NVIDIA Parakeet TDT 0.6B v2 (English-only,
better Open ASR Leaderboard word error rate than large-v3) ran over all 886
retained dictations through onnx-asr 0.12.0 on CUDA, in a scratch venv, and
Whisper ran the live path (`Transcriber.pipe` with the app's decode options)
over the same files. Parakeet was faster on 885 of 886 clips: median 0.11s
against 0.23s, whole pass 110s against 232s. Word disagreement between the
two was 3.8% of the corpus, 68% of clips identical after lowercasing, and
almost all of the gap was style rather than hearing.

Decision: don't switch. The tenth of a second saved is under the bar the
streaming decision set (0.14s median, shelved 2026-08-27) and disappears
next to a multi-second polish. The style differences all run against the
app: Parakeet keeps every uh, um and stutter (28 clips with fillers against
Whisper's 8; phrase repeats like "I don't I don't" get past the single-word
`_STUTTER`), spells numbers as words on 48 clips ("three hundred and
seventeen pounds", "Nvidia Fifty Ninety"; 26 of those under the 8s polish
gate, so they'd paste as-is, and Whisper's digits were right every time),
and has no hotword biasing (WisprClone came out "Whisperclone" in all 15
clips, Claude "Clod" in 7 of 23). Profanity counts were identical, neither
model sanitizes. The two hallucination clips were clean on both, so the
batched pipeline had already closed Whisper's own gap. Real mishearings
were rare in both directions. There is also a packaging blocker:
onnxruntime-gpu can't share a venv with the CPU onnxruntime faster-whisper
pulls in for Silero VAD, so Parakeet would have to be a full replacement,
not a fallback. PyPI's onnxruntime-gpu 1.29.0 is a CUDA 13 build and
silently fell back to CPU here; the CUDA 12 build from ORT's own index
worked.

Revisit only if a number-formatting step lands in the ONNX path or Whisper
hallucinations return on the batched pipeline. No code changed; the
`parakeet` branch was created for the test and deleted empty.

## 2026-09-01 — Hotwords pruned to words actually dictated; five clean_text corruptions fixed

Second batch out of the same review, also on the `batched` branch.

**Hotwords.** The config carried 12 words. Counting them in history.log
over the app's twelve days: qwen, SC2 and x64 were never dictated, DDR3
four times, Xeon in four lines, and one of those lines is the x73
hallucination itself. A hotword is the prompt Whisper sees on every
window, and an empty window echoes the prompt (see the batched-pipeline
entry below), so a word that never gets said is insertion risk with
nothing on the other side of the ledger. Trimmed to WisprClone, Wispr
Flow, Claude, ClaudeMD, Fable, Ollama, SOQ. SOQ stays despite two uses:
61 in the Wispr Flow corpus, job-application vocabulary that resumes in
October. corrections.txt still maps Xion=Xeon, so the common mishearing is
covered without the bias.

**clean_text.** The 73 raw/out diffs the regex layer has logged since
2026-08-27 held five real corruptions, each fixed in place - one quirk per
pattern, no new pattern added beyond a shared boundary anchor:

- `_YOU_KNOW` stripped the phrase out of "we're good, you know what I
  mean?" (pasted as "good what I mean?"). A comma before "you know" no
  longer suffices when a question word follows; that form now needs a
  comma after it too. history.log held a second victim: "Okay, you know
  what? Let's undo that one" had pasted as "Okay what?".
- `_SENTENCE_START` and `_LEADING_AND` treated the period of "a.m." and
  the last dot of an ellipsis as a sentence end ("1050 a.m. This morning",
  "probably... Doesn't", "took... Not even a minute"). Both now share
  `_SENT_END`, which excludes an ellipsis and a.m./p.m./e.g./i.e./etc./vs.
- The space-before-punctuation glue ate dot-prefixed names: "the .venv
  file" pasted as "the.venv file". It now fires only when the punctuation
  ends a token.
- `_STUTTER` collapsed "had had" (past perfect). "had" joins "that" in
  emphasis_words.txt and in the .example, whose header now says grammar
  doubles belong there too.
- `_STUTTER` and `_RUNAWAY_REPEAT` used `\w+`, so a contraction never
  collapsed: "let's, you know, let's work" had pasted as "let's let's
  work". Both now use `[\w']+`.

Verified by running the old clean_text (git HEAD) and the new one side by
side. 23 hand cases with expected output all pass, including the
protections that must not move ("very, very important", "No, no, no",
"that that's", "Do you know what", "Uh-huh", and the x73 Xeon run still
collapsing). Over the 389 raw transcripts in wisprclone.log the two
disagree on 10 lines, over the 2,046 lines in history.log on 5, and every
one is one of the five fixes landing (plus "I'm I'm looking" now
collapsing, the contraction fix at work). One consequence worth knowing:
"etc. And then I want" keeps Whisper's capital A now, since "etc." no
longer counts as a sentence end and the leading-"and" drop no longer fires
there. Preserving Whisper's own text is the safer side of that.

Live after a tray Restart: the "you know what I mean", "had had" and
".venv" lines pasted intact with no regex change logged, and a spoken
"let's let's" collapsed to one in the log. The a.m. and ellipsis guards
couldn't be triggered on demand (Whisper wrote "10am" and no ellipsis),
so they rest on the offline replay of the real log lines.

## 2026-09-01 — Whisper runs through the batched pipeline; the 30s tail window was the hallucination bug

A full-repo review (Fable 5.1, first session on the new model) found the real
cause behind the "Xeon" x73 hallucination, and a class of latency spikes with
it. `WhisperModel.transcribe` cuts audio into fixed 30-second windows. The
Xeon dictation was 30.6s of audio, which left a final 0.6s window holding no
speech but still carrying the hotword prompt. Whisper fills a window like that
with invented text, and because the result fails the compression-ratio check
it then retries at five higher temperatures, which is where that job's 5.22s
whisper time came from. Reproduced offline on the retained WAV: three
sequential runs at 5.5-6.2s each, a different invented tail every time
("Merci d'avoir regardé cette vidéo !" on one). Today's 34.6s dictation
reproduced the same thing offline (4.7-5.8s, tail "Sq4") even though its live
run happened to come out clean at 1.05s. About 4% of dictations run past 30s,
and every one of them was a coin flip on this.

Fix: `Transcriber` now runs jobs through faster-whisper's
`BatchedInferencePipeline` wrapped around the same loaded model (`self.pipe`;
the CPU fallback gets its own, `self.cpu_pipe`). It cuts windows at Silero
VAD silences instead of every 30s and decodes them together, at a single
temperature and without timestamps. Same model, same decode options, same
`no_speech_prob` filter. `self.model` stays as the bare WhisperModel for the
GPU warm-ups and the mining scripts. 29 lines in transcribe.py.

Verified offline against retained_audio, new path vs old on the same clips,
GPU warmed before each run:

- Xeon clip: 0.41-0.62s with a clean tail, vs 5.45-6.16s with garbage.
  Today's 34.6s clip: 0.47-0.72s vs 4.72-5.80s.
- All 32 retained clips of 25s or more: 17.9s total vs 38.3s.
- The 40 most recent clips under 25s: mean 0.244s vs 0.281s; 2 clips changed
  a word ("git ignore" -> "git-ignore", ".venv" -> ".v env").
- Silence and a 0.4s clip yield zero segments as before, the shortest real
  clip transcribes, the CPU fallback path loads and transcribes, and
  `initial_prompt` through the pipe works for mine_merge_rule.py.

Verified live after a tray Restart: a 68.5s toggle dictation transcribed in
1.33s with a clean tail (the old path took 1.8-2.6s on a 75s clip and
would have windowed this one as 30 + 30 + 8.5s).

The trade is no temperature fallback. On this corpus fallback only ever fired
on the garbage the tail window created, and `_RUNAWAY_REPEAT` still backstops
a real loop. The 2026-08-27 decode A/B (beam 1 plus no timestamps) had shown
wording regressions; the batched path keeps beam 5, and the short-clip diff
above says no-timestamps alone was not the problem.

mine_merge_rule.py now transcribes through `t.pipe` so its parity metric
tracks the live path. mine_segment_polish.py stays on `t.model` on purpose:
it splits at Whisper's sentence-level segment timestamps, which the batched
path does not produce (one segment per VAD chunk). Also out of the review,
not acted on yet: pruning hotwords that were never dictated (qwen, SC2, x64,
DDR3, Xeon), five clean_text fixes with log evidence ("you know what I mean"
losing its "you know", capitalizing after "a.m." and "...", ".venv" glued to
"the", "had had" collapsed, apostrophe words never collapsing), a "no change"
sentinel for the 33% of polishes that return the text unchanged, and a
Parakeet TDT 0.6B v2 bake-off as a possible Whisper replacement (run the
same day, see the entry above).

## 2026-08-31 — Switched polish back to qwen2.5:7b-instruct

Downsizing to 3b (2026-08-28) traded quality for latency, and in daily use
the edits read rougher often enough that it wasn't worth the speed - the
guards catch the worst misbehavior and fall back to raw, but a merely
mediocre edit that passes the guards still ships. Reverted config.toml's
`[polish] model` to 7b-instruct; 3b stays pulled in Ollama in case this
flips again. Unloaded the 3b model from Ollama's memory (`ollama stop
qwen2.5:3b-instruct`) since nothing points at it anymore.

## 2026-08-31 — Repaste pill false positives fixed with a post-paste field read

Dictating into VS Code's editor pane and into eBay's message compose box
popped the repaste pill even though the paste had landed fine. Both
signals the pill decision leaned on fail there for the same reason: those
editors draw their own caret, so `caret_visible()` reads False, and they
report to UI Automation as a Document control rather than Edit, so
`focused_editable()` reads False too - deliberately, since counting
Document would hide the pill on read-only web pages, exactly where it's
needed.

The fix reuses the pattern the continuation stitch established - ground
truth over control-type guessing. After the Ctrl+V, `focused_text()`
re-reads the focused field (paste()'s clipboard-restore sleep has already
given the app time to consume the keystroke) and the paste counts as
landed when the field now ends with the pasted text's normalized tail,
or, for a mid-document paste where content sits below the caret (VS
Code's normal case), when the tail is present now but wasn't in the
pre-paste read the stitch logic already takes. Newly-present matters:
merely "present" would let a short dictation that already existed
somewhere in the field mask a paste that really missed, the exact
false-negative the Document exclusion was protecting against. A field
that reads empty or exposes no UIA text falls back to the old
caret/Edit/terminal heuristic unchanged, terminals skip the read as
always (their UIA text is the whole viewport, not the input line), and a
menu-blocked paste still forces the pill immediately. One side effect
worth having: a paste dropped because the clipboard was busy now shows
the pill too, since the field genuinely lacks the text - the old
heuristic stayed quiet there, and the repaste click re-pastes from
`last_text`, not the never-written clipboard, so the offer actually
recovers that case.

Live retest: VS Code and eBay both fixed, but CalCareers' login page
still flashes the pill on a paste that lands (see BUGS.md same date). A
follow-up commit adds a `landed miss:` log line capturing the check's
inputs on every failure, so the next dictation there shows which branch
misfires - diagnostics only, no behavior change.

Prompted by today's NVIDIA driver crash finding having nowhere to live - no
code changed, so it had no natural commit to hang a LOG.md entry off of. It's
in BUGS.md instead, not here. BUGS.md
is the fix: a standing file for bugs and incidents, whether or not they
produced a commit, separate from this file's commit-keyed decision log.

Full backfill, not just going forward: one Fable pass read all of LOG.md
(955 lines, 08-21 through 08-29) and pulled every genuine bug/incident into
BUGS.md, newest first, each with symptom, root cause, fix, and status. A
second, independent Fable pass re-read LOG.md and the real git log fresh,
built its own list blind, and audited the first pass's draft against it -
checking for omissions, miscategorized entries, wrong commit hashes, and
wrong status labels. Verdict: zero factual, hash, or status errors across
34 entries, ship as-is, with three optional nits (all addressed by hand
before merge: trimmed one added claim the ground truth hadn't stated, added
one thin entry the audit flagged as missing, left one borderline inclusion
as judgment). CLAUDE.md now documents the two-pass process as the standard
for any future large rewrite of either file.

## 2026-08-29 — Moved the mine_*.py scripts into analysis_tools/

Purely organizational, prompted by wanting the repo's file listing to stop
reading as one long undifferentiated list - the 7 offline mining scripts
now live in their own `analysis_tools/` directory, separate from the 4
files that are actually the app (`wisprclone.py`, `audio.py`,
`transcribe.py`, `ui.py`), which stay in root.

Every one of the 7 locates the repo root's logs/config via
`BASE = Path(__file__).parent`, which broke the instant they moved a level
deeper - fixed to `.parent.parent` in all 7. A second breakage the initial
scope missed: three scripts (`mine_ollama_parallel.py`,
`mine_segment_polish.py`, `mine_merge_rule.py`) import `transcribe`
directly, which only works when a directly-run script's own directory is on
`sys.path` - true when they lived next to `transcribe.py`, false once moved.
Fixed by adding the repo root to `sys.path` before those imports. Caught in
review before merging, the same way the `emphasis_words.txt` gap was caught
on the previous fix - Fable's own verification pass is real value here, but
so is a second read before anything lands.

Verified with real runs, not just import checks: all 7 scripts executed
from `analysis_tools/` against the actual repo's logs (some against months
of real data - `mine_merge_rule.py`'s full CUDA replay, `mine_polish_3b.py`'s
full 252-case Ollama replay), both scripts with a CLI path override tested
in both modes. One unrelated latent bug surfaced along the way and was left
alone per the relocation-only scope: `mine_segment_polish.py` can hit a
`UnicodeEncodeError` when piped output includes a transcript with CJK or
Cyrillic characters and no console encoding is set - pre-existing, not
caused by the move, worth its own fix another time
(`PYTHONIOENCODING=utf-8` works around it meanwhile).

## 2026-08-29 — Runaway-repeat guard for Whisper hallucination loops ("the Xeon bug")

Whisper occasionally hallucinates a word repeated dozens of times in a row,
comma-separated - the 2026-08-28 case was "Xeon" x73. `_STUTTER` is
deliberately written to skip comma-separated repeats (so real emphasis like
"very, very important" survives), which means it read the hallucination as
emphasis and let all 73 through untouched. Not a new failure mode either:
an earlier entry below (2026-08-23) shows the exact same thing happening
with "Let this happen" x4, "fixed" at the time with trailing-silence trim
and `condition_on_previous_text=False` - both still in place, evidently not
sufficient on their own.

Checked the actual corpus before picking a threshold rather than guessing:
every exact-repeat run in the whole log tops out at 3x for real speech
("no, no, no", genuine emphasis) and jumps straight to 73x for the one
hallucination - nothing has ever happened in between, including at exactly
4x (a synthetic test, since no real case sits there). New `_RUNAWAY_REPEAT`
regex in `transcribe.py` collapses 4+ exact repeats of the same word, comma
or not, down to one, and logs a warning naming the word and count so a
future occurrence shows up in wisprclone.log directly. Kept as a separate
pattern from `_STUTTER` rather than extending it, per this repo's
one-quirk-per-pattern rule for `clean_text()`.

Caught in review before merging, not after: the first draft ran before
`emphasis_words.txt` even loaded, so a word on that list (which exists
specifically so `_STUTTER` never touches a word the speaker doubles on
purpose) would still have been silently collapsed by the new guard if it
ever hit 4+ repeats. The live file actually contains "no" - a real
"no, no, no, no" would have been mangled despite the explicit opt-out.
Fixed: `emphasis_words.txt` now loads before the runaway-repeat substitution
and a protected word is exempt from it exactly like `_STUTTER`, with no
warning logged either, since a run the speaker asked for isn't a
hallucination.

Verified against real log data both before and after the emphasis fix: the
real 73x Xeon case still collapses correctly, the real "no, no, no" (3x)
and "easily, easily" (2x) still survive untouched, the exact-4x boundary
collapses, normal `_STUTTER` behavior is unaffected, and a protected word
at 4+ repeats now survives with zero warning while an unprotected one at
the same count still collapses and logs. `mine_polish.py`'s audit text
updated in three places to say "below 4x" instead of unconditionally, so
its own claims about what's left alone stay accurate.

## 2026-08-28 — Polish downsized to qwen2.5:3b-instruct for speed

The two earlier polish-model swaps (qwen -> dolphin-mistral -> qwen) were
both about correctness - profanity sanitizing, no-op rate. This one is
purely about latency: while testing dictation feel, polish was consistently
the larger half of total processing time, and unlike whisper (whose cost
scales with audio length and was already about as fast as it gets),
polish's cost is really about which model answers the call.

Replayed the current corpus (232 raw dictations - log rotation had eaten
the rest of the original 405 used for the qwen-vs-dolphin decision) through
`qwen2.5:3b-instruct` with `_polish`'s exact prompt and options, scored the
same way that decision was: qwen2.5:7b no-opped 31% of the time; the 3b
model no-opped only 13% - it edits more, not less, so it doesn't fail the
dolphin way. Mean latency 0.63s vs 7b's ~1.4s, roughly half. 8% of its
edits tripped a `mine_polish.py`-style quality flag (mostly a crude
sentence-count check, plus 4 dropped swears out of 201 edits) - the live
swear-count/dropped-question/ratio/`_lost_sentence` guards already catch
all of those and fall back to raw, so the real cost is polish occasionally
not firing, not corrupted output.

Confirmed live, not just in the replay: a real 24.4s toggle-mode dictation
right after the switch polished in 0.77s, versus 1.36s for a 23.6s
dictation on 7b earlier the same night - nearly identical audio length,
nearly half the polish time. `qwen2.5:7b-instruct` stays pulled in Ollama
in case this needs reverting. The replay script (`mine_polish_3b.py`) is
now committed too - a genuinely reusable comparison tool in the same
`mine_*.py` mold for the next time a model swap needs judging.

## 2026-08-28 — Restored wisprclone.ico

The Aug 24 cleanup pass (see below) removed this as an "unused tray icon
asset" after confirming the tray icon is drawn live in `ui.py`, not loaded
from a file - true, but the sweep only checked the repo, and the Desktop
shortcut's `IconLocation` still points at this exact path. Missed because
that reference lives outside the repo, so nothing in-repo would surface
it. Desktop icon went blank until this got restored from git history.

## 2026-08-28 — Guarded the `av` import against Smart App Control blocks

Windows Smart App Control started blocking `av\audio\frame.pyd` (PyAV, a
faster_whisper dependency) this morning with no local trigger - no Windows
update, no file change, same file that had run clean for a week. Checked
the Code Integrity event log: that file had never been blocked before
today. A Fable deep-dive confirmed this is documented Smart App Control
behavior, not a bug - cloud reputation verdicts on unsigned binaries get
cached with an expiry and requeried around reboot/logon, so a previously-
fine file can flip to blocked (or back) with no local cause. Also
confirmed against Microsoft's own docs: no per-app allowlist or
supplemental-policy exception exists for it, so turning Smart App Control
off entirely was the only lever on that side.

Narrower fix: `av` is only used by faster_whisper's `decode_audio()`,
which this app never calls - every `transcribe()` call here passes a
numpy array from the mic, never a file path. Added a try/except around
`import av` in transcribe.py that stubs a placeholder module into
`sys.modules` if the real import fails, so faster_whisper's own `import
av` succeeds against the stub instead of crashing the app at startup.
Logs a warning with the real exception text when the stub kicks in,
silent otherwise.

Tested against the live block (still active at commit time - it had moved
to `av\codec\codec.pyd` by then, confirming it roams within the package
rather than sticking to one file) and a simulated block, plus a silent-
pass check when `av` imports normally. Restarted the real running
instance afterward and confirmed a clean `started` line in
wisprclone.log instead of the crash. Doesn't cover other unsigned
binaries in the venv (ctranslate2, onnxruntime) if Smart App Control ever
targets one of those instead - no stub is possible there, since they're
load-bearing.

## 2026-08-27 — Fixed a stray leading space on nearly every paste

Reported bug: dictated text almost always started with a space, invisible
in a chat box that trims on send, but genuinely there. Root cause: the
leading-space decision was keyed off `self.last_hwnd is None` - true
exactly once, on the very first dictation since the app started - not off
whether the destination field actually had anything in it. Every
dictation after the first got a bare space prepended unconditionally,
empty field or not.

Fix: the continuation-stitch logic already reads the focused field's real
text via UI Automation, so that same read now decides the leading space
too - no space for an empty or already-whitespace-terminated field, a
space when the field genuinely has trailing content to separate from. The
`last_hwnd is None` special case is gone entirely; it was also subtly
wrong, since it skipped the space even when the very first dictation of a
session landed in a field with real prior content. An unreadable field
(no UIA text pattern - the same population the stitch logic already falls
back on) keeps the old always-space behavior, since a stray space is
harmless where a chat box trims it, but a missing one glues words
together and needs hand-editing. Terminals are unaffected: their UIA text
is the whole viewport, not the input line, so they never read the field
and keep pasting with a space, same as before.

Live-tested the menu-blocked-paste fix below the same day and refined it
on the spot. The first version polled up to 5s for the menu to close,
then auto-pasted the instant it cleared. Real usage doesn't work that
way: the actual sequence is notice the stray right-click, stop talking,
close the menu - and at that point the interrupted dictation should be
something to click on, not text landing unprompted the moment the menu
happens to clear.

Simplified rather than added a third state: since "menu cleared during
the wait" and "menu outlasted the wait" now want the identical outcome
(skip the keystroke, force the repaste pill), the poll loop had nothing
left to decide - `wait_paste_clear()` and its 5s threshold were deleted
entirely in favor of one instantaneous `paste_blocked()` check at paste
time. Side benefit, not just simpler: the pill now appears immediately
instead of up to 5s late. A menu that opens and fully closes before
transcription finishes is still invisible to this (same as before -
detecting that would mean tracking menu state through the whole
recording, not just at paste time), but that's a much narrower window
than the case this exists for.

## 2026-08-27 — Fixed dictation loss from a menu opening mid-PTT

Reported bug: holding PTT, accidentally right-clicking to open a context
menu, and the whole dictation vanishing - no paste, no repaste pill,
nothing logged. Root cause: an open menu (native Win32 modal loop, or a
Chromium/Firefox popup) eats the injected Ctrl+V instead of the text
field getting it, the 300ms clipboard restore then wipes the only copy,
and the existing landed-check (`caret_visible()`) stayed quiet because
the text field behind the menu never lost its caret - the check measures
"an editable field is focused," which stayed true throughout, so it was
never going to catch this. Deterministic, not a timing fluke: every
affected dictation would have failed the same way.

Ruled out a competing theory (right-click physically disturbing the held
PTT button, discarding the recording before any paste attempt existed):
the mouse hook filter early-returns on any button that isn't the
configured PTT one, a system-wide low-level hook isn't affected by
another process's own modal loop, and - decisively - that mechanism
would predict a pasted fragment cut off at the right-click, not total
silence, which contradicts the actual symptom.

Fix: `paste_blocked()` in `transcribe.py` detects an open menu three ways
(GUI menu-mode flags, mouse-capture ownership, UIA reporting a focused
menu element - covers native Win32 and both browser-engine popup styles).
The paste now waits up to 5s for the menu to clear before sending Ctrl+V;
if it outlasts the wait, the keystroke is skipped entirely (a stray "v"
can activate a menu item) and the repaste pill is forced instead, with
continuation state left untouched so the next dictation still stitches
against the last real paste, not a skipped one.

## 2026-08-27 — idle_close_s reverted 300 -> 10

Raised earlier today over a clipping-risk theory: a reopened mic loses its
pre-roll and pays a 50-300ms device-open delay, and at 10s that reopen
preceded 87% of real dictations (measured from history.log). Reverted the
same day on direct feedback: across 10s having been the long-standing
value before today's change, and both before and after it, not a single
felt clip of a first word. The indicator staying lit for up to 5 minutes
was the real, felt cost; the clip it was meant to prevent never showed up
in practice. Preferring the indicator clearing quickly since the
theoretical risk it traded away turned out to not matter day to day.

## 2026-08-27 — Real streaming shelved

The multi-day streaming plan (Silero VAD chunking during recording so
Whisper starts transcribing before the user stops talking) never got a
firm go/no-go. Today's segment-parallel-polish work exposed that
`mine_streaming.py`'s felt-latency model was wrong in a way that mattered
to that decision: its formula charged polish only against the final
chunk's share of characters, quietly assuming per-segment polish already
worked - the same assumption the segment-polish experiment just ruled out.
The correction was described in prose in that entry but never actually
made in the script.

Fixed the formula (`mine_streaming.py`): polish is whole-transcript only
(no viable alternative - segment-parallel doesn't parallelize, since
Ollama serializes requests on this machine, and blind per-piece polish
severs continuations at the cut), so it costs the same with or without
streaming and now gets charged in full on both sides. Added an explicit
`win` column (felt latency saved vs today's pipeline) instead of an
unanchored absolute number. Re-run over 160 real toggle records:

    ms   win  p90win
   300   0.2s   0.7s
   400   0.2s   0.6s
   500   0.1s   0.7s
   700   0.0s   0.7s
  1000   0.0s   0.4s

At the 500ms front-runner, median win is 0.14s and only 13/160 toggles
(8%, all 100s+ dictations) save more than a second. The old table implied
roughly 4x that. Considered and rejected redoing the pause-threshold pick
against Whisper's own segment timestamps instead of Silero: a live
chunker has to decide where to cut *before* transcribing, so Whisper
timestamps (which only exist after a chunk is transcribed) were never a
usable signal for this - Silero remains correct, the 500ms pick stands.

Decision: shelve the streaming build. The corrected win doesn't clear the
cost of a live VAD chunker in the audio path (chunk-boundary risk, the
still-unsettled merge rule, mid-stream failure handling) in the
most correctness-critical part of the app. `vad_shadow.log` and
`retained_audio/` are left running rather than torn out, in case 60s+
dictations become common enough to reopen this - see their updated
CLAUDE.md/config.toml notes. The one path that could still matter later:
polishing earlier chunks *while the user is still talking* (distinct from
the already-dead parallel-after-stop design) isn't blocked by Ollama's
serialization, only by the same boundary-quality risk - worth a right-
context-aware redesign of `mine_segment_polish.py` before ever revisiting,
not a reason to reopen this decision as-is.

## 2026-08-27 — Sentence-drop guard added; segment-parallel polish ruled out

Two things prompted by the same day's earlier finding: today's 405-case
qwen-vs-dolphin replay also surfaced that qwen2.5 silently drops a whole
sentence or meaning-bearing clause on a small slice of dictations, uncaught
by any existing guard - a real-content-loss failure mode distinct from the
profanity-drop one already fixed.

**Guard.** Replayed all 405 corpus cases against the live `qwen2.5:7b-
instruct` config specifically (not the historical era mix): 3/405 (0.7%)
lost a whole sentence outright, none caught by the ratio, question, or
profanity guards. Added `_lost_sentence()` to `_polish()` - a sentence
counts as surviving only if at least half its content words (crude
stemming, stopwords/filler and digits excluded so legitimate edits can't
trip it) appear anywhere in the output; first failing sentence rejects the
polish result (`polish_status=dropped_sentence`), raw text ships instead.
Verified against the merged tree: exact reproduction of all 3 real drops,
zero false fires across 405 replayed cases plus a separate 8-case random
spot-check of legitimate edits.

**Segment-parallel polish: no-go.** Investigated whether splitting a long
dictation at pause boundaries and polishing the pieces in parallel (an
idea raised while discussing streaming's true prerequisites) could cut
polish's cost on long toggle dictations, which today's data showed
dominates over Whisper on anything past ~60s of audio. Killed by a
directly measured fact, not a guess: Ollama serializes requests on this
machine (`OLLAMA_NUM_PARALLEL:1`, confirmed by real concurrent-call timing,
not just the config line) - a 3-way split of a 2508-char dictation wall-
clocked within 1-9% of one whole-transcript call, and each extra piece
adds its own ~0.5-1s call overhead, so splitting can only add total GPU
time under serialization. Quality was a secondary concern anyway: 2-3 of
13 multi-piece test records showed real boundary damage (a mid-sentence
continuation split into a dangling fragment), separate from restart-
stammers, which never occurred across 16 boundaries tested. Net: nothing
to gain on either axis. This also forced a correction to `mine_streaming`'s
felt-latency table, which had quietly assumed working segment-polish -
the real median toggle felt-latency win from streaming is close to zero:
only the rare 60s+ dictation sees a meaningful gain, and only if segment-
polish's boundary-damage risk gets accepted or mitigated. `mine_segment_
polish.py` and `mine_ollama_parallel.py` added as the permanent record of
this finding, matching the existing `mine_*.py` analysis-script family.

## 2026-08-27 — Capped polish generation length

The dolphin-mistral runaway from today's replay ("Damn" -> 170s / 25k+
chars before the ratio guard caught it) had a root cause the guard only
patched over after the fact: the Ollama request had no `num_predict`, so
nothing bounded how long the model could generate before the existing
min_ratio/max_ratio check ever saw the result. Added a cap derived from
the input itself - `max(64, len(text)/4 * max_ratio)`, reusing the
existing `[polish]` max_ratio rather than a new constant - so it scales
with the dictation instead of being one fixed number that either clips
long inputs or does nothing for short ones. Verified against the real
"Damn" case post-fix: dolphin still misbehaves (fabricates a fake
dictation) but stops at the 64-token floor in ~2.6s, the ratio guard
still rejects it, raw text still ships. qwen2.5, the live production
model, is unaffected - it already no-ops on "Damn" in ~0.1s.

## 2026-08-27 — Polish swapped back to qwen2.5, backed by a swear-count guard

The 8/25 swap to dolphin-mistral traded a real problem for a worse one and
nothing logged it until today's new clean_text/polish diff logging made it
visible: a dictation came back completely unpunctuated despite polish
reporting `polish_status=ok`. Reproduced deterministically (byte-identical
output across three prompt variants) and confirmed not a one-off - across
every polished dictation since the swap, 77% came back with zero edits.

Dispatched a Fable agent (max effort, worktree) to build a 405-case offline
replay corpus from history.log + wisprclone.log and run it through both
models with `_polish`'s real request shape. Findings: dolphin no-ops 71% of
dictations vs qwen's 31%, worst exactly in the 300-600 char range polish
exists for; of the 150 inputs qwen's own production era actually needed to
fix, dolphin can only fix 38% of them today. Latency is a wash (0.97s vs
0.95s median, warmed) - my earlier "qwen is 2.5x slower" reading was a
cold-model-load artifact, not a real cost. Neither model fabricated content
on realistic input, but dolphin has a failure mode qwen doesn't: two
inputs under 3.5s of audio sent it into runaway generation, 170s producing
25k+ characters before the ratio guard caught it (harmless in practice
since both are under the 8s polish gate, but real). qwen's real cost:
lightly rewords in ~75% of its edits and silently dropped a real sentence
or hedge in ~1.5-2% of all 405 cases, uncaught by any guard.

Added a third deterministic guard to `_polish`, same shape as the existing
ratio/dropped-question checks: count profanity words in vs out, reject and
fall back to raw text (`polish_status=dropped_profanity`) on any drop. This
replaces the 8/25 approach of asking the prompt to preserve swears, which
the replay showed doesn't hold reliably for either model - both dolphin and
qwen dropped profanity at least once in the 45 flagged replay cases, and
all of those would have shipped silently under the old guards.

## 2026-08-27 — Observability: clean_text edits logged, paste tail timed

The last two review items. clean_text() edited every dictation with no
log trail - the "that that" corruption stayed invisible to mine_polish's
audit for a week because the regex layer left nothing to mine. It now
logs the same raw/out diff block as polish (lead-in "clean_text changed
text:", verified not to trip mine_polish's block parser), including when
cleanup empties the text entirely. The job line gains paste=: polish-end
through the Ctrl+V keystroke (UIA continuation read and clipboard swap
included, the 300ms restore sleep after the keystroke excluded), closing
the one unmeasured span of felt latency. The polish diff and job line
moved after the paste to carry the timing, with the diff kept
immediately before its job line - mine_polish resolves each pair's
mode/status from the next timestamped line, and the continuation line
would otherwise split them. All three log parsers verified against both
line formats; the agent's stub suites were re-run against the merged
tree (its worktree had spawned two commits stale, so its own
verification predated the warm code).

## 2026-08-27 — Two accuracy fixes from the fresh-eyes review: "that that" and idle_close_s

Two of the review's accuracy findings, applied directly (no code):

- **"that" added to emphasis_words.txt.** The pre-WisprClone Wispr Flow
  corpus (2,291 dictations) has 12 legitimate "that that" uses ("realized
  that that strategy", "now that that's out of the way"); history.log
  since WisprClone went live has zero - _STUTTER has been silently
  collapsing every one, corrupting the grammar roughly once every day or
  two at current dictation volume. Applies live, no restart. The regex
  layer's lack of any log trail (this bug was invisible to mine_polish)
  is being fixed separately.
- **idle_close_s raised 10 -> 300.** At 10s the mic suspended between
  nearly every pair of dictations, so almost every dictation started
  with a 50-300ms device reopen and an empty pre-roll - a first-syllable
  clipping risk on every fresh thought, paid for a cosmetic benefit (the
  in-use indicator clearing quickly). 5 minutes keeps the mic hot through
  active work; the indicator still clears once actually away. Takes
  effect on next restart.

## 2026-08-27 — Live testing revised the boost hold to ~2s; bounded re-warm shipped

Live verification of the warm-at-press fix (restart 11:32; five ~4s cold
PTT reps, micro/burst/long rounds, and a 0.3s-interval GPU clock trace):
the warm fires on every press - the trace caught utilization spiking to
100% at press and clocks jumping to 2790MHz - but under real desktop
load the boost holds only ~2s after the warm's work ends, not the 4-5s
the idle bench suggested (ambient desktop GPU activity cycles clocks
every ~5s and drags the boost down). Results: micro holds 0.20-0.22s
reliably, ~4s holds bimodal (0.31-0.33 vs 0.44-0.53, a coin flip on
where the ambient cycle sits at release), 24.3s toggle unchanged at
0.83s. No transcription-quality change anywhere.

Follow-up (second Fable agent, isolated worktree): _warm_gpu is now a
bounded re-warm loop - it re-warms every 2s while Status.recording holds
(new flag, mirrored by Recorder start/stop_recording; stop covers the
too-short discard path), giving up 15s in so a minutes-long toggle never
sustains periodic draw. Re-warms run inline in the worker, never queued,
so a release waits behind at most the one warm in flight (~0.1-0.2s
measured) and then transcribes hot. Worker-loop design chosen over
tick-driven enqueueing because the tick variant can stack two warms
ahead of a release exactly when the GPU is contended. Both constants
derived from measurement, kept in code, not config.

Verified from clock-gated cold states with interleaved controls: 5-6s
holds now transcribe at the 0.28s hot floor where the single-warm build
measured 0.44-0.51; a 17.5s hold lands cold by design (last warm starts
at 14.3s). Stop conditions, the one-in-flight wait (187ms), and
counter/drain safety all demonstrated (22/22 stub suite). Live re-check
after this merge: rerun the ~4s cold reps - they should stop
coin-flipping.

## 2026-08-27 — GPU warm-at-press: hide the idle clock ramp inside the recording

A fresh-eyes latency review found ~0.25s of nearly every dictation was
GPU clock ramp: the card idles down between dictations (minutes apart in
real use), and the same 7s clip measured 0.56-0.61s after 45s of GPU
idle vs 0.30-0.33s repeated immediately - which is the gap between the
live PTT whisper median (0.52s over 669 jobs) and back-to-back
benchmarks (0.33s). Decode options were ruled out first: beam 1 /
no-timestamps A/B'd on 24 retained WAVs bought only ~50-70ms and
introduced real wording regressions, so beam 5 stays.

Fix (Fable agent, isolated worktree, offline verification):
Recorder.start_recording() enqueues a {"warm": True} sentinel when the
device is CUDA and nothing is queued or in flight; the worker transcribes
1s of zeros (VAD off, same constraint as _load's warmup) and discards
it, so the clock ramp happens while the user is still talking. The
sentinel is handled before the job try/finally (it never touches the
transcribing counter teardown waits on), skipped without side effects in
the model-load-failure drain loop, and a warm failure is logged and
swallowed - it can never flash the pill or feed the CPU fail latch.

Verified from clock-gated cold states (210MHz confirmed before every
cycle; fixed idle sleeps proved unreliable - desktop GPU use holds
clocks up): control 0.533-0.551s, warmed 0.258-0.285s, which is the hot
floor. Counter/drain safety 12/12 against the real run() loop with a
stubbed model. Measured bound: the clock boost decays ~4-5s after the
warm, so short PTT bursts get the full ~0.26s, the ~6s median gets part
of it, and long recordings land cold exactly as before - never a
regression; streaming owns the long case. Periodic re-warm during long
recordings considered and rejected (sustained GPU draw through
minutes-long toggles).

## 2026-08-26 — Repo-wide doc consistency review, and a standing rule for it

A Sonnet review agent, then an independent Fable verification pass on top
of it, checked all 17 tracked files plus the gitignored ones the docs
reference against actual code/config/git history. 9 real mismatches found
and fixed: CLAUDE.md's Files list was missing the four `mine_*.py` scripts
and `retained_audio/`, its VAD candidate list and job-log format string
were both stale, and it never mentioned Fable dispatches use an isolated
worktree; SETUP.md still told a new reader to pull `qwen2.5:7b-instruct`
with reasoning that no longer applied; README's continuation-bridging
description, "no network calls" claim, and a promised-but-missing
Technical Highlights section were all stale. Fable's pass caught one
Sonnet missed: README and SETUP.md both said "double-tap Right Ctrl" for
toggle mode, wrong since the very first commit - it's always been a single
tap. All fixed same session.

Prompted the standing rule now in CLAUDE.md's Conventions: doc consistency
is a property of every push, checked before pushing, not a periodic audit.

## 2026-08-26 — Polish diff audit script; the swear-preservation fix isn't fully holding

Second of the three "mine WisprClone's own logs" passes planned back on
2026-08-23 (`mine_vocab.py` was the first, already shipped). Built
`mine_polish.py`, a sibling in the same plain-functions/summary-table style
as `mine_streaming.py`: parses every `polish changed text` raw/out pair in
`wisprclone.log` plus every job's `polish_status`, flags anything that looks
like a hard-rule violation (dropped question, dropped swear, sentence-count
drop, a length ratio outside a normal band), and profiles residual filler
that reaches polish uncaught by `clean_text()`.

First real run (180 pairs) found the 2026-08-25 swear-preservation fix -
previously logged as RESOLVED - isn't fully holding: 4 dropped "fuck", 4
dropped "shit", 1 dropped "damn", including one the same morning this audit
ran. The earlier 3-dictation live spot-check that looked clean wasn't
representative of the real rate. Decided not to chase it further for now -
low frequency on words rarely used, and live-verified in the same session
that plenty of real swears do survive intact today. Revisit only if it
actually costs something real. Also found: "you know" reaches polish
uncaught 24 times across 14 dictations (by design - the regex only strips
the comma-flanked form), zero missed stutters or um/uh leakage otherwise.
3 dropped-question cases, all traced to the old qwen model, not current.

## 2026-08-26 — Streaming merge-rule experiment: audio retention + offline simulator

The Phase A shadow (2026-08-24) answered which pause threshold to use.
The next open question - the "merge rule" for a short trailing chunk at the
end of a dictation, the one case that sits on streaming's felt-latency path
- needed real audio, not just segment bounds, since Whisper's accuracy on
an isolated short clip can't be judged from timing data alone.

Extended the shadow pass to also retain each dictation's audio (same
post-paste, non-interference slot; own try/except so a full disk can't cost
the shadow record), gitignored, capped at 1000MB with oldest-first pruning
(`[retain]` in config.toml). Added `mine_merge_rule.py`: replays the
500ms-threshold chunking three ways for a short tail (bare, given the
previous chunk's text as a prompt, or re-merged with the previous chunk's
audio), scores each against a freshly-regenerated full-clip transcript via
word-level alignment, and flags join-punctuation and clipping artifacts.

This is temporary experiment data, not a feature - `config.toml`'s comment
says how to clear it out once the merge rule is decided. Collecting now
against real usage plus a handful of deliberately-shaped test dictations.

## 2026-08-26 — Standing convention: code changes go to a Fable agent

Code writing/modification in this repo now goes to a Fable agent (max
effort, always spelled out explicitly since the Agent tool has no effort
parameter), not written directly in the main chat - Fable outperformed on
both an independent SleepWatcher diagnosis and a critique of a Sonnet-
authored test plan (the merge-rule plan above, in fact - the first version
had a real flaw a second pass caught). Main chat still scopes the work,
reviews the result, and handles git. Every such dispatch runs in an
isolated git worktree and gets a ~60s Monitor heartbeat digesting real
progress, not just a liveness ping.

## 2026-08-26 — Job log now records why polish left text unchanged

Was asked whether a specific dictation's missing commas meant polish did a
bad job. Digging into `wisprclone.log` turned up a real ambiguity: a job
where polish ran and genuinely decided no edit was needed looks identical
in the log to one where polish silently failed (timeout, exception) and
fell back to raw - both just show `raw == text`, with the actual cause
buried in a separate ERROR/WARNING line that has to be timestamp-matched
back to the job by hand.

Mined the full log (687 jobs, 272 with polish gated in) to check a
hypothesis that long dictations make polish more conservative - the data
said the opposite (58% edit rate under 15s, up to 83% over 40s). But the
ten longest zero-edit dictations included two real hidden failures: a
183.8s dictation that hit an unhandled exception, and a 106.1s one that
timed out at the configured 30s ceiling. A third, 130.1s, had no error at
all - polish ran clean and just declined to punctuate a 78s continuous
speech segment.

Fix: `_polish()` now returns `(text, status)` instead of bare text, and
the job log line carries `polish_status=` (`ok`, `skipped`, `suspicious`,
`dropped_question`, `timeout`, `error`). No more cross-referencing
timestamps to tell a real no-op from a swallowed failure.

## 2026-08-25 — Stammer cleanup: exact-repeat rule shipped, diverging-restart rule pulled

Noticed polish was leaving real stammers untouched ("there were, there was
a mining thing" pasted exactly as said). Three fixes, tested live against
real dictations before landing:

- **Exact-repeat stammers now get cleaned by polish.** Added a rule +
  worked example to `POLISH_PROMPT` naming the pattern directly ("there
  were, there was" -> "there was"), since the existing abstract "false
  start" wording wasn't concrete enough for dolphin-mistral to act on
  reliably. Verified live across several real dictations, consistent
  every time.
- **The stutter-collapse regex (`_STUTTER`) no longer eats intentional
  emphasis.** It used to collapse any doubled word unconditionally ("very,
  very important" -> "very important"), silently, no log trail. Now it
  only collapses a bare repeat with no comma between the words - a comma
  is Whisper's own signal that the speaker paused before repeating on
  purpose. Verified live: "saw saw" still collapses, "very, very
  important" now survives.
- **New `emphasis_words.txt`** (gitignored, same pattern as
  `corrections.txt`, `.example` template committed): words on this list
  are never collapsed by `_STUTTER`, comma or not. Closes the residual gap
  the comma-heuristic can't cover - fast, no-pause emphasis ("very very
  important" said quickly). Seeded with very/really/no and verified live.

**Known bug, still in tuning, NOT committed:** a second prompt rule meant
to also catch a *diverging*-wording restart ("I want to grab, I want to
get the keys" -> "I want to get the keys", first attempt abandoned
incomplete) tested clean 3/3 offline on short text, but failed on a real,
longer live dictation - polish left it completely untouched with no log
trail. Re-running the exact real sentence 4 more times offline gave a
third, different, still-wrong result each time ("I was going, hoping..."
- drops just the word "I", never the full phrase). Same root cause as the
temperature-0 finding from the profanity work: GPU float non-determinism
means temp 0 isn't truly deterministic on near-tied logits, and this
sentence lands in that territory - the model's confidence is split three
ways (full removal, partial removal, no change) with no reliable winner.
Not harmful (worst case is a no-op or a partial edit, never corruption),
just not solid enough to ship. Reverted out of `POLISH_PROMPT` for now;
the exact-repeat rule above is unaffected and stays in.

## 2026-08-25 — Decided against a public release

Compared WisprClone directly against Handy (github.com/cjpais/Handy), the
most popular free Wispr Flow alternative: 30k+ GitHub stars, MIT, Rust/Tauri,
100+ contributors, signed installer, cross-platform, 69-model catalog, its
own test suite. The gaps between this repo and a real public release
(packaging, model auto-download, GPU auto-detection, cross-platform support,
tests) are exactly what Handy already has, built by a team over years, not
something a solo project closes by iterating further. Decided to stop
treating public release as a goal and keep this as a personal tool /
portfolio piece instead. README.md's Scope section, SETUP.md's intro, and
CLAUDE.md were all updated to reflect that framing.

This doesn't roll back anything already built. The comparison also surfaced
that a few pieces here (mouse-button push-to-talk via a Win32 hook, CUDA
transcription, the local-by-default polish pass with its correctness
guardrails, Clipboard History exclusion, UIA continuation stitching) don't
have an equivalent in Handy at all — worth remembering if either the
clipboard-exclusion or continuation-stitching idea is ever worth upstreaming
as a PR to Handy itself, separate from running a release of this repo.

## 2026-08-25 — UIA continuation stitching, polish swapped to dolphin-mistral (branch: streaming)

- Researched how Wispr Flow solves the "no period between short dictations"
  problem before building anything: it reads the focused field's live
  content via accessibility APIs instead of guessing from a timer. Built
  the same approach: `focused_text()` reads the focused control via UI
  Automation (TextPattern, ValuePattern fallback), and a follow-up
  dictation stitches a period onto the previous paste only when the field
  still ends with that paste's tail. Old timer/same-window heuristic
  survives as `continuation_gap_s` (4s), used only when a field exposes
  neither pattern. Terminals excluded - the full screen buffer would
  corrupt commands. Verified live across Claude desktop, Firefox
  (chrome + web content), Gmail compose, Notepad, and both PowerShell and
  WSL terminals via a throwaway probe script (not committed, gitignored).
- Separately: qwen2.5:7b-instruct was sanitizing profanity in dictated
  speech despite the polish prompt's "keep the speaker's own wording"
  rule ("dog shit" -> "rejected", "weird shit" -> "weird"), inconsistently
  in the same session. Diagnosed with temperature 0 vs 0.1 A/B testing -
  output still varied at temp 0 (GPU float non-determinism on near-tied
  logits), proving the model was confidently choosing to censor certain
  spots, not randomly guessing. Switched `[polish] model` to
  `dolphin-mistral` and added an explicit prompt rule ("preserve all
  profanity... never remove, replace, or soften"). Fixed most cases
  outright; a swear used as a stand-alone interjection ("fuck me") is
  still the harder case for the model to leave alone, though live use
  after the change has come through clean.

## 2026-08-24 — pill is draggable, position persists (branch: streaming)

- The parked maybe-later from the slimming decision, implemented as the
  full draggable option (what Wispr Flow itself shipped) rather than config
  offsets. Left-drag moves it, 5px threshold keeps result-state clicks
  working, position clamps on-screen and saves to the gitignored
  pill_pos.txt on release (state, not config - config.toml stays clean).
  Result-state widening now grows around the parked center instead of
  recentering on the screen. Delete pill_pos.txt to reset.

## 2026-08-24 — pill resized 57x28 -> 64x24 (branch: streaming)

- The pill's top edge was grazing the Claude textbox; the bottom edge is
  pinned 60px above the screen bottom, so shortening it lowers the top.
  Went shorter and longer per Uriah's ask, radius 14 -> 12 to stay a true
  capsule. Verified against a phone playing a news clip at the mic: looks
  good, textbox clear.

## 2026-08-24 — interior vignette and breathing tune (branch: streaming)

- The flat #1e1e28 interior was the last dead layer; added a cold-blue
  radial lift behind the bars that breathes on the same clock as the halo,
  so the pulse reads as coming from inside the pill. One layer covers both
  approved ideas: the base lift is the depth vignette, the glow-scaled part
  is the breathing.
- Two feel fixes from live testing: cycle slowed 1.2s -> 1.8s ("a really
  neat pace"), and the halo alpha floor raised 60 -> 120 because the dim
  end of the breath took the ring fully invisible - it now breathes between
  soft and bright without ever vanishing.

## 2026-08-24 — glow halo: reactive -> pulsating, icy white-blue (branch: streaming)

- Three same-evening refinements, each approved live before the next: halo
  brightened (its old floor got eaten by the 0.65 whole-pill alpha), then
  reworked from voice-tracking to a steady 1.2s breathing pulse (Uriah's
  call - bars stay reactive, the border just breathes), then recolored from
  saturated sky blue to icy white-blue (195, 238, 255). Alpha swings 60-255
  across the cycle and blur radius rides the same value, so it swells as it
  brightens.
- Side finding while trying to screenshot it for him: the pill is invisible
  to every GDI capture path (PrtScn, PIL ImageGrab, BitBlt even with
  CAPTUREBLT) - UpdateLayeredWindow content just isn't there. Pictures of
  the pill come from rendering Pill._render offline and compositing at
  _ALPHA, which is what the scratchpad render script did.

## 2026-08-24 — pill recording animation overhaul (branch: streaming)

- Four effects added in one pass at Uriah's "fuck it, add all four": a
  voice-breathing glow halo (the pill window gained a 10px transparent
  margin for it; per-pixel alpha made this possible), center-out symmetric
  waveform replacing the left-to-right scroll, a center-to-edge blue
  gradient on the bars, and loudness-reactive brightness (quiet dims, loud
  blooms). Worst-case frame measured 2.8ms against the 33ms tick budget.
- Level multiplier doubled (12 -> 24) after live testing at his normal
  2.5ft distance: the pill shows raw mic level (normalize_peak only applies
  at transcription), and the quiet mic kept the animation near the floor
  unless he leaned in. Verdict at normal distance after the bump: keeper.

## 2026-08-24 — pill and tray go light blue (branch: streaming)

- Swapped the green (#34c759) for light blue (#5ac8fa) on the recording
  bars, the result checkmark (same constant), and the tray icon's waveform.
  Pure preference: Uriah wanted to see it, then kept it.

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
