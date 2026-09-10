"""Links to other websites open in a new tab, so the operator never loses the page
they were on (the desktop window has no back button at all)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIRS = [ROOT / "panel" / "templates", ROOT / "approval_queue" / "templates"]

# An href that is a full URL or is filled from a record (an item or draft url).
OFFSITE = re.compile(r'<a\s[^>]*href="(?:https?://|\{\{)[^>]*>')


def test_every_offsite_link_opens_in_a_new_tab():
    seen = 0
    for d in TEMPLATE_DIRS:
        for tpl in sorted(d.glob("*.html")):
            for tag in OFFSITE.findall(tpl.read_text(encoding="utf-8")):
                seen += 1
                assert 'target="_blank"' in tag, f"{tpl.name}: {tag}"
                assert "noopener" in tag, f"{tpl.name}: {tag}"
    assert seen >= 5, "the templates carry the source, doi, tweet and claim links"
