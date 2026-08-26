"""Model loading, transcription worker, cleanup, and paste-into-focused-app."""

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import re
import struct
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

# cuBLAS/cuDNN come from pip wheels; their DLL dirs must be visible before
# faster_whisper (ctranslate2) is imported or CUDA init fails. ctranslate2
# loads them via plain LoadLibrary, which searches PATH and ignores
# os.add_dll_directory — so PATH gets both dirs prepended as well.
for _sub in ("cublas", "cudnn"):
    _d = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia" / _sub / "bin"
    if _d.is_dir():
        os.add_dll_directory(str(_d))
        os.environ["PATH"] = str(_d) + os.pathsep + os.environ["PATH"]

import numpy as np
import pywintypes
import requests
import win32clipboard
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions, get_speech_timestamps
from pynput.keyboard import Controller, Key

POLISH_PROMPT = (
    "Clean up this dictated speech with minimal, conservative edits:\n"
    "- Remove filler words (um, uh, you know, like) and immediate word "
    "stutters.\n"
    "- Remove a false start only when the speaker immediately restarts the "
    "same sentence. This includes repeated-word stammers like 'there were, "
    "there was' or 'I think was it, was it' - keep only the final, complete "
    "version of the restarted phrase.\n"
    "  Example: 'there were, there was a thing' -> 'there was a thing'.\n"
    "- Fix punctuation and obvious grammar slips.\n"
    "Hard rules:\n"
    "- Add nothing: never append words the speaker did not say, and never "
    "answer a question the speaker asked.\n"
    "- Never drop, merge, or reorder sentences: every sentence of the input "
    "must appear as a sentence in the output.\n"
    "- A question must remain a question.\n"
    "- Keep the speaker's own wording; do not paraphrase, summarize, or "
    "improve style. Removing a stammered restart is not paraphrasing.\n"
    "- Preserve all profanity and swear words exactly as spoken - never "
    "remove, replace, or soften them.\n"
    "- If unsure whether something is a stammer vs. intentional repetition, "
    "leave it unchanged.\n"
    "Output only the cleaned text - no preamble, no quotes, no "
    "explanation.\n\nText: "
)

log = logging.getLogger("wisprclone")

# a machine-wide HTTP_PROXY would reroute the Ollama call (no automatic
# loopback bypass for 127.0.0.1 on Windows) - never trust proxy env vars
_ollama = requests.Session()
_ollama.trust_env = False

# one set of decoder options for both the GPU and CPU-fallback calls, so an
# anti-hallucination tweak can't land on one path and miss the other.
# condition_on_previous_text feeds each chunk's text back as context for the
# next - the mechanism repetition loops and end-of-clip hallucinations feed on
DECODE_OPTS = dict(language="en", vad_filter=True,
                   condition_on_previous_text=False)

# Formats we can save and replay byte-for-byte. CF_DIB/CF_DIBV5/PNG cover a
# copied screenshot's actual image data. A screenshot also litters the
# clipboard with CF_BITMAP (a live GDI handle that dies the moment we touch
# the clipboard, not a copyable buffer) and OS plumbing (DataObject,
# cloud-clipboard flags) - none of that is content a paste target needs, so
# it's fine to drop. We only refuse to restore when TEXT shows up alongside
# something outside this set (rich text from Word/browsers) - there, partial
# restore would silently downgrade it to plain text, so we skip entirely.
_TEXT_FORMATS = {win32clipboard.CF_TEXT, win32clipboard.CF_OEMTEXT,
                 win32clipboard.CF_UNICODETEXT, win32clipboard.CF_LOCALE}
_IMAGE_FORMATS = {win32clipboard.CF_DIB, win32clipboard.CF_DIBV5,
                  win32clipboard.RegisterClipboardFormat("PNG")}
_SAFE_FORMATS = _TEXT_FORMATS | _IMAGE_FORMATS

