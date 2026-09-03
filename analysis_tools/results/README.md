# Analysis results

Running record of every offline test run against the retained dictation
audio and the logs: what was compared, the numbers, and the call. Newest
first, same shape as LOG.md. LOG.md keeps the decision and why; this file
keeps the tables. The data behind each entry sits in a dated folder next to
this file (`2026-09-01/` and so on) and is gitignored, since it holds full
dictation transcripts. The scripts that produced a set sit in the same
folder, also untracked. Only this README is committed.

Method common to all of these: no human-transcribed ground truth exists, so
"accuracy" is measured as disagreement between candidates plus a read of
what the disagreements are. Whisper baselines always run through the live
app path (`Transcriber.pipe` with the app's decode options and prompt;
hotwords before 2026-09-02).

## 2026-09-03 — Polish replay on the About Me interview's dictations (second confirmation)

Question: does the 9/1 polish-off call (69% no-op, 21% punctuation-only over
548 real dictations) hold up against a different corpus - the 31 dictations
from an interview session, longer and more coherent than typical toggle-mode
use? Same live model (qwen3.5:9b), same `_polish` prompt/options/guards,
replayed directly rather than read from logs (polish has been off since
9/1, so no new log pairs exist to mine).

| | |
|---|---:|
| dictations in the window | 31 |
| under min_audio_s=8s, never reach polish live | 4 |
| sent to the model | 27 |
| no-op | 24 (89%) |
| edited and accepted | 3 (11%) |
| rejected by a guard | 0 |
| wall clock / mean / median per call | 105.8s / 3.92s / 3.72s |

Higher no-op rate than 9/1's 69%, consistent with these being longer,
well-formed answers rather than the rambling short commands polish targets.
Latency ran well past the "~1s per dictation" estimate in config.toml's
comment - that figure was averaged over short dictations, and the model
generates its full output regardless of whether anything changes, so cost
scales with length; this corpus averaged 112 words per answer.

Of the 3 accepted edits: one fixed a stray mid-sentence period into a
comma, one dropped a filler "I guess" - both correct. The third resolved a
live self-correction ("most of my childhood, no, all of my childhood") by
deleting the correction and keeping the wrong value ("most"), inverting
the intended meaning. None of `_polish`'s four guards (ratio, dropped
question, dropped profanity, dropped sentence) catch a silent value-swap
like that - it's the same failure category that got qwen2.5:7b and
dolphin-mistral rejected earlier, surfacing again in the model that
replaced them.

Call: stay off. A second, independent sample lands on the same conclusion
as 9/1 - not enough benefit to justify the latency, and a real chance of a
silent, guard-invisible meaning change. Data:
`2026-09-03/interview_polish_replay.py`, `2026-09-03/output.txt`.

## 2026-09-02 — Chunk-join rule replay

Question: does the join rule proposed in the calibration entry below fix
the artifact joins without lowercasing real sentence starts or names? The
rule as built (`join_segments()` in transcribe.py): at a join where the
previous chunk didn't end with . ! ? (a trailing "..." is treated as a
cut and dropped), lowercase the next chunk's first word unless it is I or
I'm-style, a capitalized word from the Whisper prompt or a corrections.txt
target, a word with a capital past its first letter, or a word capitalized
mid-sentence elsewhere in the same dictation. Three checks, all against the
code that ships: 17 unit cases; every join from the 8s-chunk calibration
replay, with the whole-clip baseline transcript's case of the word after
the join as truth (clean only for clips under 30s, since the baseline of
longer clips carries production chunk joins of its own); and production
chunking (chunk_length 30) over every retained clip of 25s or more.

| calibration joins, clips under 30s (clean truth) | |
|---|---:|
| rule lowercased, baseline lowercase (fixed) | 156 |
| rule lowercased, baseline capital | 8 |
| of those, real regressions | 2 ("Spanish", "Thursday") |
| chunk capital kept, baseline lowercase (period-at-cut case, out of scope) | 63 |
| untouched and matching | 156 |
| not located in the baseline | 98 |

