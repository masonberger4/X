"""run_logos.py and draft/logos.py: site choice, icon discovery, image normalisation, the CLI."""

import io

import pytest

import run_logos
from draft import logos

HTML = """<html><head>
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="icon" href="/favicon-32.png" sizes="32x32">
<link rel="apple-touch-icon" sizes="180x180" href="/img/touch.png">
<link rel="mask-icon" href="/mask.svg">
<link rel="stylesheet" href="/a.css">
<link rel="apple-touch-icon" href="/img/touch-small.png">
</head></html>"""


def test_company_domain_from_config_or_feed_host():
    assert logos.company_domain({"domain": "https://Jnj.com/"}) == "jnj.com"
    assert logos.company_domain({"url": "https://investors.amgen.com/rss/x.xml"}) == "amgen.com"
    assert logos.company_domain({"url": "https://ir.exelixis.com/rss"}) == "exelixis.com"
    assert logos.company_domain({"url": "https://www.merck.com/media/news/feed/"}) == "merck.com"
    assert logos.company_domain({"url": "https://crisprtx.gcs-web.com/rss"}) is None
    assert (
        logos.company_domain({"url": "https://autolus.gcs-web.com/x", "domain": "autolus.com"})
        == "autolus.com"
    )
    assert logos.company_domain({}) is None


def test_icon_candidates_order_and_well_known_fallbacks():
    cands = logos.icon_candidates(HTML, "https://x.com/")
    urls = [c.url for c in cands]
    assert urls[:3] == [
        "https://x.com/img/touch.png",  # apple-touch-icon, 180
        "https://x.com/img/touch-small.png",  # apple-touch-icon, size unknown
        "https://x.com/favicon-32.png",
    ]
    assert not any(u.endswith(".svg") for u in urls)
    assert urls[-3:] == [
        "https://x.com/apple-touch-icon.png",
        "https://x.com/apple-touch-icon-precomposed.png",
        "https://x.com/favicon.ico",
    ]
    assert [c.url for c in logos.icon_candidates("", "https://y.com/")] == [
        "https://y.com" + p for p in logos.WELL_KNOWN_PATHS
    ]
    assert logos.icon_candidates("<html><link rel=icon", "https://z.com/")  # broken page


def _png(size, mode="RGB", fmt="PNG"):
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30)).save(buf, format=fmt)
    return buf.getvalue()


def test_normalise_png_resizes_converts_and_rejects_junk():
    pytest.importorskip("PIL")
    from PIL import Image

    out = logos.normalise_png(_png((900, 300), fmt="JPEG"), "image/jpeg")
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG" and img.mode == "RGBA" and img.size == (256, 85)
    ico = logos.normalise_png(_png((64, 64), fmt="ICO"), "image/x-icon")
    assert Image.open(io.BytesIO(ico)).size == (64, 64)
    assert logos.normalise_png(_png((16, 16)), "image/png") is None  # a favicon is not a logo
    assert logos.normalise_png(b"<!DOCTYPE html><html>", "text/html") is None
    assert logos.normalise_png(b"not an image", "image/png") is None


def test_cli_saves_reviews_and_skips(monkeypatch, tmp_path, caplog):
    pytest.importorskip("PIL")
    import logging

    caplog.set_level(logging.INFO, logger="run_logos")
    cfg = {
        "http": {"user_agent": "ua"},
        "branding": {
            "logos_dir": "logos",
            "companies": [{"key": "jnj", "name": "J", "domain": "jnj.com"}],
        },
        "companies": {
            "feeds": [
                {"key": "amgen", "name": "Amgen", "url": "https://investors.amgen.com/rss"},
                {"key": "hosted", "name": "H", "url": "https://h.gcs-web.com/rss"},
            ]
        },
    }
    monkeypatch.setattr("config.load_config", lambda: cfg)
    monkeypatch.setattr("panel.frozen.data_dir", lambda: tmp_path)
    fetched = []

    def get_text(url, **kw):
        fetched.append(url)
        if "amgen" in url:
            return HTML
        raise RuntimeError("down")

    def get_bytes(url, **kw):
        fetched.append(url)
        if url.endswith("touch.png"):
            return _png((120, 120)), "image/png"
        if url.endswith("favicon.ico"):
            return _png((48, 48), fmt="ICO"), "image/x-icon"
        raise RuntimeError("404")

    monkeypatch.setattr(run_logos.http, "get_text", get_text)
    monkeypatch.setattr(run_logos.http, "get_bytes", get_bytes)
    assert run_logos.main([]) == 0
    assert (tmp_path / "logos" / "amgen.png").is_file()
    assert (tmp_path / "logos" / "jnj.png").is_file()  # homepage down, favicon.ico fallback
    assert "hosted: no site" in caplog.text and "done: 2 saved, 0 skipped, 1 missing" in caplog.text
    assert "https://amgen.com/" in fetched and "https://amgen.com/img/touch.png" in fetched
    # a second run skips existing files unless --force
    caplog.clear()
    assert run_logos.main(["--only", "amgen"]) == 0
    assert "amgen: exists, skipped" in caplog.text
    n = len(fetched)
    assert run_logos.main(["--only", "amgen", "--force"]) == 0 and len(fetched) > n
    # dry run downloads nothing
    caplog.clear()
    before = len(fetched)
    assert run_logos.main(["--only", "amgen", "--dry-run"]) == 0
    assert "amgen: would fetch https://amgen.com/img/touch.png" in caplog.text
    assert fetched[before:] == ["https://amgen.com/"]  # only the homepage was read


def test_run_logos_is_a_dispatchable_cli():
    from panel.frozen import CLIS

    assert "run_logos" in CLIS
