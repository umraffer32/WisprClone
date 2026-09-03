# WisprClone

A local push-to-talk dictation tool for Windows, built as a lighter, private alternative to Wispr Flow. Hold a button, speak, release, and the transcription pastes into whatever's focused. Everything runs on-device: no account, no cloud API, no audio ever leaves the machine.

I built it after hitting Wispr Flow's free-tier limit and deciding a monthly subscription wasn't worth it for what's fundamentally speech-to-text with autopaste. This does the same job locally, with a model I chose and can swap out, and as a side effect nothing I say leaves the machine.

## What it does

Press and hold the mouse's back button (or a configured key) to record, release to transcribe and paste. A quick tap of Right Ctrl switches into toggle mode instead, for longer dictation where holding a button down isn't practical.

While recording, a small translucent pill appears at the bottom of the screen showing a live level meter. If a paste probably missed its target (the focused field, read back afterward, is missing the text that was just pasted), the pill instead offers a click-to-repaste checkmark with a countdown, plus an X to dismiss it early. Both have hover and click feedback built in.

The tray icon carries a running word count, both for today and all-time, seeded from `history.log` on startup so it survives a restart instead of resetting to zero every launch. From there you can also re-copy the last transcription, jump straight to the history file or config, reconnect the mic, or quit.

## Architecture

The app is a single process, multi-threaded, no server, no IPC:

- A **pynput listener thread** watches raw Win32 input events for the configured push-to-talk button or the toggle key.
- A **PortAudio callback thread** owns the microphone stream and the audio buffer outright; every other thread only flips a boolean to tell it whether to be recording.
- A **transcriber worker thread** pulls finished recordings off a queue, runs them through faster-whisper, cleans up the text, and pastes it.
- A **ducker thread** (optional) lowers other apps' volume via pycaw while recording, and restores it after. A short clip also marks the start and stop through winsound, played straight to the default device so it isn't ducked.
- An **anchor thread** polls UI Automation for the Claude Code compose box while that app is in front and publishes its center x, so the pill sits under the chat column even as the app's side panels open and close. Any other app in front, or a failed read, and the pill keeps its dragged/default center.
- The **tkinter main thread** drives a 33ms UI tick that reads shared state and redraws the pill, plus the Win32 hook filters that need to run inline to suppress input events.

Nothing shares mutable state without a clear owner: the audio buffer belongs to the callback thread alone, the recording state machine is protected by a single lock, and a small `Status` object is the only thing multiple threads touch concurrently for UI state.

## Technical highlights

