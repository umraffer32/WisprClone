# WisprClone

A local push-to-talk dictation tool for Windows, built as a lighter, private alternative to Wispr Flow. Hold a button, speak, release, and the transcription pastes into whatever's focused. Everything runs on-device: no account, no cloud API, no audio ever leaves the machine.

I built it because Wispr Flow does the job well but sends every word I say to a server I don't control. This does the same job locally, with a model I chose and can swap out, and nothing leaves my machine.

## What it does

Press and hold the mouse's back button (or a configured key) to record, release to transcribe and paste. A quick double-tap of Right Ctrl switches into toggle mode instead, for longer dictation where holding a button down isn't practical.

While recording, a small translucent pill appears at the bottom of the screen showing a live level meter. If a paste probably missed its target, no visible text caret and no editable control has focus, the pill instead offers a click-to-repaste checkmark with a countdown, plus an X to dismiss it early. Both have hover and click feedback built in.

The tray icon carries a running word count, both for today and all-time, seeded from `history.log` on startup so it survives a restart instead of resetting to zero every launch. From there you can also re-copy the last transcription, jump straight to the history file or config, reconnect the mic, or quit.

## Architecture

The app is a single process, multi-threaded, no server, no IPC:

- A **pynput listener thread** watches raw Win32 input events for the configured push-to-talk button or the toggle key.
- A **PortAudio callback thread** owns the microphone stream and the audio buffer outright; every other thread only flips a boolean to tell it whether to be recording.
- A **transcriber worker thread** pulls finished recordings off a queue, runs them through faster-whisper, cleans up the text, and pastes it.
- A **ducker thread** (optional) lowers other apps' volume via pycaw while recording, and restores it after.
- The **tkinter main thread** drives a 33ms UI tick that reads shared state and redraws the pill, plus the Win32 hook filters that need to run inline to suppress input events.

Nothing shares mutable state without a clear owner: the audio buffer belongs to the callback thread alone, the recording state machine is protected by a single lock, and a small `Status` object is the only thing multiple threads touch concurrently for UI state.

## Technical highlights

**Suppressing input at the OS level.** The push-to-talk button is read through a low-level Win32 hook (pynput's `win32_event_filter`), not a normal callback, so the press can be both acted on and swallowed before it reaches any other app. That matters specifically for the mouse: the X1/X2 buttons natively mean browser back/forward, and without suppression every dictation press would also navigate the page underneath it.

**A clipboard swap that doesn't clobber what was there.** Pasting means writing to the clipboard, sending Ctrl+V, then restoring the previous contents. The restore logic is format-aware: it enumerates every clipboard format present, only restores what it explicitly knows how to save safely (plain text and image formats), and refuses to restore at all if it finds something outside that set alongside text, like rich text copied from Word, rather than risk silently downgrading it to plain text.

**Guessing whether a paste landed.** There's no OS signal for "the paste worked." Instead, after pasting, the app checks for a visible system text caret and, for apps that draw their own (Electron, Chromium), asks UI Automation whether the focused control is actually an editable field. Only when both come back negative does the repaste offer appear.

**GPU with a real fallback, not just a try/except.** The model loads on CUDA if available and falls back to CPU on failure. A failure counter latches to CPU after repeated GPU failures within a session, so a flaky driver doesn't retry and fail on every single transcription.

**An always-hot mic.** The audio stream opens once at launch and stays open, because opening a device takes 50-300ms, long enough to clip the first word of an utterance and stall the input hook while it waits. A short pre-roll buffer captures the moment just before the button is pressed so speech doesn't get cut off. It suspends after a period of inactivity, mainly so Windows' mic-in-use indicator doesn't stay lit all day; the first syllable after re-opening can clip, a documented tradeoff rather than an oversight.

**Word counts that survive a restart.** Rather than keeping a running total only in memory, the tray's word counters are seeded on startup by re-parsing `history.log`, so quitting and relaunching the app doesn't lose the count.

## Things I tried and reversed

Early versions ran an LLM polish pass over toggle-mode dictation through a local Ollama model, and separately tried bridging punctuation across consecutive dictations in the same window. The polish pass got removed entirely, code and Ollama dependency both, once it turned out to add latency for a wording improvement I didn't actually want most of the time. The punctuation bridging stayed in the code but got turned off by default: it glued together enough unrelated messages in normal chat-style use that the false-positive rate wasn't worth what it fixed. Both are visible in git history if you want to see the actual back-and-forth.

## Tech stack

`faster-whisper` / `ctranslate2` for transcription, `sounddevice` for audio I/O, `pynput` for the Win32 input hook, `pystray` + `Pillow` for the tray icon, `pywin32` for clipboard and window APIs, `pycaw` for per-session volume ducking, and `tkinter` for the overlay pill. No web framework, no database, no network calls.

## How I have mine set up

- Model: `distil-large-v3` on CUDA, chosen over `large-v3-turbo` for its stronger resistance to hallucinating text on short or silent clips, the exact failure mode a push-to-talk tool with short bursts runs into.
- Push-to-talk on the mouse's X2 (back) button, with a Right Ctrl double-tap for toggle mode.
- Audio ducking on: other apps drop to 5% volume while I'm recording.
- Mic releases after 10 seconds idle so Windows' mic-in-use indicator doesn't stay lit all day.
- Repaste offer stays up for 10 seconds before it disappears on its own.

## Scope

This is a personal tool built around my own hardware, my own corrections list, and my own habits, not a general-purpose release. It's Windows-only by design (the input hook, clipboard handling, and tray icon are all Win32-specific), and the model is English-only. It's here as a portfolio piece rather than something meant to be cloned and run as-is.
