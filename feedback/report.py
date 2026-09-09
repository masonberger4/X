"""Render an Analysis plus Suggestions to markdown. Pure; summarises metrics, never science.

Sections: headline, group tables, correlation table, top/bottom posts, suggestions, caveats.
"""

from __future__ import annotations

from collections.abc import Sequence

from feedback.analysis import Analysis, GroupSummary, PostRow
from feedback.suggest import Suggestion

GROUP_TITLES = {
    "source": "By source",
    "evidence_level": "By evidence level",
    "kind": "By format (single vs thread)",
    "slot": "By publishing slot",
    "edited": "Edited vs unedited",
    "topic": "By topic (title keywords from feedback/config.yaml)",
    "hour": "By hour posted (local)",
}

CAVEATS = """## Caveats

- Numbers come from the X API v2 `public_metrics` fields only: impressions, likes, reposts,
  replies, quotes and bookmarks. Profile visits, link clicks, and follows-from-post are not
  available through this endpoint.
- The Original Content Rewards programme counts "Premium impressions" (Home-timeline
  impressions from Premium users). That figure is **not exposed by the API**;
  `impression_count` is a proxy that counts every impression from every viewer.
- Snapshots are taken daily for the first days after posting and weekly after that, so
  a post's numbers keep growing until its last snapshot. Compare posts of similar age.
- Groups below the minimum post count are shown for completeness but marked small-n;
  nothing should be changed on their evidence alone. Spearman rho on fewer than ~10 posts
  is noise.
- Thread metrics are the head tweet's own numbers; reply tweets are summed separately.
- This report summarises engagement. It says nothing about the science, and nothing in it
  is medical advice.
"""


def _fmt(n: float | None, digits: int = 0) -> str:
    if n is None:
        return "-"
    return f"{n:,.{digits}f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.2f}%"


def _rho(x: float | None) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _short(s: str, n: int = 70) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|")


def headline(a: Analysis) -> str:
    fd = a.followers
    followers_now = _fmt(fd.end.followers) if fd.end else "-"
    delta = fd.delta
    if delta is None:
        delta_s = "n/a (need two follower snapshots inside the window)"
    else:
        delta_s = f"{delta:+,d} (from {fd.start.followers:,} on {fd.start.captured_on})"
    heads = a.head_total
    replies = a.reply_total
    lines = [
        f"# Feedback report {a.window_start.date()} to {a.window_end.date()}",
        "",
        f"- Posts with metrics: **{a.n_posts}** ({a.n_threads} threads)",
        f"- KPI: **{a.kpi}** (head tweets); median {_fmt(a.median_kpi)}",
        f"- Head-tweet totals: {heads.impressions:,} impressions, {heads.likes:,} likes, "
        f"{heads.reposts:,} reposts, {heads.replies:,} replies, {heads.quotes:,} quotes, "
        f"{heads.bookmarks:,} bookmarks; engagement rate {_pct(heads.engagement_rate)}",
        f"- Thread reply tweets (summed separately): {replies.impressions:,} impressions, "
        f"{replies.engagements:,} engagements",
        f"- Followers: {followers_now}; delta over window: {delta_s}",
        "",
    ]
    return "\n".join(lines)


def group_table(title: str, groups: Sequence[GroupSummary], kpi: str, min_posts: int) -> str:
    lines = [f"### {title}", ""]
    if not groups:
        lines += ["_No posts in the window._", ""]
        return "\n".join(lines)
    lines += [
        f"| group | n | median {kpi} | mean {kpi} | engagement rate | |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for g in groups:
        flag = "small-n" if g.n < min_posts else ""
        lines.append(
            f"| {_cell(g.group)} | {g.n} | {_fmt(g.median_kpi)} | {_fmt(g.mean_kpi, 1)} "
            f"| {_pct(g.engagement_rate)} | {flag} |"
        )
    lines.append("")
    return "\n".join(lines)


def correlation_table(a: Analysis, min_posts: int) -> str:
    lines = [
        f"## Score dimensions vs {a.kpi} (Spearman rho)",
        "",
        "| dimension | n | rho | |",
        "|---|---:|---:|---|",
    ]
    for c in a.correlations:
        flag = "small-n" if c.n < min_posts else ""
        lines.append(f"| {c.name} | {c.n} | {_rho(c.rho)} | {flag} |")
    lines.append("")
    return "\n".join(lines)


def post_table(title: str, rows: Sequence[PostRow], kpi: str) -> str:
    lines = [f"### {title}", ""]
    if not rows:
        lines += ["_None._", ""]
        return "\n".join(lines)
    lines += [
        f"| {kpi} | eng. rate | replies (thread) | source | kind | score | rating | title "
        "| angle |",
        "|---:|---:|---:|---|---|---:|---:|---|---|",
    ]
    for r in rows:
        thread = f"{r.reply_sum.get(kpi):,} / {r.reply_count}" if r.is_thread else "-"
        total = r.scores.get("total")
        lines.append(
            f"| {r.kpi(kpi):,} | {_pct(r.head.engagement_rate)} | {thread} | {_cell(r.source)} "
            f"| {r.kind} | {_fmt(total)} | {_fmt(r.rating)} "
            f"| {_cell(_short(r.title, 60))} | {_cell(_short(r.suggested_angle, 90))} |"
        )
    lines.append("")
    return "\n".join(lines)


def suggestions_section(suggestions: Sequence[Suggestion], min_posts: int) -> str:
    lines = ["## Suggestions (proposed; a human applies them)", ""]
    if not suggestions:
        lines += [
            f"_No suggestions. Either no group reached {min_posts} posts on both sides of a "
            "comparison, or no effect was clear-cut (median KPI ratio and rank correlation "
            "thresholds in feedback/config.yaml). This is the expected state early on._",
            "",
        ]
        return "\n".join(lines)
    for i, s in enumerate(suggestions, 1):
        lines += [
            f"{i}. **[{s.kind}]** `{s.target}`",
            f"   - {s.rationale}",
            f"   - Evidence: {s.evidence}",
        ]
    lines += [
        "",
        "Nothing above has been applied. Rubric changes go in score/rubric.py with a "
        "PROMPT_VERSION bump (run_score.py then re-scores); prefilter and cadence changes go "
        "in config.yaml; slots and post_format in publish/config.yaml; voice in draft/voice.md.",
        "",
    ]
    return "\n".join(lines)


def render(analysis: Analysis, suggestions: Sequence[Suggestion], *, min_posts: int = 5) -> str:
    parts = [headline(analysis), "## Groups", ""]
    for dim, title in GROUP_TITLES.items():
        parts.append(group_table(title, analysis.groups.get(dim, []), analysis.kpi, min_posts))
    parts.append(correlation_table(analysis, min_posts))
    parts += [
        "## Posts",
        "",
        post_table(f"Top {len(analysis.top)}", analysis.top, analysis.kpi),
        post_table(f"Bottom {len(analysis.bottom)}", analysis.bottom, analysis.kpi),
        suggestions_section(suggestions, min_posts),
        CAVEATS,
    ]
    return "\n".join(parts)
