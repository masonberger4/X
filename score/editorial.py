"""The editor's decision: post this story or not, and the reason categories an
explanation should name.

Both raters answer the same yes/no question. `digest.py --rate` and the panel's feed
page ask the human; `score/rater.py` asks the model. A decision is stored in the
`ratings` table as its numeric form (yes = 5, no = 1) so the feedback report's
correlation against engagement, and rows rated 1-5 before the switch, keep working:
any row of 4 or more reads back as a yes.

REASON_CATEGORIES is the vocabulary for explanations. An explanation that starts with
the deciding factor clusters into rubric changes far faster than free prose, so the
same list is shown next to the note prompt (CLI and feed page) and given to the model.
"""

from __future__ import annotations

YES = "yes"
NO = "no"
DECISIONS = (YES, NO)

# Numeric form kept in ratings.rating (1-5 CHECK) so older rows stay comparable.
RATING_FOR = {YES: 5, NO: 1}
YES_THRESHOLD = 4

# (name, what to say). Order is the order they are shown in.
REASON_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("beat", "on or off the beat: CAR-T / cell therapy, engagers and bispecifics, adjacent IO"),
    ("company", "a named public company or sponsor, or none stated"),
    ("catalyst", "a dated catalyst (readout, PDUFA, conference) or nothing to watch"),
    ("evidence", "strength of the data: phase, n, randomized, preprint, preclinical"),
    ("thesis", "changes a thesis or the competitive picture, or reads across to peers"),
    ("business", "a business or investment implication, or none (guideline, review, consensus)"),
    ("coverage", "new, already covered, or stale"),
    ("hype", "overhyped relative to the data"),
)

_YES_WORDS = {"y", "yes", "post", "5", "4"}
_NO_WORDS = {"n", "no", "skip", "pass", "1", "2", "3"}


def parse_decision(raw: str | None) -> str:
    """'yes'/'no' (or y/n, or the old 1-5 numbers). Anything else is an error."""
    value = str(raw or "").strip().lower()
    if value in _YES_WORDS:
        return YES
    if value in _NO_WORDS:
        return NO
    raise ValueError("decision must be yes or no")


def rating_for(decision: str) -> int:
    return RATING_FOR[parse_decision(decision)]


def decision_of(rating: int | None) -> str | None:
    """The decision a stored rating stands for; None when there is no rating."""
    if rating is None:
        return None
    return YES if int(rating) >= YES_THRESHOLD else NO


def reasons_text(indent: str = "  ") -> str:
    """The reason-category box, one line per category, for the terminal and the prompt."""
    lines = ["Start the explanation with the deciding factor. Reason categories:"]
    lines += [f"{indent}{name}: {what}" for name, what in REASON_CATEGORIES]
    return "\n".join(lines)
