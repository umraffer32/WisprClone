"""Model loading and the Transcriber worker thread: whisper, clean, polish, paste, log."""

import json
import logging
import os
import re
import sys
import threading
import time
import wave
from collections import Counter
from datetime import datetime
from pathlib import Path

log = logging.getLogger("wisprclone")

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
    log.warning("import av failed (%s); stubbed it so faster_whisper can load", _e)

import numpy as np
import requests
from faster_whisper import BatchedInferencePipeline, WhisperModel
from faster_whisper.vad import VadOptions, get_speech_timestamps

# The analysis scripts (analysis_tools/ and its gitignored results/ folders)
# import these names from transcribe, so the ones this file doesn't use
# itself - _FILLER, _STUTTER, _RUNAWAY_REPEAT, _GUARD_STOP, _SENT_SPLIT -
# are imported here only to keep those scripts working.
from cleanup import (_FILLER, _RUNAWAY_REPEAT, _STUTTER, _proper_nouns,
                     clean_text, join_segments)
from clipboard import Clipboard
from focus import (caret_visible, focused_editable, focused_text,
                   foreground_window, is_terminal, paste_blocked)
from polish import POLISH_PROMPT, _GUARD_STOP, _SENT_SPLIT, _SWEARS, _lost_sentence

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


def _warm_model(model):
    """Run a second of silence through the encoder. transcribe() is lazy and
    VAD short-circuits silence before the encoder runs, so a warmup must
    disable VAD and consume the generator."""
    segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32),
                               language="en", vad_filter=False)
    list(segs)


_HISTORY_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}\] (.*)$")


class Transcriber(threading.Thread):
    def __init__(self, cfg, base_dir, jobs, status):
        super().__init__(daemon=True, name="transcriber")
        self.cfg = cfg["model"]
        self.decode_opts = dict(DECODE_OPTS,
                                initial_prompt=self.cfg.get("prompt") or None)
        self.base_dir = base_dir
        self.jobs = jobs
        self.status = status
        self.clipboard = Clipboard(cfg)
        self.corrections = base_dir / cfg["files"]["corrections"]
        self.emphasis_words = base_dir / cfg["files"]["emphasis_words"]
        self.history = base_dir / cfg["files"]["history"]
        self.model = None  # WhisperModel: GPU warm-ups and the mining scripts call it directly
        self.pipe = None   # BatchedInferencePipeline over it: every dictation goes through this
        self.cpu_pipe = None
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
        _warm_model(m)
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
            self.status.device = "cpu"
        # Jobs run through faster-whisper's batched pipeline, not
        # WhisperModel.transcribe: the sequential path cuts audio into fixed
        # 30s windows, so a 30.6s clip leaves a 0.6s tail window that holds
        # no speech but still gets the prompt - Whisper fills it with
        # invented words (the "Xeon" x73 case, 2026-08-28) and then burns ~5s
        # retrying at higher temperatures. The pipeline cuts windows at VAD
        # silences instead and decodes them together. Measured on retained
        # audio 2026-09-01: clean tails on both clips that reproduced the
        # failure, long clips ~2x faster, short clips unchanged. It decodes
        # at one temperature (no fallback cascade) and without timestamps.
        self.pipe = BatchedInferencePipeline(self.model)
        if self.status.device == "cpu":
            self.cpu_pipe = self.pipe

    def _transcribe(self, audio):
        try:
            if self.status.device == "cuda":
                segs, _ = self.pipe.transcribe(audio, **self.decode_opts)
                result = list(segs)
                self.gpu_fails = 0
                return result
        except Exception:
            log.exception("GPU transcription failed, retrying on CPU")
            self.gpu_fails += 1
            if self.gpu_fails >= self.cfg["gpu_fail_latch"]:
                log.error("latching to CPU after %d GPU failures", self.gpu_fails)
                self.status.device = "cpu"
        if self.cpu_pipe is None:
            self.cpu_pipe = BatchedInferencePipeline(self._load("cpu"))
        segs, _ = self.cpu_pipe.transcribe(audio, **self.decode_opts)
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
        toward the CPU latch. Failures are swallowed - a warm is optional
        and must never flash the pill."""
        start = time.monotonic()
        while True:
            try:
                _warm_model(self.model)
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
                "think": False,
                # same num_ctx as _polish, or this warmup loads the model at
                # Ollama's 4096 default and the first real polish pays a full
                # reload mid-dictation (~4s, measured)
                "options": {"num_ctx": 8192},
            }, timeout=120)
        except Exception as e:
            log.warning("polish warmup failed (%s); first toggle dictation "
                        "may be slow or unpolished", e)

    def _polish(self, text):
        """LLM cleanup for long dictations (min_audio_s and up, either mode) -
        false starts, run-on sentences. Any failure (Ollama down, timeout, suspicious
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
                # think: Qwen 3.5 (and Gemma 4) reason by default and spend
                # the whole num_predict budget doing it, returning an empty
                # response (2026-09-01 replay). Top-level field, not an
                # option; models without a thinking mode accept and ignore it.
                "think": False,
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
            # background: don't delay readiness for a model only dictations
            # over min_audio_s need. A cold Ollama load is the 3s+ delay the
            # first of those would otherwise pay.
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
                whisper_text = join_segments(
                    (s.text for s in segments if s.no_speech_prob < 0.6),
                    _proper_nouns(self.cfg.get("prompt"), self.corrections))
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
                hwnd = foreground_window()
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
                # at all keeps the old heuristic. A miss logs the inputs
                # that produced it (CalCareers' login page still flashes
                # the pill on a good paste, 2026-08-31 - cause unknown).
                if pastable:
                    verify = None if terminal else focused_text()
                    if verify:
                        after = " ".join(verify.split())
                        at_end = after.endswith(self.last_tail)
                        newly = (field is not None
                                 and self.last_tail in after
                                 and self.last_tail not in " ".join(field.split()))
                        landed = at_end or newly
                        if not landed:
                            log.info("landed miss: field-read at_end=%s newly=%s "
                                     "pre_read=%s tail=%r after_tail=%r",
                                     at_end, newly, field is not None,
                                     self.last_tail, after[-80:])
                    else:
                        caret, edit, term = (caret_visible(), focused_editable(),
                                             is_terminal())
                        landed = caret or edit or term
                        if not landed:
                            log.info("landed miss: fallback terminal=%s verify=%r "
                                     "caret=%s editable=%s is_terminal=%s tail=%r",
                                     terminal, verify, caret, edit, term,
                                     self.last_tail)
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
    text = join_segments((s.text for s in segments if s.no_speech_prob < 0.6),
                         _proper_nouns(t.cfg.get("prompt"), t.corrections))
    print(f"transcribed in {time.monotonic() - start:.2f}s: "
          f"{clean_text(text, t.corrections, t.emphasis_words)!r}")
