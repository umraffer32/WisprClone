"""Paste through the clipboard: save what's there, Ctrl+V the text, put it back."""

import logging
import struct
import threading
import time

import pywintypes
import win32clipboard
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

# The same three formats password managers use to keep secrets out of
# Windows Clipboard History and Cloud Clipboard sync. Nothing we put on the
# clipboard should persist anywhere beyond the one paste it's for - dictation
# stays local per CLAUDE.md.
_EXCLUDE_FORMATS = [win32clipboard.RegisterClipboardFormat(name) for name in (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard")]


class Clipboard:
    def __init__(self, cfg):
        p = cfg["paste"]
        self.retries = p["clipboard_retries"]
        self.retry_s = p["clipboard_retry_ms"] / 1000
        self.restore_delay = p["restore_delay_ms"] / 1000
        self.read_timeout = p["clipboard_read_timeout_ms"] / 1000
        self.kb = Controller()
        # Three threads reach this one instance: the Transcriber worker, the
        # thread a repaste click spawns, and pystray's for "Re-copy last".
        # Held across the whole save/write/paste/restore sequence on purpose -
        # a second paste starting mid-sequence would save our own text as the
        # "prior" clipboard and restore that instead of the user's.
        self._lock = threading.Lock()

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

    def _replace_clipboard(self, populate):
        """Call with the clipboard already open: clears it, lets populate()
        write the new contents, then marks the result transient - including
        a restored prior clipboard, so putting it back doesn't create a
        fresh history entry for it."""
        win32clipboard.EmptyClipboard()
        try:
            populate()
        finally:
            # even on a failed write: whatever did land must not reach
            # Clipboard History, and on the restore path that's the user's
            # own prior clipboard
            self._mark_transient()

    def _write_text(self, text):
        self._replace_clipboard(
            lambda: win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT))

    def _write_text_direct(self, text):
        """Same job as _write_text, but skips EmptyClipboard - used only
        after _paste has already given up on the prior owner (see
        _read_prior_clipboard's timeout). EmptyClipboard synchronously
        notifies the CURRENT owner via WM_DESTROYCLIPBOARD before handing
        us the clipboard, which blocks exactly like the abandoned read did.
        SetClipboardData just overwrites the format directly, so a reader
        asking for CF_UNICODETEXT gets our text without waiting on that
        owner at all. Leaves whatever other formats it had registered."""
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
        self._mark_transient()

    def _read_prior_clipboard(self, result):
        """Runs on its own thread, called by _paste with a bounded join().
        GetClipboardData blocks for as long as the clipboard owner takes to
        service WM_RENDERFORMAT for a delayed-rendered format - a hung app
        (Firefox gone Not Responding, 2026-09-14) can block it for as long
        as it stays hung. Win32 clipboard calls are thread-affined (only
        the thread that opened the clipboard may read or close it), so
        _paste can't just time out a call it made itself - it has to hand
        the read to a thread it's willing to abandon. If _paste gives up
        waiting, this thread is left to finish (or never does); its
        CloseClipboard is then a no-op at best, since _paste's own reopen
        to write the dictated text may already have closed that session."""
        if not self._open():
            return
        try:
            formats, f = [], 0
            while (f := win32clipboard.EnumClipboardFormats(f)):
                formats.append(f)
            has_text = any(fmt in _TEXT_FORMATS for fmt in formats)
            restorable = not (has_text and not all(fmt in _SAFE_FORMATS for fmt in formats))
            saved = {}
            if restorable:
                saved = {fmt: win32clipboard.GetClipboardData(fmt)
                         for fmt in formats if fmt in _SAFE_FORMATS}
                if formats and not saved:
                    # nothing we can safely carry forward (e.g. a copied
                    # file) - leave the dictated text rather than wipe
                    # the clipboard to empty
                    restorable = False
            result["saved"], result["restorable"], result["done"] = saved, restorable, True
        finally:
            try:
                win32clipboard.CloseClipboard()
            except pywintypes.error:
                pass  # this session may already be gone - see docstring

    def paste(self, text):
        with self._lock:
            return self._paste(text)

    def _paste(self, text):
        read = {}
        reader = threading.Thread(target=self._read_prior_clipboard, args=(read,), daemon=True)
        reader.start()
        reader.join(self.read_timeout)
        timed_out = not read.get("done")
        if timed_out:
            log.warning("clipboard restore skipped: read timed out after %gs", self.read_timeout)
            saved, restorable = {}, False
        else:
            saved, restorable = read["saved"], read["restorable"]

        if not self._open():
            log.error("clipboard busy, dropping paste: %r", text[:80])
            return None
        try:
            # On the timed-out path the prior owner just proved unresponsive -
            # EmptyClipboard would notify it via WM_DESTROYCLIPBOARD and hang
            # the same way, so overwrite the format directly instead.
            if timed_out:
                self._write_text_direct(text)
            else:
                self._write_text(text)
        finally:
            win32clipboard.CloseClipboard()

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
                def restore():
                    for fmt, data in saved.items():
                        win32clipboard.SetClipboardData(fmt, data)
                self._replace_clipboard(restore)
            finally:
                win32clipboard.CloseClipboard()
        return sent

    def set_text(self, text):
        with self._lock:
            self._set_text(text)

    def _set_text(self, text):
        if self._open():
            try:
                self._write_text(text)
            finally:
                win32clipboard.CloseClipboard()