# The same three formats password managers use to keep secrets out of
# Windows Clipboard History and Cloud Clipboard sync. Nothing we put on the
# clipboard should persist anywhere beyond the one paste it's for - dictation
# stays local per CLAUDE.md.
_EXCLUDE_FORMATS = [win32clipboard.RegisterClipboardFormat(name) for name in (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard")]

# Guards on both sides so "uh-huh" survives. The surrounding commas exist
# because of the filler pause, so they go with it ("should, uh, remove" ->
# "should remove").
_FILLER = re.compile(r",?\s*(?<![\w-])(?:um+|uh+|erm|hmm+)(?![\w-]),?\s*", re.IGNORECASE)
# Unlike um/uh, "you know" is also a real phrase ("do you know..."), so it's
# only stripped when a comma marks it as the spoken pause ("the store, you
# know, and milk" -> "the store and milk"). Bare "you know" with no comma on
# either side is left alone - misses some filler uses, but a false strip
# ("do you know" -> "do") is worse than a miss.
_YOU_KNOW = re.compile(r",\s*you know\s*,?|(?<![\w-])you know\s*,", re.IGNORECASE)
# Collapses an immediate stutter ("I I think", "the the box" -> "I think",
# "the box"). Keeps the first occurrence's own casing via the backreference.
# No comma allowed between repeats on purpose: a comma is Whisper's own
# signal of a spoken pause, which is how deliberate emphasis ("very, very
# important") differs from a real stutter (words run together, no pause).
# Residual trade-off: fast, no-pause emphasis ("very very important") still
# collapses, since there's no punctuation to tell it apart from a stutter.
_STUTTER = re.compile(r"\b(\w+)(?:\s+\1\b)+", re.IGNORECASE)
# Drops a leading "and" that starts a sentence ("And I went" -> "I went"),
# keeping whatever anchored the match (start of text, or ". ") so the next
# word still gets capitalized below. "and" mid-sentence is left alone - it's
# only the sentence-opening filler use that reads wrong in dictated text.
_LEADING_AND = re.compile(r"(^|[.!?]\s+)and\b,?\s*", re.IGNORECASE)
_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-z])")
_HISTORY_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\] (.*)$")


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND),
                ("rcCaret", wt.RECT)]


def caret_visible():
    """True when the foreground window shows a system text caret - i.e. the
    paste almost certainly landed in a real text field. Apps that draw their
    own caret read as False, which errs toward showing the repaste offer."""
    gti = _GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(gti)
    if ctypes.windll.user32.GetGUIThreadInfo(0, ctypes.byref(gti)):
        return bool(gti.hwndCaret)
    return False


_TERMINAL_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
                     "ConsoleWindowClass"}             # conhost/cmd


def is_terminal():
    """Windows Terminal and conhost draw their own cursor and expose the
    buffer as a Document/Pane to UI Automation, so caret_visible() and
    focused_editable() both read False here even though a Ctrl+V into a
    console always lands at the input line - no read-only case to miss,
    unlike a webpage."""
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(ctypes.windll.user32.GetForegroundWindow(), buf, 256)
    return buf.value in _TERMINAL_CLASSES


_uia = None


def _get_uia():
    global _uia
    if _uia is None:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        _uia = comtypes.client.CreateObject(UIA.CUIAutomation,
                                            interface=UIA.IUIAutomation)
    return _uia


def focused_editable():
    """UI Automation check for apps that draw their own caret (Electron,
    Chromium): is the focused control an Edit field? Only Edit counts -
    Document would also match read-only web pages, and a wrong True here
    hides the repaste offer exactly when it's needed."""
    try:
        el = _get_uia().GetFocusedElement()
        ct = el.CurrentControlType
        log.debug("focused control type: %d", ct)
        return ct == 50004  # UIA_EditControlTypeId
    except Exception:
        log.debug("UIA focus check failed", exc_info=True)
        return False


def focused_text():
    """Full text of the focused control (TextPattern first, ValuePattern as
    the fallback), or None when it exposes neither - the caller's signal to
    fall back to the blind same-window heuristic. Probed 2026-08-25: every
    field Uriah dictates into (Claude desktop, Firefox chrome + web content,
    Gmail compose, Notepad) answers via TextPattern."""
    try:
        from comtypes.gen import UIAutomationClient as UIA
        el = _get_uia().GetFocusedElement()
        pat = el.GetCurrentPattern(10014)  # UIA_TextPatternId
        if pat:
            return pat.QueryInterface(
                UIA.IUIAutomationTextPattern).DocumentRange.GetText(-1)
        pat = el.GetCurrentPattern(10002)  # UIA_ValuePatternId
        if pat:
            return pat.QueryInterface(
                UIA.IUIAutomationValuePattern).CurrentValue
    except Exception:
        log.debug("UIA text read failed", exc_info=True)
    return None


