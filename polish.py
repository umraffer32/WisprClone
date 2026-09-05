"""The LLM polish prompt and the guards that reject a bad polish output."""

import re

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
# 2026-08-25), so Transcriber._polish backs the rule with a count check
# against this list. Inflections are spelled out because \b can't connect
# "fucking" to "fuck". Internal sanity list, not a config knob. Matched
# against lowercased text, so no IGNORECASE needed.
_SWEARS = re.compile(
    r"\b(?:fuck(?:ing|ed|er|ers)?|motherfuck(?:ing|er|ers)?|shit(?:s|ty)?|"
    r"bullshit|damn|dammit|goddamn(?:it)?|ass(?:es|hole|holes)?|"
    r"bitch(?:es|y)?|bastards?|crap(?:py)?|piss(?:ed|ing)?|dicks?|cocks?|"
    r"cunts?|pricks?|whores?|sluts?)\b")

# The prompt's never-drop-sentences rule is the one qwen2.5 breaks most
# quietly: a whole sentence vanishing from a multi-sentence dictation moves
# the length ratio too little to trip min_ratio and needn't be a question
# (405-case replay 2026-08-27: 3 whole-sentence drops, none caught by the
# other guards). _lost_sentence backs that rule the way _SWEARS backs
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
