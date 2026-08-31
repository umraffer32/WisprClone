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
from collections import Counter
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

# Smart App Control can revoke a previously-fine unsigned binary's reputation
# at any time - it has intermittently blocked av\audio\frame.pyd (PyAV),
# which faster_whisper imports at module level, so the whole app died at
# startup. PyAV only backs faster_whisper's decode_audio(), and every
# transcribe() call here passes a numpy array from the mic, never a file, so
# a stub module in its place loses nothing.
try:
    import av
except (ImportError, OSError) as _e:
    import types
    sys.modules["av"] = types.ModuleType("av")
    logging.getLogger("wisprclone").warning(
        "import av failed (%s); stubbed it so faster_whisper can load", _e)

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

# A model's alignment can quietly sanitize swears despite the prompt's
# preserve-profanity rule (qwen2.5 rewrote "dog shit" out entirely,
# 2026-08-25), so _polish backs the rule with a count check against this
# list. Inflections are spelled out because \b can't connect "fucking" to
# "fuck". Internal sanity list, not a config knob. Matched against
# lowercased text, so no IGNORECASE needed.
_SWEARS = re.compile(
    r"\b(?:fuck(?:ing|ed|er|ers)?|motherfuck(?:ing|er|ers)?|shit(?:s|ty)?|"
    r"bullshit|damn|dammit|goddamn(?:it)?|ass(?:es|hole|holes)?|"
    r"bitch(?:es|y)?|bastards?|crap(?:py)?|piss(?:ed|ing)?|dicks?|cocks?|"
    r"cunts?|pricks?|whores?|sluts?)\b")

# The prompt's never-drop-sentences rule is the one qwen2.5 breaks most
# quietly: a whole sentence vanishing from a multi-sentence dictation moves
# the length ratio too little to trip min_ratio and needn't be a question
# (405-case replay 2026-08-27: 3 whole-sentence drops, none caught by the
# guards above). _lost_sentence backs that rule the way _SWEARS backs
# profanity: a sentence counts as surviving only if at least half its
# content words (or their crude stems, so "causing"->"caused" still
# matches) appear anywhere in the output. Function words and fillers don't
# count - polish removes those legitimately - and a stammered restart can't
# false-fire because its words survive in the kept version of the phrase.
# Digits don't count either: qwen reformats them ("830" -> "8:30"), which
# only looks like a mismatch. Internal sanity thresholds, not config knobs.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_GUARD_WORD = re.compile(r"[a-z']+")
_GUARD_STOP = frozenset("""
a an the and or but if so to of in on at for with by from as is are was were
be been being am i you he she it we they me him her us them my your his its
our their this that these those there here not no nor do does did doing have
has had having will would can could should shall may might must what which
who whom whose when where why how then than too very also just really
actually basically literally kind sort course okay ok yeah yes well um uh
erm hmm like know mean gonna wanna oh all some any more most other into out
up down over under again once about because while during before after
""".split())