class Status:
    """Shared state between worker, recorder, and the UI tick."""

    def __init__(self):
        self._lock = threading.Lock()
        self._transcribing = 0
        self.ready = False
        self.device = ""
        self.mic_ok = True
        self.error = ""
        self.last_text = ""
        self.flash_error = False  # UI tick consumes this
        self.result_until = 0.0   # pill offers click-to-repaste until then
        self.words_today = 0
        self.words_total = 0
        self._word_day = datetime.now().date()

    def add_words(self, text):
        with self._lock:
            today = datetime.now().date()
            if today != self._word_day:
                self._word_day = today
                self.words_today = 0
            n = len(text.split())
            self.words_today += n
            self.words_total += n

    def inc_transcribing(self):
        with self._lock:
            self._transcribing += 1

    def dec_transcribing(self):
        with self._lock:
            self._transcribing -= 1

    @property
    def transcribing(self):
        return self._transcribing


def clean_text(text, corrections_path, emphasis_path):
    text = _FILLER.sub(" ", text)  # single space, collapsed below
    text = _YOU_KNOW.sub(" ", text)
    # emphasis_words.txt is re-read each job, same as corrections.txt, so a
    # word added mid-session takes effect without a restart. A word on this
    # list is never collapsed by _STUTTER, comma or not - the speaker said
    # outright that this word gets doubled on purpose.
    protected = set()
    try:
        for line in Path(emphasis_path).read_text(encoding="utf-8").splitlines():
            word = line.strip()
            if word and not word.startswith("#"):
                protected.add(word.lower())
    except OSError:
        pass
    text = _STUTTER.sub(
        lambda m: m.group(0) if m.group(1).lower() in protected else m.group(1),
        text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,!?;])", r"\1", text)
    text = _LEADING_AND.sub(lambda m: m.group(1), text)
    text = _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), text)
    # corrections.txt is re-read each job so edits apply without a restart
    try:
        for line in Path(corrections_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            wrong, right = line.split("=", 1)
            replacement = right.strip()
            # lambda repl: right-hand side is literal text, never a regex
            # backreference template (a bare "\1" or "\t" would otherwise
            # corrupt output or raise re.error and break every dictation)
            text = re.sub(rf"\b{re.escape(wrong.strip())}\b",
                          lambda m: replacement, text, flags=re.IGNORECASE)
    except OSError:
        pass
    # Whisper itself puts a period on short fragments ("Outdoor camping."),
    # which reads wrong in search boxes and titles. Strip it when the text
    # is short and has no other sentence punctuation; real sentences keep it.
    if (text.endswith(".") and len(text.split()) <= 5
            and not re.search(r"[.!?]", text[:-1])):
        text = text[:-1]
    return text


class Clipboard:
    def __init__(self, cfg):
        p = cfg["paste"]
        self.retries = p["clipboard_retries"]
        self.retry_s = p["clipboard_retry_ms"] / 1000
        self.restore_delay = p["restore_delay_ms"] / 1000
        self.kb = Controller()

    def _open(self):
        # OpenClipboard routinely loses races against clipboard managers
        for _ in range(self.retries):
            try:
                win32clipboard.OpenClipboard()
                return True
            except pywintypes.error:
                time.sleep(self.retry_s)
        return False

    def _mark_transient(self):
        """Call with the clipboard already open, right after writing to it."""
        zero = struct.pack("i", 0)
        for fmt in _EXCLUDE_FORMATS:
            win32clipboard.SetClipboardData(fmt, zero)

    def paste(self, text):
        saved = {}
        restorable = False
        if self._open():
            try:
                formats, f = [], 0
                while (f := win32clipboard.EnumClipboardFormats(f)):
                    formats.append(f)
                has_text = any(fmt in _TEXT_FORMATS for fmt in formats)
                restorable = not (has_text and not all(fmt in _SAFE_FORMATS for fmt in formats))
                if restorable:
                    saved = {fmt: win32clipboard.GetClipboardData(fmt)
                             for fmt in formats if fmt in _SAFE_FORMATS}
                    if formats and not saved:
                        # nothing we can safely carry forward (e.g. a copied
                        # file) - leave the dictated text rather than wipe
                        # the clipboard to empty
                        restorable = False
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                self._mark_transient()
            finally:
                win32clipboard.CloseClipboard()
        else:
            log.error("clipboard busy, dropping paste: %r", text[:80])
            return

        # A physically held modifier would corrupt the Ctrl+V chord
        for mod in (Key.ctrl, Key.shift, Key.alt):
            self.kb.release(mod)
        with self.kb.pressed(Key.ctrl):
            self.kb.tap("v")

        # No signal exists for "paste consumed"; the delay is the honest fix
        time.sleep(self.restore_delay)
        if restorable and self._open():
            try:
                win32clipboard.EmptyClipboard()
                for fmt, data in saved.items():
                    win32clipboard.SetClipboardData(fmt, data)
                # marked transient too, so restoring the user's own prior
                # clipboard doesn't create a fresh history entry for it
                self._mark_transient()
            finally:
                win32clipboard.CloseClipboard()

    def set_text(self, text):
        if self._open():
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                self._mark_transient()
            finally:
                win32clipboard.CloseClipboard()


def normalize(audio, target_peak):
    """Boosts a quiet recording (e.g. a mic several feet away) toward
    target_peak before transcription. Skips near-silence so the noise floor
    doesn't get amplified into something Whisper mistakes for speech, and
    caps the gain so a single loud pop doesn't leave the rest under-boosted."""
    if not target_peak:
        return audio
    peak = np.max(np.abs(audio))
    if peak < 1e-4:
        return audio
    gain = min(target_peak / peak, 10.0)
    return audio * gain


def trim_trailing_silence(audio, keep_s=0.25):
    """Drops the quiet tail between the last speech and the stop press.
    Long trailing silence is what sends Whisper's decoder into repetition
    loops and phantom-sentence hallucinations at the end of a clip."""
    peak = np.max(np.abs(audio))
    if peak < 1e-4:
        return audio
    # cap the threshold: one loud transient (mouse-click pop in the pre-roll,
    # desk bump) must not raise it above quiet distant-mic speech, or the
    # trim cuts real trailing words
    thr = min(peak * 0.02, 0.005)
    above = np.nonzero(np.abs(audio) > thr)[0]
    end = min(len(audio), above[-1] + int(keep_s * 16000))
    return audio[:end]


# --- Phase A streaming shadow: diagnostic only, removed when real streaming lands.
SHADOW_SILENCE_MS = (300, 400, 500, 700, 1000)


def vad_shadow(audio, jobs):
    """{min_silence_ms: [(start_s, end_s), ...]}, or None if a real job is waiting."""
    out = {}
    for ms in SHADOW_SILENCE_MS:
        if not jobs.empty():
            return None  # never make a queued dictation wait more than one pass
        ts = get_speech_timestamps(audio, VadOptions(
            min_silence_duration_ms=ms, speech_pad_ms=0,
            min_speech_duration_ms=0, max_speech_duration_s=float("inf")))
        out[ms] = [(round(t["start"] / 16000, 2), round(t["end"] / 16000, 2))
                   for t in ts]
    return out


class Transcriber(threading.Thread):
    def __init__(self, cfg, base_dir, jobs, status):
        super().__init__(daemon=True, name="transcriber")
        self.cfg = cfg["model"]
        self.decode_opts = dict(DECODE_OPTS,
                                hotwords=self.cfg.get("hotwords") or None)
        self.base_dir = base_dir
        self.jobs = jobs
        self.status = status
        self.clipboard = Clipboard(cfg)
        self.corrections = base_dir / cfg["files"]["corrections"]
        self.emphasis_words = base_dir / cfg["files"]["emphasis_words"]
        self.history = base_dir / cfg["files"]["history"]
        self.model = None
        self.cpu_model = None
        self.gpu_fails = 0
        self.result_display_s = cfg["paste"]["result_display_s"]
        self.continuation_gap_s = cfg["paste"]["continuation_gap_s"]
        self.normalize_peak = cfg["audio"]["normalize_peak"]
        self.polish_cfg = cfg["polish"]
        # Temporary experiment data (streaming Phase A), not a feature: each
        # dictation's audio is kept so mine_merge_rule.py can replay the
        # chunked pipeline offline. Goes away with the shadow; config.toml's
        # [retain] comment says how to clear it out.
        self.retain_dir = base_dir / cfg["retain"]["dir"]
        self.retain_mb = cfg["retain"]["max_mb"]
        # sentence-continuity tracking: the last paste's window, tail (its
        # final chars, whitespace-normalized, for matching against the
        # focused field's UIA text), and end-punctuation state, so a
        # follow-up dictation can retroactively close an unfinished
        # sentence instead of running the two together
        self.last_hwnd = None
        self.last_tail = ""
        self.last_ended_sentence = True
        self.last_paste_ts = 0.0

    def _load_word_counts(self):
        """Word counters live in Status but must survive a restart, so seed
        them from history.log rather than starting at zero every launch."""
        today = datetime.now().date().isoformat()
        total = today_words = 0
        try:
            with open(self.history, encoding="utf-8") as f:
                for line in f:
                    m = _HISTORY_LINE.match(line)
                    if not m:
                        continue
                    n = len(m.group(2).split())
                    total += n
                    if m.group(1) == today:
                        today_words += n
        except OSError:
            pass
        self.status.words_total = total
        self.status.words_today = today_words

    def _load(self, device):
        m = WhisperModel(self.cfg["name"], device=device,
                         compute_type=self.cfg["compute_type"] if device == "cuda" else "int8",
                         local_files_only=True)
        # transcribe() is lazy and VAD short-circuits silence before the
        # encoder runs, so a warmup must disable VAD and consume the generator
        segs, _ = m.transcribe(np.zeros(16000, dtype=np.float32),
                               language="en", vad_filter=False)
        list(segs)
        return m

    def _load_models(self):
        device = self.cfg["device"]
        if device in ("auto", "cuda"):
            try:
                self.model = self._load("cuda")
                self.status.device = "cuda"
            except Exception:
                log.exception("CUDA load failed")
                if device == "cuda":
                    raise
        if self.model is None:
            self.model = self._load("cpu")
            self.cpu_model = self.model
            self.status.device = "cpu"

    def _transcribe(self, audio):
        try:
            if self.status.device == "cuda":
                segs, _ = self.model.transcribe(audio, **self.decode_opts)
                result = list(segs)
                self.gpu_fails = 0
                return result
        except Exception:
            log.exception("GPU transcription failed, retrying on CPU")
            self.gpu_fails += 1
            if self.gpu_fails >= self.cfg["gpu_fail_latch"]:
                log.error("latching to CPU after %d GPU failures", self.gpu_fails)
                self.status.device = "cpu"
        if self.cpu_model is None:
            self.cpu_model = self._load("cpu")
        segs, _ = self.cpu_model.transcribe(audio, **self.decode_opts)
        return list(segs)

    def _warm_polish(self):
        """Bare model-load request: no prompt, so no generation and none of
        _polish's guards, and its own generous timeout - a cold load off disk
        takes far longer than the warm generations timeout_s is sized for."""
        try:
            _ollama.post("http://127.0.0.1:11434/api/generate", json={
                "model": self.polish_cfg["model"], "keep_alive": "24h",
                # same num_ctx as _polish, or this warmup loads the model at
                # Ollama's 4096 default and the first real polish pays a full
                # reload mid-dictation (~4s, measured)
                "options": {"num_ctx": 8192},
            }, timeout=120)
        except Exception as e:
            log.warning("polish warmup failed (%s); first toggle dictation "
                        "may be slow or unpolished", e)

    def _polish(self, text):
        """LLM cleanup for rambling toggle-mode dictations - false starts,
        run-on sentences. Any failure (Ollama down, timeout, suspicious
        output length) falls back to the raw transcript rather than risk
        losing or corrupting the dictation. Returns (text, status); status
        distinguishes a real no-edit-needed pass ("ok") from a fallback
        ("timeout"/"error"/etc.) since both look identical in the pasted
        text - the job log line is where the difference has to show up."""
        p = self.polish_cfg
        try:
            # 127.0.0.1, NOT localhost: Windows tries IPv6 ::1 first and eats
            # ~2s falling back to IPv4, where Ollama actually listens
            r = _ollama.post("http://127.0.0.1:11434/api/generate", json={
                "model": p["model"], "stream": False, "keep_alive": "24h",
                "prompt": POLISH_PROMPT + text,
                # num_ctx: Ollama's runtime default is 4096 tokens, which a
                # max-length dictation approaches; overflow silently truncates
                # the FRONT of the prompt - the instructions - leaving the
                # model free-running on bare dictation text
                "options": {"temperature": 0, "num_ctx": 8192},
            }, timeout=p["timeout_s"])
            r.raise_for_status()
            polished = r.json()["response"].strip()
            ratio = len(polished) / max(1, len(text))
            if not polished or not (p["min_ratio"] <= ratio <= p["max_ratio"]):
                log.warning("polish output length suspicious (ratio %.2f), using raw", ratio)
                return text, "suspicious"
            if polished.count("?") < text.count("?"):
                # a dropped short question passes the ratio guard easily
                log.warning("polish dropped a question, using raw")
                return text, "dropped_question"
            return polished, "ok"
        except requests.Timeout:
            log.exception("polish timed out, using raw text")
            # a post-eviction cold model load is the likely cause; re-warm in
            # the background so the next dictation gets polished
            threading.Thread(target=self._warm_polish, daemon=True).start()
            self.status.flash_error = True
            return text, "timeout"
        except Exception:
            log.exception("polish pass failed, using raw text")
            self.status.flash_error = True
            return text, "error"

    def _retain_audio(self, audio, now):
        """Saves the exact audio the shadow VAD saw (post-trim,
        post-normalize) so the logged segment bounds line up with the WAV
        sample-for-sample. Returns the filename for the shadow record's
        "wav" field, or None when retention is disabled. Prunes oldest-first
        past the cap; the name sorts chronologically, which is what makes
        the sort below a time order."""
        if not self.retain_mb:
            return None
        self.retain_dir.mkdir(exist_ok=True)
        name = f"{now:%Y%m%d_%H%M%S}.wav"
        n = 0
        while (self.retain_dir / name).exists():  # two jobs in one second
            n += 1
            name = f"{now:%Y%m%d_%H%M%S}_{n}.wav"
        with wave.open(str(self.retain_dir / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
        wavs = sorted(self.retain_dir.glob("*.wav"))
        total = sum(p.stat().st_size for p in wavs)
        while len(wavs) > 1 and total > self.retain_mb * 1_000_000:
            total -= wavs[0].stat().st_size
            wavs.pop(0).unlink()
        return name

    def run(self):
        self._load_word_counts()
        try:
            self._load_models()
        except Exception:
            log.exception("model load failed; is the model downloaded?")
            self.status.error = "model unavailable"
            # drain jobs forever so recordings don't pile up
            while True:
                self.jobs.get()
                self.status.dec_transcribing()
                self.status.flash_error = True
        self.status.ready = True
        log.info("model ready on %s", self.status.device)
        if self.polish_cfg["enabled"]:
            # background: don't delay PTT readiness for a model toggle mode
            # alone needs. A cold Ollama load is the 3s+ delay a first
            # dictation would otherwise pay.
            threading.Thread(target=self._warm_polish, daemon=True).start()

        while True:
            job = self.jobs.get()
            t0 = time.monotonic()
            try:
                audio = np.concatenate(job["blocks"])
                audio = trim_trailing_silence(audio)
                audio = normalize(audio, self.normalize_peak)
                segments = self._transcribe(audio)
                text = " ".join(s.text.strip() for s in segments
                                if s.no_speech_prob < 0.6)
                text = clean_text(text, self.corrections, self.emphasis_words)
                if not text:
                    continue  # silence/hallucination: paste nothing
                t1 = time.monotonic()
                # duration decides polish, not which hotkey started the
                # recording - short bursts stay instant, anything longer
                # gets cleaned regardless of mode
                polish_status = "skipped"
                if (self.polish_cfg["enabled"]
                        and len(audio) / 16000 >= self.polish_cfg["min_audio_s"]):
                    raw = text
                    text, polish_status = self._polish(text)
                    if text != raw:
                        log.info("polish changed text:\n  raw: %s\n  out: %s", raw, text)
                t2 = time.monotonic()
                log.info("job: audio=%.1fs whisper=%.2fs polish=%.2fs mode=%s polish_status=%s",
                         len(audio) / 16000, t1 - t0, t2 - t1, job["mode"], polish_status)
                self.status.last_text = text
                self.status.add_words(text)
                with open(self.history, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")

                # Continuation, ground truth first: read the focused field
                # via UIA. If its text still ends with our previous paste
                # and that paste had no closing punctuation, the user is
                # continuing the same thought -> stitch a period on. A sent,
                # cleared, or hand-edited field simply fails the match and
                # starts fresh. Fields that expose no text fall back to the
                # blind heuristic (same window within continuation_gap_s).
                # Terminals never stitch: a period would corrupt a command.
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                stitch = False
                if not self.last_ended_sentence and not is_terminal():
                    field = focused_text()
                    if field is not None:
                        stitch = bool(self.last_tail) and \
                            " ".join(field.split()).endswith(self.last_tail)
                    else:
                        stitch = (hwnd == self.last_hwnd
                                  and time.monotonic() - self.last_paste_ts
                                  < self.continuation_gap_s)
                    log.info("continuation: %s stitch=%s",
                             "field-read" if field is not None else "blind", stitch)
                if stitch:
                    joined = ". " + text
                elif self.last_hwnd is None:
                    joined = text  # very first dictation - nothing to bridge from
                else:
                    joined = " " + text

                self.clipboard.paste(joined)
                self.last_hwnd = hwnd
                self.last_tail = " ".join(text.split())[-60:]
                self.last_ended_sentence = text[-1] in ".!?"
                self.last_paste_ts = time.monotonic()

                # offer click-to-repaste only when the paste probably missed
                if not (caret_visible() or focused_editable() or is_terminal()):
                    self.status.result_until = time.monotonic() + self.result_display_s

                # Streaming shadow (Phase A): log the segment bounds streaming
                # would have used. Post-paste so it can't be felt; bails
                # between passes when a job waits; own except so it can't
                # flash the pill.
                try:
                    t3 = time.monotonic()
                    segs = vad_shadow(audio, self.jobs)
                    if segs is not None:
                        s700 = [b for b in segs[700] if b[1] - b[0] >= 0.2]  # headline sans blips
                        speech = sum(e - s for s, e in s700)
                        log.info("vad shadow: 700ms segs=%d last=%.1fs longest=%.1fs "
                                 "pause=%.1fs shadow=%.2fs",
                                 len(s700), s700[-1][1] - s700[-1][0] if s700 else 0,
                                 max((e - s for s, e in s700), default=0),
                                 len(audio) / 16000 - speech, time.monotonic() - t3)
                        now = datetime.now()
                        # own try: a full disk must not cost the shadow record
                        wav = None
                        try:
                            wav = self._retain_audio(audio, now)
                        except Exception:
                            log.exception("audio retention failed")
                        rec = {"ts": f"{now:%Y-%m-%d %H:%M:%S}",
                               "mode": job["mode"], "audio_s": round(len(audio) / 16000, 1),
                               "whisper_s": round(t1 - t0, 2), "polish_s": round(t2 - t1, 2),
                               "chars": len(text), "shadow_s": round(time.monotonic() - t3, 2),
                               "wav": wav,
                               "segs": {str(ms): [list(b) for b in bs] for ms, bs in segs.items()}}
                        with open(self.base_dir / "vad_shadow.log", "a", encoding="utf-8") as f:
                            f.write(json.dumps(rec) + "\n")
                except Exception:
                    log.exception("vad shadow failed")
            except Exception:
                log.exception("transcription job failed")
                self.status.flash_error = True
            finally:
                self.status.dec_transcribing()


if __name__ == "__main__":
    # Self-test: transcribe a wav file. Proves DLL wiring, CUDA, and fallback.
    import tomllib
    import wave

    logging.basicConfig(level=logging.INFO)
    with open(Path(__file__).parent / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    with wave.open(sys.argv[1], "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, "need 16kHz mono"
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        audio = audio.astype(np.float32) / 32768.0

    t = Transcriber(cfg, Path(__file__).parent, None, Status())
    start = time.monotonic()
    t._load_models()
    print(f"loaded+warmed on {t.status.device} in {time.monotonic() - start:.1f}s")
    start = time.monotonic()
    segments = t._transcribe(audio)
    text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6)
    print(f"transcribed in {time.monotonic() - start:.2f}s: "
          f"{clean_text(text, t.corrections, t.emphasis_words)!r}")
