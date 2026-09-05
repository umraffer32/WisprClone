"""Whisper's text after decoding: the chunk join and the regex cleanup layer."""

import logging
import re
from pathlib import Path

log = logging.getLogger("wisprclone")

# Guards on both sides so "uh-huh" survives. The surrounding commas exist
# because of the filler pause, so they go with it ("should, uh, remove" ->
# "should remove").
_FILLER = re.compile(r",?\s*(?<![\w-])(?:um+|uh+|erm|hmm+)(?![\w-]),?\s*", re.IGNORECASE)
# Unlike um/uh, "you know" is also a real phrase ("do you know..."), so it's
# only stripped when a comma marks it as the spoken pause ("the store, you
# know, and milk" -> "the store and milk"). Bare "you know" with no comma on
# either side is left alone - misses some filler uses, but a false strip
# ("do you know" -> "do") is worse than a miss. A comma before it isn't
# enough on its own when a question word follows: "we're good, you know what
# I mean?" is the fixed phrase, not the filler (pasted as "good what I mean?"
# on 2026-09-01), so that form only strips with a comma after it too.
_YOU_KNOW = re.compile(
    r",\s*you know\s*,\s*"
    r"|,\s*you know\b(?!\s+(?:what|how|that|if|where|when|why|who|the|this|it|i)\b)\s*"
    r"|(?<![\w-])you know\s*,",
    re.IGNORECASE)
# Collapses an immediate stutter ("I I think", "the the box" -> "I think",
# "the box"). Keeps the first occurrence's own casing via the backreference.
# No comma allowed between repeats on purpose: a comma is Whisper's own
# signal of a spoken pause, which is how deliberate emphasis ("very, very
# important") differs from a real stutter (words run together, no pause).
# Residual trade-off: fast, no-pause emphasis ("very very important") still
# collapses, since there's no punctuation to tell it apart from a stutter.
# [\w'] rather than \w so contractions count: "let's let's work" sailed
# through the old pattern (2026-09-01 log). Same class in _RUNAWAY_REPEAT.
_STUTTER = re.compile(r"\b([\w']+)(?:\s+\1\b)+", re.IGNORECASE)
# Whisper occasionally hallucinates one word repeated dozens of times with a
# comma after each ("Xeon, Xeon, Xeon, ..." x73, 2026-08-28) - the commas make
# it read as emphasis to _STUTTER, so it sailed through untouched. This
# collapses 4+ exact repeats of the same word, comma-separated or not, down to
# the first occurrence (keeping its casing via the backreference). Kept
# separate from _STUTTER because a real spoken triple ("no, no, no",
# 2026-08-25) is a confirmed legitimate case that must not be touched: the
# whole log corpus tops out at 3x for real speech, and the only hallucination
# seen was 73x, with nothing in between - so 4 is the threshold.
_RUNAWAY_REPEAT = re.compile(r"\b([\w']+)\b(?:[\s,]+\1\b){3,}", re.IGNORECASE)


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
# Sentence boundary shared by the two patterns below: end punctuation plus
# whitespace, except the "." of an ellipsis (Whisper's trailing-off marker:
# "probably... doesn't" is mid-sentence) or of a common abbreviation ("1050
# a.m. this morning" pasted as "a.m. This morning", 2026-09-01).
_SENT_END = r"(?<!\.\.)(?<![apAP]\.[mM])(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)[.!?]\s+"
_LEADING_AND = re.compile(rf"(^|{_SENT_END})and\b,?\s*", re.IGNORECASE)
_SENTENCE_START = re.compile(rf"(^|{_SENT_END})([a-z])")


def _proper_nouns(prompt, corrections_path):
    """Capitalized words from the Whisper prompt and corrections.txt targets:
    the names join_segments must not lowercase."""
    words = set(re.findall(r"[A-Z][A-Za-z']*", prompt or ""))
    try:
        for line in Path(corrections_path).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                words.update(re.findall(r"[A-Z][A-Za-z']*", line.split("=", 1)[1]))
    except OSError:
        pass
    return words


def join_segments(segments, proper):
    """Join the batched pipeline's chunks into one text. Each chunk is decoded
    on its own, so a chunk cut mid-sentence ends without punctuation (often
    with a trailing "...") and the next starts with a capital as if it were
    a new sentence. In a 586-join replay every such join was a continuation
    (2026-09-02), so drop the cut ellipsis and lowercase the next chunk's
    first word, except I/I'm-style words, proper nouns, and anything with a
    capital past the first letter (YouTube, GPU)."""
    texts = [t.strip() for t in segments if t.strip()]
    # a word Whisper capitalized mid-sentence anywhere in this dictation is a
    # name too (Windows, Thursday), whether or not the prompt knows it
    proper = proper | {w for t in texts
                       for w in re.findall(r"(?<=[^.!?] )[A-Z][a-z']+", t)}
    out = []
    for text in texts:
        if out:
            prev = re.sub(r"(?:\.\.\.|…)$", "", out[-1]).rstrip()
            if not re.search(r"[.!?]['\")]*$", prev):
                out[-1] = prev
                m = re.match(r"([A-Z][a-z']*)\b", text)
                word = m.group(1).removesuffix("'s") if m else "I"
                if word != "I" and not word.startswith("I'") and word not in proper:
                    text = word[0].lower() + text[1:]
        out.append(text)
    return " ".join(out)


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
    # only punctuation that ends a token: "the .venv file" must not become
    # "the.venv file" (2026-09-01)
    text = re.sub(r"\s+([.,!?;])(?=\s|$)", r"\1", text)
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