def _guard_stem(w):
    for suf in ("ing", "ed", "es", "s", "ly"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def _guard_words(s):
    return [w for w in (t.strip("'") for t in _GUARD_WORD.findall(s.lower()))
            if w and w not in _GUARD_STOP]


def _lost_sentence(text, polished):
    """First input sentence whose content didn't survive into polished
    (under half its content words present), or None. Sentences with fewer
    than 2 content words carry too little signal to judge and are skipped."""
    out_words = set(_guard_words(polished))
    out_stems = {_guard_stem(w) for w in out_words}
    for sent in _SENT_SPLIT.split(text):
        words = _guard_words(sent)
        if len(words) < 2:
            continue
        hits = sum(1 for w in words
                   if w in out_words or _guard_stem(w) in out_stems)
        if hits / len(words) < 0.5:
            return sent
    return None

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
# Whisper occasionally hallucinates one word repeated dozens of times with a
# comma after each ("Xeon, Xeon, Xeon, ..." x73, 2026-08-28) - the commas make
# it read as emphasis to _STUTTER, so it sailed through untouched. This
# collapses 4+ exact repeats of the same word, comma-separated or not, down to
# the first occurrence (keeping its casing via the backreference). Kept
# separate from _STUTTER because a real spoken triple ("no, no, no",
# 2026-08-25) is a confirmed legitimate case that must not be touched: the
# whole log corpus tops out at 3x for real speech, and the only hallucination
# seen was 73x, with nothing in between - so 4 is the threshold.
_RUNAWAY_REPEAT = re.compile(r"\b(\w+)\b(?:[\s,]+\1\b){3,}", re.IGNORECASE)


def _collapse_runaway(m, protected):
    # an emphasis_words.txt word is exempt here too, same as in _STUTTER -
    # the speaker opted this word out of repeat-collapsing outright, and a
    # run they asked for isn't a hallucination worth a warning
    if m.group(1).lower() in protected:
        return m.group(0)
    log.warning("hallucinated repeat run: %r x%d collapsed to one",
                m.group(1), len(re.split(r"[\s,]+", m.group(0))))
    return m.group(1)


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


# GUI_INMENUMODE | GUI_SYSTEMMENUMODE | GUI_POPUPMENUMODE (winuser.h)
_GUI_MENU_FLAGS = 0x0004 | 0x0008 | 0x0010
_MENU_CONTROL_TYPES = {50009, 50010, 50011}  # UIA Menu, MenuBar, MenuItem


def paste_blocked():
    """True while a popup menu owns input on the foreground thread - an
    injected Ctrl+V would go to the menu, not the text field, and a stray
    'v' can even activate a menu item. Native Win32 menus (Notepad,
    Explorer, Electron apps) run a modal loop that sets the menu-mode
    flags; Chromium and Firefox draw their own menu popups, which hold
    mouse capture (that's how they dismiss on an outside click) and report
    a menu-ish focused element via UI Automation."""
    gti = _GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(gti)
    if ctypes.windll.user32.GetGUIThreadInfo(0, ctypes.byref(gti)):
        if gti.flags & _GUI_MENU_FLAGS or gti.hwndCapture:
            return True
    try:
        return _get_uia().GetFocusedElement().CurrentControlType in _MENU_CONTROL_TYPES
    except Exception:
        return False


class Status:
    """Shared state between worker, recorder, and the UI tick."""

    def __init__(self):
        self._lock = threading.Lock()
        self._transcribing = 0
        self.ready = False
        self.device = ""
        self.mic_ok = True
        self.recording = False  # Recorder mirrors the live recording here;
                                # the worker's GPU re-warm loop watches it
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
    # list is never collapsed by _STUTTER or _RUNAWAY_REPEAT, comma or not -
    # the speaker said outright that this word gets doubled on purpose.
    protected = set()
    try:
        for line in Path(emphasis_path).read_text(encoding="utf-8").splitlines():
            word = line.strip()
            if word and not word.startswith("#"):
                protected.add(word.lower())
    except OSError:
        pass
    # before _STUTTER on purpose: it needs to see the full run, or _STUTTER
    # collapsing any comma-free pairs inside a mixed run first could shrink
    # it below the 4x threshold
    text = _RUNAWAY_REPEAT.sub(lambda m: _collapse_runaway(m, protected), text)
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
        # the user has their text from here on; returned so the job line's
        # paste= timing stops at the keystroke, not the restore sleep
        sent = time.monotonic()

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
        return sent

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


# Both derived from measurement, not tunable feel - so code, not config.toml.
# 2s: how long the clock boost survives a warm under real desktop load
# (ambient GPU activity cycles clocks every ~5s and drags it down; the idle
# bench's 4-5s was optimistic). 15s: PTT p90 is 16s - the bound is on
# STARTING a warm, and the last one's boost carries coverage ~2s past it.
WARM_INTERVAL_S = 2.0
WARM_BOUND_S = 15.0


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

    def _warm_gpu(self):
        """Enqueued by Recorder.start_recording as {"warm": True}: ramps the
        GPU clocks back up while the user is still talking, so the real job
        lands hot. One warm isn't enough - under desktop load the boost
        decays ~2s after a warm's work ends (live clock trace 2026-08-27) -
        so this re-warms on that period while status.recording holds,
        giving up at WARM_BOUND_S: PTT audio is median 5.7s / p90 16s, so
        ~15s covers nearly all of it, while a minutes-long toggle must not
        sustain periodic GPU draw (streaming owns long-recording latency).
        Between warms it polls every 30ms, so a real job - or the recording
        ending, including a too-short discard, which enqueues nothing -
        waits behind at most the one warm already in flight (~0.2s hot).
        Bypasses _transcribe on purpose - a failed warm must not count
        toward the CPU latch. Same constraint as _load's warmup: VAD off
        and the generator consumed, or the encoder never runs. Failures are
        swallowed - a warm is optional and must never flash the pill."""
        start = time.monotonic()
        while True:
            try:
                segs, _ = self.model.transcribe(np.zeros(16000, dtype=np.float32),
                                                language="en", vad_filter=False)
                list(segs)
            except Exception:
                log.exception("gpu warm failed")
                return  # broken once means broken on retry - log it once
            next_warm = time.monotonic() + WARM_INTERVAL_S
            while time.monotonic() < next_warm:
                if (not self.status.recording or not self.jobs.empty()
                        or time.monotonic() - start > WARM_BOUND_S):
                    return
                time.sleep(0.03)

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
                # model free-running on bare dictation text.
                # num_predict: the ratio guard below rejects anything past
                # max_ratio of the input anyway (~4 chars/token, 64-token
                # floor for tiny inputs), so cap generation there - unbounded,
                # a runaway ran 170s / 25k chars before the guard saw it
                "options": {"temperature": 0, "num_ctx": 8192,
                            "num_predict": max(64, int(len(text) / 4 * p["max_ratio"]))},
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
            if Counter(_SWEARS.findall(text.lower())) - Counter(
                    _SWEARS.findall(polished.lower())):
                # counts, not presence: "shit" twice in, once out is a loss
                log.warning("polish dropped profanity, using raw")
                return text, "dropped_profanity"
            lost = _lost_sentence(text, polished)
            if lost:
                log.warning("polish dropped a sentence (%r), using raw", lost)
                return text, "dropped_sentence"
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
                if "warm" in self.jobs.get():
                    continue  # no counter to balance, no dictation lost
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
            if "warm" in job:
                # before the try: its finally decrements transcribing,
                # which warm jobs never incremented
                self._warm_gpu()
                continue
            t0 = time.monotonic()
            try:
                audio = np.concatenate(job["blocks"])
                audio = trim_trailing_silence(audio)
                audio = normalize(audio, self.normalize_peak)
                segments = self._transcribe(audio)
                whisper_text = " ".join(s.text.strip() for s in segments
                                        if s.no_speech_prob < 0.6)
                text = clean_text(whisper_text, self.corrections, self.emphasis_words)
                if text != whisper_text:
                    # same two-line shape as the polish diff so the regex
                    # layer's edits are auditable too; the different lead-in
                    # keeps it out of mine_polish.py's block parser
                    log.info("clean_text changed text:\n  raw: %s\n  out: %s",
                             whisper_text, text)
                if not text:
                    continue  # silence/hallucination: paste nothing
                t1 = time.monotonic()
                # duration decides polish, not which hotkey started the
                # recording - short bursts stay instant, anything longer
                # gets cleaned regardless of mode
                polish_status = "skipped"
                polish_raw = None
                if (self.polish_cfg["enabled"]
                        and len(audio) / 16000 >= self.polish_cfg["min_audio_s"]):
                    polish_raw = text
                    text, polish_status = self._polish(text)
                    if text == polish_raw:
                        polish_raw = None
                t2 = time.monotonic()
                self.status.last_text = text
                self.status.add_words(text)
                with open(self.history, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")

                # A right-click mid-dictation leaves a context menu open
                # over the target field. The menu eats the injected Ctrl+V,
                # the clipboard restore wipes the text 300ms later, and the
                # caret-based landed check used at the time stayed quiet
                # because the field behind the menu still owned its caret -
                # the dictation vanished with no repaste offer (reported
                # 2026-08-27). If a menu is up now, skip the keystroke and
                # offer click-to-repaste.
                # The first fix polled up to 5s and auto-pasted the moment
                # the menu closed; reverted same day. The real sequence is
                # notice the stray right-click, stop talking, close the
                # menu - and at that point the interrupted dictation should
                # be an option to click, not text that lands unprompted the
                # instant the menu happens to clear. So a menu open at
                # paste time always means the pill, immediately.
                pastable = not paste_blocked()

                # Continuation, ground truth first: read the focused field
                # via UIA. If its text still ends with our previous paste
                # and that paste had no closing punctuation, the user is
                # continuing the same thought -> stitch a period on. A sent,
                # cleared, or hand-edited field simply fails the match and
                # starts fresh. Fields that expose no text fall back to the
                # blind heuristic (same window within continuation_gap_s).
                # Terminals never stitch (a period would corrupt a command)
                # and skip the read: their UIA text is the whole viewport,
                # not the input line.
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                terminal = is_terminal()
                field = None if terminal else focused_text()
                stitch = False
                if not self.last_ended_sentence and not terminal:
                    if field is not None:
                        stitch = bool(self.last_tail) and \
                            " ".join(field.split()).endswith(self.last_tail)
                    else:
                        stitch = (hwnd == self.last_hwnd
                                  and time.monotonic() - self.last_paste_ts
                                  < self.continuation_gap_s)
                    log.info("continuation: %s stitch=%s",
                             "field-read" if field is not None else "blind", stitch)
                # The same field read decides the leading space: pasting
                # after existing content needs one so words don't run
                # together, but an empty field - or one already ending in
                # whitespace - doesn't. Keying this off "first dictation
                # since launch" instead put a stray space at the start of
                # every fresh chat box (reported 2026-08-27). An unreadable
                # field keeps the space: a stray space is invisible and
                # send boxes trim it, glued words corrupt the dictation.
                if stitch:
                    joined = ". " + text
                elif field is not None and (not field or field[-1].isspace()):
                    joined = text
                else:
                    joined = " " + text

                if pastable:
                    t3 = self.clipboard.paste(joined)
                    if t3 is None:
                        t3 = time.monotonic()  # clipboard busy - nothing was sent
                else:
                    log.warning("paste blocked by an open menu; offering repaste")
                    t3 = time.monotonic()
                # both logged only now so paste= reaches the Ctrl+V;
                # mine_polish.py needs the diff block immediately before its
                # job line (the continuation line would otherwise split them)
                if polish_raw is not None:
                    log.info("polish changed text:\n  raw: %s\n  out: %s",
                             polish_raw, text)
                log.info("job: audio=%.1fs whisper=%.2fs polish=%.2fs mode=%s "
                         "polish_status=%s paste=%.2fs",
                         len(audio) / 16000, t1 - t0, t2 - t1, job["mode"],
                         polish_status, t3 - t2)
                if pastable:
                    # a skipped paste must not update continuation state:
                    # the field never got this text, so the next dictation
                    # should stitch (or not) against the last real paste
                    self.last_hwnd = hwnd
                    self.last_tail = " ".join(text.split())[-60:]
                    self.last_ended_sentence = text[-1] in ".!?"
                    self.last_paste_ts = time.monotonic()

                # Offer click-to-repaste only when the paste probably
                # missed. Ground truth first, same as the continuation
                # read: re-read the focused field (paste()'s restore sleep
                # already gave the app time to consume the Ctrl+V) and
                # look for the pasted text. Guessing from control type
                # flashed the pill on clean pastes into VS Code's Monaco
                # editor and web composers like eBay's - both draw their
                # own caret and report as a UIA Document, not Edit
                # (reported 2026-08-31). A mid-document paste won't sit at
                # the field's end, so the tail appearing now when the
                # pre-paste read lacked it also counts - "lacked it"
                # matters, or a short dictation already sitting in the
                # field would mask a paste that really missed. Terminals
                # skip the read as above; a field readable as empty or not
                # at all keeps the old heuristic.
                if pastable:
                    verify = None if terminal else focused_text()
                    if verify:
                        after = " ".join(verify.split())
                        landed = after.endswith(self.last_tail) or (
                            field is not None
                            and self.last_tail in after
                            and self.last_tail not in " ".join(field.split()))
                    else:
                        landed = (caret_visible() or focused_editable()
                                  or is_terminal())
                else:
                    landed = False
                if not landed:
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