Of the 8 counted as regressions, 3 are the baseline showing the same
quirk under the old unpunctuated prompt ("playing Then what I would do",
"speeches How would that change"), 1 is a lookup drift, 1 ("Windows") is
protected in real use because the test feeds only 40-character tails and
the name appears mid-sentence earlier in the same chunk, and 2 are real: a
language and a weekday the prompt doesn't know. Joins where the previous
chunk ended in "...": baseline lowercase after 152, capital after 31, so
treating the ellipsis as a cut is right about five times in six.

| production chunking, clips of 25s or more | |
|---|---:|
| clips with 2+ chunks | 16 of 981 |
| joins | 18 |
| joins changed by the rule | 11 |
| changed joins that read wrong | 0 |

The 11 include "what kind of data... | Can be useful" to "data can be
useful", "It's going to be... | The last time" to "be the last time",
"read them, | And see" to "them, and see", and "a lot of... |
Micromanaging." to "of micromanaging."; "going to... | Jump Ship" became
"jump Ship", Whisper's own capital on Ship left alone. The two live clips
that started this ("feeling | Like", "noticing is... | That") both come
out as one sentence. Call: ship it. Exposure is about one join per 55
dictations at current lengths, fixed in 11 of 18, and wrong in roughly 1
of 80 words it lowercases. Data: `2026-09-02/join_rule_test.py`,
`join_rule_test.txt`.

## 2026-09-02 — Mid-sentence capitals are chunk-join artifacts

Question: where do capitals like "It stopped feeling Like it was built"
come from, and can a rule fix them without breaking real sentence starts?
Two measurements. First, a replay of turbo over every clip of 6s or more
with `chunk_length=8`, so the batched pipeline splits at natural pauses
and produces 586 chunk joins; for each join, whether the next chunk starts
with a capital, whether the previous chunk ended with sentence
punctuation, and whether the full-clip baseline transcript (same model,
whole context) had a sentence boundary at that spot. Second, four scripted
toggle dictations with a marked 1s or 4s pause, mid-sentence or between
sentences, plus the live 43s dictation that showed the quirk.

| | |
|---|---:|
| joins in the 8s-chunk replay | 586 (539 located in the baseline text) |
| next chunk starts with a capital | 496 of 586 |
| previous chunk ends without . ! ? | 79 |
| baseline has a sentence boundary at the join | 134 of 539 |
| artifact case: previous chunk unpunctuated, next chunk capitalized | 66 |
| of those, baseline says sentence boundary | 0 of 66 |

So a capital at the start of a chunk whose predecessor didn't end a
sentence was a continuation in every one of 66 cases. Whisper never
chooses to capitalize there; the chunk restart does. The gap between chunk
end and next chunk start is not a usable pause measure (513 of 539 joins
show 0 to 0.3s, because the VAD pads both sides), so a pause-length
threshold isn't needed and wouldn't work; the previous chunk's last
character is the whole signal. Live confirmation: the 43s dictation split
at 31.9s/34.2s exactly at "feeling | Like", and the first scripted clip
(31s, 1s mid-sentence pause) split at "noticing is... | That the pill",
Whisper ending the cut chunk with its trailing-off ellipsis. The other
three scripted clips stayed under the 30s chunk limit, so they never
split, and Whisper handled the same pauses correctly inside one window
(the 4s mid-sentence pause came out as plain "is that"). History.log has
8 "... Capital" cases in 2,186 pastes and 16 function-word capitals after
an unpunctuated word, so the quirk is rare overall and confined to
dictations over ~30s. Fix (proposed): at each chunk join, if the previous
chunk ends without sentence punctuation or with "...", lowercase the next
chunk's first word unless it's "I"/"I'm"-style or a proper noun from the
prompt or corrections; drop the cut ellipsis. Script: join_calibration.py.

## 2026-09-02 — Whisper prompt style: clean sentences instead of a bare hotword list

