"""Model loading, transcription worker, cleanup, and paste-into-focused-app."""

import ctypes
import ctypes.wintypes as wt
import logging
import os
import re
import sys
import threading
import time
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
import win32clipboard
from faster_whisper import WhisperModel
from pynput.keyboard import Controller, Key

log = logging.getLogger("wisprclone")

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

# Guards on both sides so "uh-huh" survives. The surrounding commas exist
# because of the filler pause, so they go with it ("should, uh, remove" ->
# "should remove").
_FILLER = re.compile(r",?\s*(?<![\w-])(?:um+|uh+|erm|hmm+)(?![\w-]),?\s*", re.IGNORECASE)
_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-z])")


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


_uia = None


def focused_editable():
    """UI Automation check for apps that draw their own caret (Electron,
    Chromium): is the focused control an Edit field? Only Edit counts -
    Document would also match read-only web pages, and a wrong True here
    hides the repaste offer exactly when it's needed."""
    global _uia
    try:
        if _uia is None:
            import comtypes
            import comtypes.client
            comtypes.CoInitialize()
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA
            _uia = comtypes.client.CreateObject(UIA.CUIAutomation,
                                                interface=UIA.IUIAutomation)
        el = _uia.GetFocusedElement()
        ct = el.CurrentControlType
        log.debug("focused control type: %d", ct)
        return ct == 50004  # UIA_EditControlTypeId
    except Exception:
        log.debug("UIA focus check failed", exc_info=True)
        return False


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

    def inc_transcribing(self):
        with self._lock:
            self._transcribing += 1

    def dec_transcribing(self):
        with self._lock:
            self._transcribing -= 1

    @property
    def transcribing(self):
        return self._transcribing


def clean_text(text, corrections_path):
    text = _FILLER.sub(" ", text)  # single space, collapsed below
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,!?;])", r"\1", text)
    text = _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), text)
    # corrections.txt is re-read each job so edits apply without a restart
    try:
        for line in Path(corrections_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            wrong, right = line.split("=", 1)
            text = re.sub(rf"\b{re.escape(wrong.strip())}\b", right.strip(),
                          text, flags=re.IGNORECASE)
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
            finally:
                win32clipboard.CloseClipboard()

    def set_text(self, text):
        if self._open():
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()


class Transcriber(threading.Thread):
    def __init__(self, cfg, base_dir, jobs, status):
        super().__init__(daemon=True, name="transcriber")
        self.cfg = cfg["model"]
        self.base_dir = base_dir
        self.jobs = jobs
        self.status = status
        self.clipboard = Clipboard(cfg)
        self.corrections = base_dir / cfg["files"]["corrections"]
        self.history = base_dir / cfg["files"]["history"]
        self.model = None
        self.cpu_model = None
        self.gpu_fails = 0
        self.result_display_s = cfg["paste"]["result_display_s"]

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
                segs, _ = self.model.transcribe(audio, language="en", vad_filter=True)
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
        segs, _ = self.cpu_model.transcribe(audio, language="en", vad_filter=True)
        return list(segs)

    def run(self):
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

        while True:
            blocks = self.jobs.get()
            try:
                audio = np.concatenate(blocks)
                segments = self._transcribe(audio)
                text = " ".join(s.text.strip() for s in segments
                                if s.no_speech_prob < 0.6)
                text = clean_text(text, self.corrections)
                if not text:
                    continue  # silence/hallucination: paste nothing
                self.status.last_text = text
                with open(self.history, "a", encoding="utf-8") as f:
                    f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
                # trailing space so consecutive dictations don't run together
                self.clipboard.paste(text + " ")
                # offer click-to-repaste only when the paste probably missed
                if not (caret_visible() or focused_editable()):
                    self.status.result_until = time.monotonic() + self.result_display_s
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
    print(f"transcribed in {time.monotonic() - start:.2f}s: {clean_text(text, t.corrections)!r}")
