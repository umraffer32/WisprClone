# Setup

This is exactly what I did to get WisprClone running on my own machine, in the order I did it, including the two Windows-specific pieces that took real trial and error to figure out. This isn't a general public install guide, see README.md's [Scope](README.md#scope) section for why, but it's accurate: if you're curious enough to build your own copy anyway, this covers it top to bottom.

One honest flag before you start: the `[polish]` section (the optional LLM cleanup pass) is enabled on my machine right now, but I'm still actively tuning its prompt and guardrails as I use it day to day. Everything else here is settled.

## Prerequisites

- Windows 10 or 11.
- Python 3.12 (I'm on 3.12.10).
- An NVIDIA GPU. I'm running this on an RTX 4060 Ti with 16GB of VRAM. The `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` packages in requirements.txt bring their own CUDA runtime with them, so there's no separate CUDA Toolkit install. If you don't have a compatible GPU or the driver fails, the app falls back to CPU automatically — slower, but it works.
- Git.

## Clone, venv, install

```powershell
git clone <your fork or this repo> WisprClone
cd WisprClone
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

pip will also pull in a couple of undeclared dependencies (`comtypes`, for one) as part of installing `pycaw` — nothing extra to do there, just don't be surprised to see them in the venv.

## Pre-download the whisper model

This one isn't obvious, and skipping it is the single most likely way to get stuck on first launch. The model loader passes `local_files_only=True` (deliberately — so a flaky network can't hang the app at startup), which means it will **not** download a model for you. You have to fetch it once, yourself, before the first run:

```powershell
.venv\Scripts\python.exe -c "from faster_whisper.utils import download_model; download_model('large-v3-turbo')"
```

If you skip this, the app starts, but transcription fails and `wisprclone.log` says `model load failed; is the model downloaded?` — that line is the only hint you get, so save yourself the trip.

## Make the launcher exe

```powershell
Copy-Item .venv\Scripts\pythonw.exe .venv\Scripts\WisprClone.exe
```

This looks pointless but isn't: it's a byte-for-byte copy of `pythonw.exe` under a different name. The RUNASADMIN compatibility flag a couple steps down is set on this exact file path, not on `pythonw.exe` in general — naming it separately is what keeps every other Python script in the venv from also launching elevated.

## First launch — bare, no elevation, no autostart yet

```powershell
.venv\Scripts\WisprClone.exe wisprclone.py
```

Run this from a normal (non-admin) terminal, sitting in the repo root. You should get a tray icon, and pressing the mouse's back button (X2) should record and paste a transcription on release. Get this working before adding elevation, autostart, or polish — it isolates problems to one thing at a time.

One trap to avoid: never launch `pythonw.exe wisprclone.py` directly. It skips the venv wiring entirely and dies with `ModuleNotFoundError`. Always go through the exe you just copied.

## Personalizing config.toml

The shipped `config.toml` has inline comments explaining every knob, so I won't repeat all of them here — just the two choices worth knowing the reasoning behind:

- **Model**: I settled on `large-v3-turbo` after testing all three faster-whisper options. `distil-large-v3` felt instant but had rougher edges. `large-v3` heard best but added about a second on short push-to-talk bursts. `large-v3-turbo` gives distil-class speed with accuracy close to `large-v3`.
- **Hotwords**: a short list of words whisper tends to mishear (project names, technical terms, your own name), mined from your own dictation history once you have some (`mine_vocab.py`). Keep it short — a list padded with too many words starts causing whisper to insert them where you didn't say them.

## Personalizing corrections.txt

Copy the template and start filling it in as you dictate:

```powershell
Copy-Item corrections.txt.example corrections.txt
```

Each line is `wrong=right`, whole-word and case-insensitive, applied live with no restart needed. This file isn't required — if it's missing, the app just runs without any corrections (the read is wrapped in a try/except that silently continues) — but it's the fastest way to fix a mishearing you notice recurring. It's gitignored on purpose, since it fills up with your own name and vocabulary, not general-purpose content.

## Personalizing emphasis_words.txt

The regex cleanup step collapses an immediate repeated word ("the the mic" -> "the mic"), which also means it'll eat a word you doubled on purpose for emphasis ("this is very, very good") unless Whisper happens to punctuate your pause with a comma. Same setup as corrections.txt:

```powershell
Copy-Item emphasis_words.txt.example emphasis_words.txt
```

One word per line. Anything on this list is never collapsed, comma or not. Also optional, also gitignored, also applied live.

## RUNASADMIN — reaching elevated windows

Some apps you might dictate into (an elevated terminal, an installer) run elevated themselves, and Windows won't deliver keyboard/mouse input from a non-elevated process into an elevated one. WisprClone needs to run elevated too, or dictation just silently won't reach those windows.

Right-click `.venv\Scripts\WisprClone.exe` → Properties → Compatibility tab → check "Run this program as an administrator" → OK.

(For the curious: this writes a per-user compatibility flag to `HKCU\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers`, keyed to the exact exe path. You don't need to touch the registry directly — the Properties dialog does this for you.)

This elevation requirement is also *why* the next step uses Task Scheduler instead of a normal Startup shortcut: Windows silently skips Startup-folder shortcuts whose target is RUNASADMIN-flagged. A shortcut there would just never run, with no error anywhere.

## Task Scheduler autostart

Open Task Scheduler → Create Task (not "Create Basic Task," you need the extra options):

- **General**: name it `WisprClone`. Under Security options, select "Run with highest privileges."
- **Triggers**: New → "At log on."
- **Actions**: New → Start a program.
  - Program/script: `C:\path\to\WisprClone\.venv\Scripts\WisprClone.exe`
  - Add arguments: `wisprclone.py`
  - Start in: `C:\path\to\WisprClone`
- Leave "Run only when user is logged on" selected.

If you're comfortable with the command line, the equivalent is faster (though `schtasks` alone can't set "Start in" — either set it afterward in the Actions tab, or skip it, since `wisprclone.py` resolves its own file paths relative to itself, not the working directory):

```powershell
schtasks /create /tn WisprClone /tr "'C:\path\to\WisprClone\.venv\Scripts\WisprClone.exe' wisprclone.py" /sc onlogon /rl highest
```

## Ollama polish — optional, set this up last

Everything above gets you a fully working push-to-talk dictation tool. This section is what I added on top, and it's not required for a minimal setup.

**Install and pull the model:**

```powershell
ollama pull dolphin-mistral
```

I started with a more conservative-instruction-following model, then swapped to this one after finding the first choice would quietly sanitize profanity despite an explicit prompt rule to preserve it — an alignment habit that's hard to prompt away. It's still not perfect on that front, just meaningfully better. It's a 7B-class model, so it needs a real chunk of VRAM or RAM alongside whatever whisper is already using; if your GPU is smaller than mine, budget for that before assuming both fit comfortably at once.

**Why it exists at all, briefly:** I originally added this pass, then removed it entirely — code and Ollama dependency both — because it added latency for wording changes I didn't actually want most of the time. It came back once the regex-only cleanup step (`clean_text()`) started accumulating one hand-written pattern per quirk faster than felt sustainable. An LLM pass absorbs that whole category of fix in one place instead.

**A real bug worth knowing about if you're calling any local service on Windows:** polish was flat ~2.3-3 seconds slower than it should have been, and the cause wasn't the model. Python's `requests` library resolves `localhost` by trying IPv6 (`::1`) first, and Windows takes about 2 seconds to report that connection refused before it falls back to IPv4, which is where Ollama actually listens. Hardcoding `127.0.0.1` instead of `localhost` cut polish down to its real cost, about 0.6-1 second. If you're adding any other local HTTP call to this project, use `127.0.0.1`, never the hostname.

**Optional: run Ollama headless.** By default, installing Ollama also starts a tray app with its own icon. If you don't want that, you can run just the server, windowless, from a `.vbs` file in your Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`):

```vbscript
CreateObject("WScript.Shell").Run """C:\path\to\ollama.exe"" serve", 0, False
```

Then remove (or rename) Ollama's own Startup shortcut so its tray app doesn't also launch. WisprClone only ever talks to `127.0.0.1:11434`, so it doesn't care which way Ollama is running.

Once more, plainly: `[polish]` is turned on and working for me, but I'm still tuning the prompt and its guardrails as I go. Don't take it as a finished feature.

## Verifying it all works

- Tray icon appears, and hovering it shows a word count for today and all-time.
- Press-and-release the push-to-talk button (X2 by default) transcribes and pastes.
- Tap Right Ctrl to enter toggle mode for longer dictation; tap it again to stop.
- If polish is set up, check `wisprclone.log` after a toggle dictation — it logs a diff whenever polish actually changes the text, so you can see what it's doing.

## Troubleshooting

**Polish silently pastes raw, unpolished text.** Most likely `ollama.exe serve` isn't running — there's no tray icon anymore to hint at this if you went headless. Check with `curl http://127.0.0.1:11434/api/tags`; if that fails, Ollama isn't up.

**The app won't start, or crashes right after you edit config.toml.** There's no defaults handling — every config key is read directly, so a missing or misspelled key throws a raw `KeyError` at startup. Diff your file against the one that shipped with the repo.

**Dictation doesn't reach one specific elevated app.** WisprClone itself probably isn't elevated. Recheck the RUNASADMIN compatibility flag on `.venv\Scripts\WisprClone.exe` specifically — not `pythonw.exe`, the actual copy you made.

**It doesn't start automatically at login.** Run `Get-ScheduledTask -TaskName WisprClone` — it should show `Ready`. A plain Startup-folder shortcut gets silently skipped when its target is RUNASADMIN-flagged, which is exactly why this uses Task Scheduler instead.

**`ModuleNotFoundError` when you try to test something quickly.** You launched bare `pythonw.exe wisprclone.py` instead of going through `.venv\Scripts\WisprClone.exe wisprclone.py` (or the scheduled task). The bare launch skips the venv wiring entirely.
