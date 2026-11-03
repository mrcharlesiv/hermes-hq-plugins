"""Chat names that fit on one line of an iPhone screen (0.9.28).

Hermes names a new chat twice (``agent/title_generator.py``): an instant title cut from the opening message (up to 48
characters), then one from a small model asked for "3 to 7 words". Both often ran past the Sessions row on a phone
("Investigate flaky auth test on CI runner…"), so the row showed half a name. ``install`` asks the model for 2 to 4
words that fit on one line, and shortens any name either stage makes to ``LIMIT`` characters at a word break. At 17 pt
semibold, 28 characters is about 200-230 pt, the room a Sessions row's title has on a 375 pt wide iPhone beside the
face and the time.

Only the automatic names change. A name someone types (Rename, /title) is never touched: those don't go through these
functions. Hermes's own guards still run first on the model's whole answer (an answer instead of a title is still
refused, an echo of the prompt's examples still falls back), so shortening only ever trims a real title. If the pieces
this relies on are gone (a reworked Hermes), ``install`` leaves Hermes alone.
"""
import logging
import re

log = logging.getLogger("hermes-hq-push")

MARK = "_hermes_hq_short_titles"
LIMIT = 28
RULE = "- 2 to 4 words, at most 28 characters, so the name fits on one line of an iPhone screen; sentence case.\n"
# Hermes's length rule in the prompt ("- 3 to 7 words, sentence case (...).\n" as of 2026-11).
_RULE_RE = re.compile(r"^- \d+ to \d+ words[^\n]*\n", re.MULTILINE)
# Words a name shouldn't end on once its tail is cut ("Plan trip to" → "Plan trip").
_DANGLING = frozenset("a an and as at by for from in into of on or the to via vs with".split())


def shorten(title, limit: int = LIMIT, mark: str = ""):
    """``title`` cut to ``limit`` characters at a word break (``mark`` after it, counted in the limit); unchanged when it fits."""
    if not isinstance(title, str):
        return title
    if len(title) <= limit:
        return title
    room = limit - len(mark)
    words = title[:room + 1].split(" ")
    if len(words) > 1:
        words = words[:-1]  # the last piece is a cut word, or the space after a whole one
        while len(words) > 1 and words[-1].lower().strip(",.;:—-") in _DANGLING:
            words.pop()
        cut = " ".join(words)
    else:
        cut = title[:room]  # one long word: cut inside it
    return cut.rstrip(" ,.;:—-") + mark


def install() -> str:
    """Patch the title prompt and wrap both namers once per process. Returns "installed" or "absent"."""
    try:
        from agent import title_generator as titles
    except Exception:
        return "absent"
    generate, derive = getattr(titles, "generate_title", None), getattr(titles, "derive_title", None)
    template = getattr(titles, "_TITLE_PROMPT_TEMPLATE", None)
    if not callable(generate) or not callable(derive) or not isinstance(template, str):
        return "absent"
    if getattr(generate, MARK, False):
        return "installed"
    if _RULE_RE.search(template):
        titles._TITLE_PROMPT_TEMPLATE = _RULE_RE.sub(lambda _: RULE, template, count=1)
    elif "Rules:\n" in template:
        titles._TITLE_PROMPT_TEMPLATE = template.replace("Rules:\n", "Rules:\n" + RULE, 1)

    def generate_title(*args, **kwargs):
        return shorten(generate(*args, **kwargs))

    def derive_title(*args, **kwargs):
        # The instant title is a slice of the person's own words, so it keeps Hermes's "…" when it was cut.
        return shorten(derive(*args, **kwargs), mark="…")

    for wrapper, original in ((generate_title, generate), (derive_title, derive)):
        wrapper.__wrapped__ = original
        wrapper.__doc__ = original.__doc__
        setattr(wrapper, MARK, True)
    titles.generate_title, titles.derive_title = generate_title, derive_title
    return "installed"