**Suppressing input at the OS level.** The push-to-talk button is read through a low-level Win32 hook (pynput's `win32_event_filter`), not a normal callback, so the press can be both acted on and swallowed before it reaches any other app. That matters specifically for the mouse: the X1/X2 buttons natively mean browser back/forward, and without suppression every dictation press would also navigate the page underneath it.

**A clipboard swap that doesn't clobber what was there.** Pasting means writing to the clipboard, sending Ctrl+V, then restoring the previous contents. The restore logic is format-aware: it enumerates every clipboard format present, only restores what it explicitly knows how to save safely (plain text and image formats), and refuses to restore at all if it finds something outside that set alongside text, like rich text copied from Word, rather than risk silently downgrading it to plain text.

**Checking whether a paste landed.** There's no OS signal for "the paste worked." The app used to guess: check for a visible system text caret and, for apps that draw their own (Electron, Chromium), ask UI Automation whether the focused control is an editable field. That guess broke on editors that draw their own caret and also report as a UIA Document rather than an Edit control, VS Code's Monaco editor and eBay's web compose box among them, flashing a repaste offer over pastes that had landed fine. Now the app reads the focused field's text back through UI Automation, the same ground-truth read the continuation bridging below relies on, and offers a repaste only when the pasted text genuinely failed to appear in the field. The caret-and-editable-control guess survives as the fallback for fields that expose no readable text. A right-click during a dictation used to lose the whole thing silently: the context menu ate the injected Ctrl+V, and the caret behind the menu kept blinking the whole time, so the old check read "landed fine" when nothing had. If a menu is open at the moment a dictation would paste, the app skips the keystroke entirely, since a stray "v" can trigger a menu item, and offers the repaste pill instead of guessing whether or when to send it.

**Bridging separate dictations without a fixed timer.** Whether to glue a new dictation onto the previous one (a period, then a space) instead of treating it as a fresh thought needs ground truth, not a guess. After pasting, the app reads the focused field's actual text back through UI Automation; if it still ends with the previous paste's tail and that paste had no closing punctuation, it stitches on. A field that clears, gets sent, or gets hand-edited simply fails that check and starts fresh. Only when a field exposes no readable text at all (some apps don't implement the UIA text patterns) does it fall back to a blind same-window-within-a-few-seconds heuristic, and a terminal never stitches, since a stray period there would corrupt a command.

**GPU with a real fallback, not just a try/except.** The model loads on CUDA if available and falls back to CPU on failure. A failure counter latches to CPU after repeated GPU failures within a session, so a flaky driver doesn't retry and fail on every single transcription.

**A mic that skips the open latency between uses.** The audio stream doesn't reopen on every press, because opening a device takes 50-300ms, long enough to clip the first word of an utterance and stall the input hook while it waits. A short pre-roll buffer also captures the moment just before the button is pressed so speech doesn't get cut off. It's not permanently open, though: mine releases the mic after 10 seconds of no dictation so Windows' mic-in-use indicator doesn't stay lit for long. In theory that risks clipping the first syllable on reopen; in practice, that's the long-standing value, and it's never once produced a clip I actually noticed. Raised it to 5 minutes for a day to keep the mic hot through active work and see if it helped, but the indicator staying lit that long was the real, felt cost, so it went back to clearing quickly at 10 seconds instead. That idle timeout can be set to 0 for a genuinely always-hot mic if you'd rather not take the theoretical risk at all. If the mic isn't there yet at launch, say the app starts at login before a USB mic has enumerated, that open failure is caught instead of taking the whole app down before the tray icon even exists. The stale-stream watchdog picks it up and retries a few seconds later.

**Word counts that survive a restart.** Rather than keeping a running total only in memory, the tray's word counters are seeded on startup by re-parsing `history.log`, so quitting and relaunching the app doesn't lose the count.

## Things I tried and reversed

Early versions ran an LLM polish pass over toggle-mode dictation through a local Ollama model, and separately bridged punctuation across consecutive dictations purely on a timer. The first cut of the polish pass got removed entirely, code and Ollama dependency both, once it turned out to add latency for a wording improvement I didn't actually want most of the time. It came back (see [SETUP.md](SETUP.md) for the how and why) running through a different model with the latency problem actually fixed, then got switched off again just as deliberately: a review of 548 real polished dictations found it changed nothing in 69% of them and only punctuation in another 14%, so it wasn't earning the extra second it cost on longer dictations. It's off in `config.toml` as a trial, judged by whether raw Whisper's own occasional blemishes, a missing final period, a leftover stammer, actually bother me in day-to-day use; the model and guardrails stay in place either way, and flipping it back on is one config line. The timer-only bridging got turned off by default early on: it glued together enough unrelated messages in normal chat-style use that the false-positive rate wasn't worth what it fixed. It's back too, now driven by the UI Automation field-read described above instead of a blind timer, with the timer demoted to a fallback for the apps that don't expose readable field text. Both reversals are visible in git history if you want to see the actual back-and-forth.

## Tech stack

`faster-whisper` / `ctranslate2` for transcription, `sounddevice` for audio I/O, `pynput` for the Win32 input hook, `pystray` + `Pillow` for the tray icon, `pywin32` for clipboard and window APIs, `pycaw` for per-session volume ducking, `requests` for the local polish call, and `tkinter` for the overlay pill. No web framework, no database, no network calls that leave the machine — the only outbound request is the polish pass, to Ollama on 127.0.0.1.

## How I have mine set up

- Model: `large-v3-turbo` on CUDA — [SETUP.md](SETUP.md) has the reasoning behind that choice.
- Push-to-talk on the mouse's X2 (back) button, with a Right Ctrl tap for toggle mode.
- Audio ducking on: other apps drop to 1% volume while I'm recording.
- Start/stop cue on: a short clip marks when recording begins and ends. The recorded buffer mutes for the start clip's own duration so its sound can't bleed into the mic.
- Mic releases after 10 seconds idle so Windows' mic-in-use indicator clears quickly.
- Repaste offer stays up for 10 seconds before it disappears on its own.

## Setup

Everything above is what running one of these looks like day to day.
[SETUP.md](SETUP.md) is the actual how-to: clone, venv, model download,
corrections file, optional Ollama polish, and the two Windows-specific
pieces that took real trial and error (RUNASADMIN so dictation reaches
elevated windows, and a Task Scheduler task instead of a Startup shortcut).

## Scope

This is a personal tool, built around my own hardware, my own corrections list, and my own habits — not a public release, and not one I'm planning. It's Windows-only by design (the input hook, clipboard handling, and tray icon are all Win32-specific), and the model is English-only.

I looked at what a real public release would take and weighed it against [Handy](https://github.com/cjpais/Handy), the most popular free Wispr Flow alternative (30k+ stars, MIT, a hundred-plus contributors, signed installer, cross-platform, its own test suite). Everything that separates this repo from a real release, packaging, GPU auto-detection, multi-platform support, is exactly what Handy already has, built over years by a team. Chasing that would mean building a worse Handy instead of a sharper tool for myself. So this stays what it actually is: a portfolio piece documenting a specific set of engineering problems (OS-level input suppression, format-aware clipboard handling, UI Automation field reads for continuation-stitching, a local LLM polish pass with real correctness guardrails, recovering when Windows revokes trust in an unsigned dependency mid-run) and how I solved them. [SETUP.md](SETUP.md) documents exactly how mine is built and configured, for anyone curious about the approach, not as an onboarding guide for a general audience. [BUGS.md](BUGS.md) has the full incident history behind that list, not just the highlights.