Same turbo model, same 981 clips, same batched path; the only change is
the prompt Whisper reads before decoding. The baseline is the hotword list
as config.toml has it ("WisprClone, Wispr Flow, Claude, ...", no sentence,
no period). Three variants fold the same hotwords into fully punctuated
sentences: the three-sentence example from the chat exactly as Uriah
pasted it back (no ClaudeMD, no digits), a three-sentence version with
ClaudeMD and a clock time in it, and a one-sentence version. Whisper imitates the style of its prompt, so the bet
was that a punctuated prompt yields punctuated output. It does, and it
does not cost accuracy, vocabulary, or latency. Call: adopted Uriah's three
sentences, with "and the CLAUDE.md file" folded in for the ClaudeMD
hotword (that addition untested; same shape as the tested prompts), as the fix for the polish-off trial's
dominant quirk, the missing final period.

| | baseline (hotword list) | Uriah's 3 sentences | long clean prompt | short clean prompt |
|---|---:|---:|---:|---:|
| clips ending without . ! or ? | 291 | 17 | 15 | 19 |
| of those, 6+ words (real sentences) | 198 | 7 | 7 | 7 |
| clips with um/uh | 9 | 4 | 4 | 6 |
| clips with "you know" | 58 | 53 | 55 | 56 |
| "WisprClone" spelled right | 9 | 15 | 14 | 13 |
| Claude/Fable/Ollama hits | 58 | 61 | 58 | 58 |
| empty outputs | 0 | 0 | 0 | 0 |
| word disagreement vs baseline | | 0.9% | 1.1% | 0.7% |
| clips word-identical to baseline | | 880 | 873 | 902 |
| wall median | 0.235s | 0.232s | 0.236s | 0.233s |

