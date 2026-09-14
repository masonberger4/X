"""X mentions and hashtags: the accounts a post must @-mention and the names it must #-tag.

The rule (HARD RULES 11 in draft/prompt.py, mirrored here in code): a post that names an
account we know the X handle of (the journal or society that published the source, the
company whose release it is, a regulator) writes it as @handle, and a formal drug name
(#Trastuzumab Deruxtecan, #cilta-cel) or trial name (#DESTINY-Lung02, #KEYNOTE-189) is
written as a hashtag. Handles are never guessed: they come from `config.yaml` only (`x:` on
a `companies.feeds` or `branding.companies` entry, and the `mentions:` list for journals,
societies and regulators), so a company without an `x:` is simply written by name.

Pure: no network, no database. `load_handles` reads the root config dict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://\S+")
# A formal trial name: an upper-case word of five or more letters, optionally a word after
# a hyphen, then one to three digits (KEYNOTE-189, DESTINY-Lung02, CARTITUDE-1,
# CheckMate-227, TROPION-Lung01). Four-letter targets such as CTLA-4 stay clear of it.
_TRIAL_RE = re.compile(
    r"(?<![#@$\w/.-])([A-Z][A-Za-z]{4,}(?:-[A-Za-z]{1,12})?-?\d{1,3}[A-Za-z]?)(?![\w-])"
)
# A generic (INN) drug name by its stem: antibodies, kinase inhibitors, ADC payloads and
# CAR-T short names (cilta-cel, ide-cel). "#Trastuzumab Deruxtecan" tags the first word.
_INN_SUFFIXES = (
    "mab",
    "nib",
    "ciclib",
    "parib",
    "lisib",
    "zomib",
    "lutamide",
    "leucel",
    "cabtagene",
    "tecan",
    "degib",
    "vedotin",
    "govitecan",
)
_INN_RE = re.compile(
    r"(?<![#@$\w-])([A-Za-z]{3,}(?:" + "|".join(_INN_SUFFIXES) + r")|[A-Za-z]{2,}-cel)(?![\w-])"
)
_NOT_TRIALS = frozenset({"covid"})


@dataclass(frozen=True)
class Handle:
    """One X account we may @-mention: its handle and the names a post may write it as."""

    handle: str
    name: str
    aliases: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    match_names: bool = True  # False: only the URL host identifies it (Nature, Blood)

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def named_in(self, text: str) -> bool:
        """True when `text` writes this account by any of its names as whole words, in the
        configured capitalisation (a proper noun: "Merck", not "merck"). Never for an
        account with `match_names: false`, whose name is an ordinary word."""
        if not self.match_names:
            return False
        return any(_name_re(n).search(text) for n in self.names if n)

    def mentioned_in(self, text: str) -> bool:
        pat = rf"(?<!\w)@{re.escape(self.handle)}(?!\w)"
        return re.search(pat, text, re.IGNORECASE) is not None

    def owns_host(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.domains if d)


def _name_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w@#$]){re.escape(name)}(?![\w])")


def _clean_handle(value: Any) -> str:
    return str(value or "").strip().lstrip("@")


def load_handles(root_cfg: dict[str, Any] | None) -> list[Handle]:
    """Every account the root config gives a handle to: `x:` on `companies.feeds` and
    `branding.companies` entries (the feed URL host and `domain:` are its domains), plus
    each `mentions:` entry (`{name, handle, aliases, domains, match_names}`). Entries
    without a handle are skipped; the first entry wins for a repeated handle."""
    cfg = root_cfg or {}
    out: list[Handle] = []
    seen: set[str] = set()

    def add(h: Handle) -> None:
        key = h.handle.lower()
        if h.handle and h.name and key not in seen:
            seen.add(key)
            out.append(h)

    entries = [e for e in ((cfg.get("companies") or {}).get("feeds") or []) if isinstance(e, dict)]
    entries += [
        e for e in ((cfg.get("branding") or {}).get("companies") or []) if isinstance(e, dict)
    ]
    for e in entries:
        domains = []
        if e.get("domain"):
            domains.append(str(e["domain"]).strip().lower())
        host = (urlparse(str(e.get("url") or "")).hostname or "").lower()
        if host:
            domains.append(host)
        add(
            Handle(
                handle=_clean_handle(e.get("x")),
                name=str(e.get("name") or "").strip(),
                aliases=tuple(str(a).strip() for a in (e.get("aliases") or []) if str(a).strip()),
                domains=tuple(domains),
            )
        )
    for e in cfg.get("mentions") or []:
        if not isinstance(e, dict):
            continue
        add(
            Handle(
                handle=_clean_handle(e.get("handle")),
                name=str(e.get("name") or "").strip(),
                aliases=tuple(str(a).strip() for a in (e.get("aliases") or []) if str(a).strip()),
                domains=tuple(
                    str(d).strip().lower() for d in (e.get("domains") or []) if str(d).strip()
                ),
                match_names=bool(e.get("match_names", True)),
            )
        )
    return out


def relevant_handles(
    handles: list[Handle], *, source_text: str, url: str = "", source: str = ""
) -> list[Handle]:
    """The accounts a story may mention: those the source text names, the one that owns
    the source URL's host, and the one whose key the step 1 source name carries
    (`company_merck`)."""
    src = (source or "").lower()
    out = []
    for h in handles:
        if (
            h.named_in(source_text)
            or (url and h.owns_host(url))
            or (src.startswith("company_") and h.named_in(src[len("company_") :].replace("_", " ")))
        ):
            out.append(h)
    return out


def handles_block(handles: list[Handle]) -> str:
    """The user-prompt lines listing the handles the model may use, empty when none."""
    if not handles:
        return ""
    lines = ["X HANDLES (write these accounts as @handle when a post names them):"]
    for h in handles:
        names = ", ".join(h.names)
        lines.append(f"- @{h.handle} = {names}")
    return "\n".join(lines)


def trial_names(text: str) -> list[str]:
    """Formal trial names written without a hashtag, in order, without repeats."""
    text = _URL_RE.sub(" ", text)
    found = []
    for m in _TRIAL_RE.finditer(text):
        name = m.group(1)
        if name.split("-")[0].lower() in _NOT_TRIALS or name in found:
            continue
        found.append(name)
    return found


def drug_names(text: str) -> list[str]:
    """Generic drug names (by INN stem) written without a hashtag, without repeats. A
    second stem word right after a first ("trastuzumab deruxtecan", "#Trastuzumab
    Deruxtecan") belongs to the same name: only the first word carries the #."""
    text = _URL_RE.sub(" ", text)
    found = []
    for m in _INN_RE.finditer(text):
        before = text[: m.start()].rstrip()
        prev = before.split()[-1] if before and text[m.start() - 1].isspace() else ""
        if prev and (_INN_RE.fullmatch(prev) or _INN_RE.fullmatch(prev.lstrip("#"))):
            continue
        name = m.group(1)
        if name not in found:
            found.append(name)
    return found


def tag_problems(text: str, handles: list[Handle] | None = None) -> list[str]:
    """Why one post breaks the mention/hashtag rule, worded for the retry prompt. Empty
    when it passes. `handles` are the accounts the post is allowed to know about (the
    relevant ones for the story); a name written without its @handle is a violation, as is
    a trial or drug name without its #."""
    problems: list[str] = []
    for h in handles or []:
        if h.named_in(text) and not h.mentioned_in(text):
            problems.append(f"names {h.name} without its handle @{h.handle}")
    trials = trial_names(text)
    if trials:
        problems.append(
            "trial name(s) without a hashtag: " + ", ".join(f"{t} -> #{t}" for t in trials)
        )
    drugs = drug_names(text)
    if drugs:
        problems.append(
            "drug name(s) without a hashtag: " + ", ".join(f"{d} -> #{d}" for d in drugs)
        )
    return problems
