"""Spelled-out letters, recovered from what the recogniser wrote.

On a real call the caller spelled "S, A, N, J" and NVIDIA's recogniser wrote
"Yes, A and J": letters sound like words (S -> "yes", N -> "and"). This finds
runs that look like spelling and adds the letters to the transcript before the
model sees it:

    "Yes, A and J"  ->  "Yes, A and J (spelled: S A N J)"

A run must have at least three letter-like tokens, two of them plain single
letters, so "yes, and you" in normal speech is left alone.
"""

import re

# Words a recogniser writes for a spoken letter.
LETTER_WORDS = {
    "ay": "a", "eh": "a",
    "bee": "b", "be": "b",
    "see": "c", "sea": "c", "si": "c",
    "dee": "d",
    "ee": "e",
    "eff": "f", "ef": "f",
    "gee": "g", "ji": "g",
    "aitch": "h", "edge": "h",
    "eye": "i", "aye": "i",
    "jay": "j", "je": "j",
    "kay": "k",
    "el": "l", "ell": "l",
    "em": "m",
    "en": "n", "and": "n", "an": "n",
    "oh": "o", "owe": "o",
    "pee": "p", "pea": "p",
    "cue": "q", "queue": "q",
    "are": "r", "ar": "r",
    "es": "s", "ess": "s", "yes": "s",
    "tee": "t", "tea": "t",
    "you": "u",
    "vee": "v",
    "ex": "x",
    "why": "y", "wai": "y",
    "zed": "z", "zee": "z",
}
MIN_RUN = 3
MIN_PLAIN_LETTERS = 2


def spelled_runs(text: str) -> list[str]:
    """Letter runs in `text`, e.g. "Yes, A and J" -> ["sanj"]."""
    tokens = re.findall(r"[a-z]+", (text or "").lower())
    runs, run, plain = [], [], 0

    def close():
        if len(run) >= MIN_RUN and plain >= MIN_PLAIN_LETTERS:
            runs.append("".join(run))

    for tok in tokens:
        letter = tok if len(tok) == 1 else LETTER_WORDS.get(tok)
        if letter:
            run.append(letter)
            plain += len(tok) == 1
        else:
            close()
            run, plain = [], 0
    close()
    return runs


def annotate(text: str) -> str:
    runs = spelled_runs(text)
    if not runs:
        return text
    spelled = "; ".join(" ".join(r.upper()) for r in runs)
    return f"{text} (spelled: {spelled})"


def install_spelling(user_aggregator) -> None:
    """Annotate the caller's whole turn as it goes into the conversation.

    Done per turn, not per transcript: NVIDIA sends a spelled name as one
    final per letter ("S", "U", "N", "J"), so no single piece holds a run.
    The user aggregator joins a turn's pieces with aggregation_string() just
    before adding it to the context; wrapping that one method sees the whole
    turn. The call record gets the same text (on_user_turn_stopped).
    """
    joined = user_aggregator.aggregation_string

    def aggregation_string() -> str:
        return annotate(joined())

    user_aggregator.aggregation_string = aggregation_string