What the word-level diffs were, both variants: "alright" to "all right"
(Whisper's spelling under a formal prompt; the most common diff), "grock"
to "grok" (a fix), "standby" to "stand by", a few compounds fused
("biocomputing", "chasebank", "push2talk" in the long variant only). The
long prompt also splits some run-ons into two sentences ("Yeah, fuck it.
Give me one more."), the short one mostly just appends the period and a
comma or two; the short one is the lighter touch and the closer match to
the baseline. Hotword recall went up, not down, with the sentence framing.
The "whisper(clone) misspellings" row in prompt_style_report.txt is a
counting bug (negative values) and should be ignored. Scripts:
prompt_style_test.py and prompt_style_test2.py; outputs
whisper_results_prompt_<variant>.json.

## 2026-09-02 — Granite Speech 3.3 2B vs large-v3-turbo

981 clips (turbo baseline; Granite ran 990). Plain transformers stack
(AutoModelForSpeechSeq2Seq, bfloat16, greedy, max_new_tokens 1200) in a
scratch venv, Sonnet agent, main chat supervising; CUDA held through every
install this time. Call: stay on turbo. Granite's output is normalized
text: no capital letter in any of 990 clips, sentence punctuation in 21%,
so it would need a separate punctuation and casing model before anything
could be pasted. Beyond that it repeats the Canary pattern.

| | turbo | Granite 2B |
|---|---:|---:|
| word disagreement (of 19,583 turbo words) | | 5.0% |
| clips identical after normalizing | | 66% |
| wall median | 0.24s | 1.33s |
| wall p95 | 0.43s | 4.92s |
| fit, per audio second | 0.011s | 0.18s |
| whole pass | 257s | 1770s |
| slower on | | 980 of 981 clips |
| clips with any capital letter | 975 of 981 | 0 of 990 |
| clips with . ! or ? | 713 | 211 |
| clips returned empty | 0 | 1 (the 76s clip, nothing at all) |
| turbo has a digit, Granite none | | 26 clips |
| clips with um/uh | 9 | 13 |
| wrote WisprClone for the hotword | 9 of 13 | 0 of 13 |
| GPU memory | | ~5.9 GB |

Disagreement by length: 4.1% under 10s, 3.6% at 10 to 30s, 15.5% over 30s
(the empty 76s clip). Numbers as words throughout ("ninety percent",
"seven eight second", and "5090" as "fiftie ninety"); "alright" to "all
right" the single most common diff; profanity counts within one of turbo's.
Setup notes in granite_install_notes.md (torchaudio's loader wants
torchcodec; soundfile used instead). Load 8s, ~5 GB on disk.

## 2026-09-02 — Canary-Qwen 2.5B vs large-v3-turbo

981 clips (the turbo baseline, extended that morning from 886; Canary ran
986, the 5 newest had no baseline). Canary-Qwen via NeMo 3.1 trunk on
torch 2.14 cu126 in a scratch venv (the NeMo install silently replaced the
CUDA torch with a CPU wheel; caught and reinstalled before the pass). Run by
a Sonnet agent with the main chat supervising. Call: stay on turbo. Canary
is the most accurate open English model on the leaderboard, but on this
voice it agrees with turbo as closely as the others did, spells numbers as
words, keeps fillers, has no hotwords, dropped half of the longest
dictation, and costs five to nine times the latency.

| | turbo | Canary-Qwen |
|---|---:|---:|
| word disagreement (of 19,583 turbo words) | | 3.5% |
| clips identical after normalizing | | 713 (73%) |
| wall median | 0.24s | 1.20s |
| wall p95 | 0.43s | 4.57s |
| clips under 10s, median | 0.22s | 0.89s |
| 10 to 30s, median | 0.33s | 2.91s |
| over 30s, median | 0.64s | 6.96s |
| fit, per audio second | 0.011s | 0.174s |
| whole pass | 257s | 1596s |
| slower on | | 981 of 981 clips |
| clips returned empty | 0 | 0 |
| clips with under 70% of turbo's words | | 1 (the 76s clip: 235 words to 131) |
| turbo has a digit, Canary none | | 22 clips |
| clips with um/uh in the raw output | 9 | 27 |
| wrote WisprClone for the hotword | 9 of 14 | 0 of 14 |
| GPU memory during generation | | ~10 GB |

Disagreement by length: 3.3% under 10s, 2.6% at 10 to 30s, 9.1% over 30s,
the last driven by the 76s clip losing its second half and a 69s clip
losing a sentence; a decoder that generates token by token has a length
where it stops. What the rest of the disagreements were: compound
splitting ("chat gpt", "wi fi", "screen shot", "stand by"), "alright" to
"all right" throughout, numbers as words ("twenty twenty six", "one
hundred percent", "seven eight second video"), more fillers kept. Real
hearing differences went both ways and were rare: Canary got "grok" and
"chat gpt"; turbo got "thumbs up" (Canary "dumza"), "Fable" (Canary
"fawa"), "Asmongold", "C++" (Canary "C"). Profanity counts identical.
Casing and punctuation are good, and it never produced an empty output.
Setup notes: `pip install nemo_toolkit[asr]` from trunk, then reinstall
torch from the cu126 index with `--no-deps`, plus `peft` and `accelerate`
which the model class imports without declaring. Load 29s, 4.8 GB on disk.

Reproducibility check, same day: 30 clips rerun (25 random plus 5 of the
digit cases) came back byte-identical to the saved pass, 30 of 30, so the
decoding is deterministic and the numbers-as-words habit is systematic,
not noise. The two long clips rerun with max_new_tokens raised from 400 to
1200 produced the same shortened output (131 and 138 words), so the cut is
the model skipping a middle stretch of the audio, not the token cap; the
output still ends on the clip's real last sentence. Median wall on the
rerun 1.37s with nothing else on the GPU. Script: canary_repeat.py.

## 2026-09-01 — large-v3 vs large-v3-turbo on the batched path

886 retained clips, both models through `Transcriber.pipe`. Call: stay on
turbo. The disagreements are mostly style, v3 is slower on every clip, it
ignores the WisprClone hotword, and its no-speech scores would make the
app's segment filter drop 2% of dictations outright.

| | turbo | large-v3 |
|---|---:|---:|
| word disagreement (of 17,653 turbo words) | | 4.9% |
| clips identical after normalizing | | 720 (81%) |
| wall median | 0.23s | 0.49s |
| wall p95 | 0.43s | 1.47s |
| clips under 10s, median | 0.22s | 0.41s |
| 10 to 30s, median | 0.34s | 0.98s |
| over 30s, median | 0.64s | 1.90s |
| whole pass | 232s | 549s |
| slower on | | 886 of 886 clips |
| clips returned empty | 0 | 18 |
| wrote "WisprClone" for the hotword | 9 of 9 | 0 of 9 |

Disagreement by clip length: 3.7% under 10s, 5.5% at 10 to 30s, 7.2% over
30s. What the disagreements were: v3 formalizes ("gonna" to "going to",
"wanna" to "want to"), fuses compounds ("gitignore", "venv", "onto",
"servicenow"), and keeps fillers turbo drops ("you know", "uh", "I mean").
Real hearing differences were rare and went both ways ("grock" to "grok" in
v3's favor, "fuck Jordan" to "photjourn" against it). The 18 empty clips
were transcribed correctly by v3 but scored no_speech_prob 0.60 to 0.86 on
plain speech; the app drops segments over 0.6, and turbo never crosses that
line on real speech. Any future Whisper model swap has to re-check that
threshold first.

## 2026-09-01 — Polish "no change" sentinel

qwen3.5:9b over the same 548 polish inputs as the bake-off below, three
prompt variants, compared against its own outputs under the current
prompt. Call: dropped. The model retypes unchanged text by habit, and every
wording that made it stop also made it skip real edits.

| variant | what happened |
|---|---|
| current prompt + "if no changes, output exactly NOCHANGE" | 388 of 548 outputs identical to input; sentinel used on 4. When used: 0.30s vs 0.87s. Missed edits: 0. |
| sentinel instruction first, with an example (40 no-op + 20 edited inputs) | fired on 16 of 40 no-ops, and wrongly on 7 of 20 edited inputs |
| separate YES/NO "does this need cleanup" question, 0.22s, before the polish | NO on 30 of 40 no-ops, and on 8 of 20 edited inputs; the skipped edits were mostly the missing final period |

Net for the classifier variant: about half of polishes would save ~0.65s,
a third would pay 0.22s more, and one in eight would lose its punctuation
fix.

## 2026-09-01 — Polish model bake-off

548 real polish inputs (317 raw lines from wisprclone.log's polish diffs,
231 clips of 8s or more from the Whisper pass through `clean_text()`,
duplicates removed) through the exact `_polish()` request (temperature 0,
num_ctx 8192, num_predict from max_ratio, keep_alive 24h, `think: false`
added for the three new models). Guards are the real ones from
transcribe.py. Thirty outputs were also read blind. Call: switch to
qwen3.5:9b, which happened the same day; then, after reading what its
edits actually were (last table), polish was switched off entirely as a
trial.

Guards, of 548:

| | qwen2.5:7b-instruct | qwen3.5:4b | qwen3.5:9b | gemma4:e4b |
|---|---:|---:|---:|---:|
| errors / timeouts | 0 | 0 | 0 | 0 |
| suspicious length | 0 | 0 | 0 | 1 |
| dropped question | 10 | 5 | 1 | 4 |
| dropped profanity | 2 | 0 | 0 | 1 |
| dropped sentence | 3 | 0 | 0 | 1 |
| rejected, any guard | 14 (3%) | 5 (1%) | 1 (0%) | 6 (1%) |

Over-editing, accepted outputs only:

| | qwen2.5:7b-instruct | qwen3.5:4b | qwen3.5:9b | gemma4:e4b |
|---|---:|---:|---:|---:|
| accepted | 534 | 543 | 547 | 542 |
| no-op (identical to input) | 127 (24%) | 252 (46%) | 377 (69%) | 298 (55%) |
| word edit distance, mean | 0.052 | 0.014 | 0.005 | 0.011 |
| edits changing over 10% of words | 84 | 20 | 6 | 10 |
| deleted a content word | 122 (23%) | 33 (6%) | 10 (2%) | 33 (6%) |
| added a content word | 52 (10%) | 25 (5%) | 5 (1%) | 5 (1%) |
| fewer sentences than input | 19 | 10 | 7 | 13 |
| changed a number | 5 | 12 | 0 | 7 |
| lost a proper noun | 5 | 1 | 1 | 1 |
| added an em dash | 4 | 0 | 0 | 0 |
| added a semicolon | 8 | 17 | 6 | 6 |
| residual fillers removed | 22 of 31 | 7 of 32 | 10 of 39 | 30 of 34 |
| every swear kept (44 swearing inputs) | 42 (95%) | 44 | 44 | 43 (98%) |

Latency, each model alone on the GPU next to Whisper, same 30 inputs:

| | qwen2.5:7b-instruct | qwen3.5:4b | qwen3.5:9b | gemma4:e4b |
|---|---:|---:|---:|---:|
| cold load | 2.6s | 3.6s | 4.1s | 5.7s |
| median polish | 0.76s | 0.68s | 1.05s | 0.69s |
| p95 polish | 1.51s | 1.21s | 2.00s | 1.32s |
| generation tok/s | 55 | 73 | 45 | 70 |
| VRAM (/api/ps) | 4.8 GB | 3.1 GB | 5.2 GB | 3.1 GB |
| disk | 4.7 GB | 3.4 GB | 6.6 GB | 9.6 GB |

Full-pass fit of wall time against audio length: 7b 0.6 / 1.6 / 3.2s at 10
/ 30 / 60s of audio; 4b 0.5 / 1.3 / 2.5; 9b 0.8 / 2.1 / 4.1; gemma 0.6 /
1.4 / 2.7.

Blind read: the 7b was the model that rewrites ("Wait, no, fuck that"
became "What I'd like you to do is fuck that"; self-corrections cut;
sentences shortened into tidier ones that weren't said). The 9b returned
21 of 30 unchanged and never changed a meaning. The 4b broke an ellipsis
into a hard stop, spelled "8 seconds" as "eight seconds", and added
semicolons. Gemma punctuated run-ons best and removed fillers best, but
reformatted numbers ("830" to "8:30").

What the 9b's accepted edits actually were, 548 inputs:

| change | share |
|---|---:|
| nothing | 69% |
| punctuation or capitalization only | 14% |
| a final period only | 7% |
| words removed (mostly stammer restarts) | 6% |
| filler words removed only | 2% |
| a word changed or added | 2% |

Three models resident at once next to Whisper oversubscribed the 16 GB
card (9b fell to 19 tok/s, back to 44 once one was unloaded); the 9b's
first 77 requests were excluded from its full-pass latency for that reason.

## 2026-09-01 — Parakeet TDT 0.6B v2 vs large-v3-turbo

886 retained clips (116.6 minutes; 661 under 10s, 207 at 10 to 30s, 18
over 30s). Parakeet via onnx-asr 0.12.0, fp32 ONNX, onnxruntime-gpu 1.29.0
CUDA 12 build (PyPI's 1.29.0 is a CUDA 13 build and silently fell back to
CPU), in a scratch venv. Call: stay on Whisper. Parakeet is about twice as
fast, but the saving is about 0.12s on a typical clip, and its habits run
against the app.

| | Whisper (live path) | Parakeet |
|---|---:|---:|
| median | 0.23s | 0.11s |
| p95 | 0.43s | 0.21s |
| under 10s, median | 0.22s | 0.10s |
| 10 to 30s, median | 0.34s | 0.15s |
| over 30s, median | 0.64s | 0.24s |
| whole pass | 232s | 110s |
| faster on | | 885 of 886 clips |
| model load | 3.5s | 2.5s |
| GPU memory on load | | ~3.5 GB |

Agreement: corpus word disagreement 3.8%, per-clip median 0, p90 16%; 68%
of clips word-identical. Rerunning Whisper against 80 live raw lines gave
85% identical (noise floor, partly the batched switch). Disagreement
categories: fillers and stutters (Parakeet keeps every "uh", "um" and
phrase repeat; `clean_text()` would change 251 Parakeet outputs vs 121
Whisper ones, and phrase repeats get past `_STUTTER`); numbers as words on
48 clips ("three hundred and seventeen pounds", "Nvidia Fifty Ninety"), 26
of them under the 8s polish gate, while Whisper's digits were right every
time; no hotwords (WisprClone came out "Whisperclone" in all 15 clips,
Claude "Clod" in 7 of 23); punctuation good (sentence-final punctuation on
838 clips vs 622) but never an ellipsis; profanity identical (neither
sanitizes); both 30s+ hallucination clips clean on both models; zero
runaway repeats and zero empty outputs from either. Packaging: onnxruntime
gpu can't share a venv with the CPU onnxruntime faster-whisper pulls in
for Silero VAD, so Parakeet would be a full replacement, not a fallback.
