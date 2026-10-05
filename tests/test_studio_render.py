"""Card drawing (studio/render.py).

Three layers: the pure helpers; render_card's plumbing with a stand-in browser (a local
script that records how it was called and answers as the test asks); and the real layout
checker in headless Chromium, skipped where no browser can draw. Nothing here reaches the
network: the real browser runs with it blocked, and the one server a test starts listens
on loopback only, to prove that no request gets to it.
"""

from __future__ import annotations

import concurrent.futures
import functools
import http.server
import io
import json
import logging
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from studio import render

LONG = "This sentence is far too long for a small box"
NOT_RUN = (
    "the layout check did not run (unclosed markup, a comment say, may hide the end of the "
    "page); fix the HTML so it loads cleanly"
)


def rail(h: int) -> str:
    """A thin painted line down the card's right side. The test cards hold one or two
    things on purpose; the rail keeps the empty-band check quiet about the space around
    them, so each card reports only the problem it is about."""
    return (
        f'<i style="position:absolute;right:30px;top:30px;width:2px;height:{h - 60}px;'
        'background:#252c37"></i>'
    )


def page(
    body: str,
    *,
    size: tuple[int, int] = render.DEFAULT_SIZE,
    head: str = "",
    filled: bool = True,
) -> str:
    """A card the way the card brief asks for one: the page sized to the card. `filled`
    adds the rail (see `rail`); a card about empty space leaves it out."""
    w, h = size
    meta = '<meta name="card-size" content="1600x900">' if size == render.WIDE_SIZE else ""
    return (
        f'<!doctype html><html><head><meta charset="utf-8">{meta}<style>html,body{{margin:0;'
        f"width:{w}px;height:{h}px;background:#0d1117;color:#fff;font-family:Inter}}</style>"
        f"{head}</head><body>{body}{rail(h) if filled else ''}</body></html>"
    )


def box(x: int, y: int, inner: str, style: str = "") -> str:
    return f'<div style="position:absolute;left:{x}px;top:{y}px;{style}">{inner}</div>'


def png_header(w: int, h: int) -> bytes:
    """The first 24 bytes of a PNG, all that render.png_size reads."""
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big")


def write(folder: Path, text: str, name: str = "card_1.html") -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- pure helpers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "head, size",
    [
        ("", (1080, 1350)),
        ('<meta name="card-size" content="1600x900">', (1600, 900)),
        ("<META NAME='card-size' CONTENT='1600 X 900'>", (1600, 900)),
        ('<meta name="card-size" content="1080x1350">', (1080, 1350)),
        ('<meta name="card-size" content="1920x1080">', (1080, 1350)),
        ('<meta name="card-size" content="900x1600">', (1080, 1350)),
        ('<meta name="card-size" content="1600x9000">', (1080, 1350)),
        ('<meta name="card-size" content="wide">', (1080, 1350)),
        ('<meta name="viewport" content="1600x900">', (1080, 1350)),
        # valid HTML in any attribute order, with other attributes or the self-closing slash
        ('<meta content="1600x900" name="card-size">', (1600, 900)),
        ('<meta id="size" name="card-size" data-x="1" content="1600x900" />', (1600, 900)),
        ('<meta charset="utf-8"><meta name="card-size" content="1600x900">', (1600, 900)),
        (
            '<meta name="viewport" content="width=1080"><meta content="1600x900" name="card-size">',
            (1600, 900),
        ),
        ('<meta name="card-size">', (1080, 1350)),
    ],
)
def test_only_the_two_house_sizes_are_accepted(head, size):
    assert render.card_size(f"<html><head>{head}</head><body>x</body></html>") == size


def test_the_house_fonts_ship_and_each_gets_a_rule_naming_its_file():
    rules = re.findall(r"@font-face\{[^}]*\}", render.font_css())
    assert len(rules) == len(render.FONTS) == 5
    assert {family for family, *_ in render.FONTS} == {"Inter", "IBM Plex Mono"}
    for rule, (family, filename, weight, urange) in zip(rules, render.FONTS, strict=True):
        path = render.FONT_DIR / filename
        assert path.read_bytes()[:4] == b"wOF2"  # a real woff2 file, not a stub
        assert f"font-family:'{family}'" in rule and f"font-weight:{weight}" in rule
        assert f"src:url('{path.as_uri()}') format('woff2')" in rule
        assert "font-display:block" in rule  # text never paints in a fallback font first
        assert ("unicode-range:" in rule) == bool(urange)


def test_a_missing_font_file_is_left_out(tmp_path):
    shutil.copy(render.FONT_DIR / "ibm-plex-mono-latin-600-normal.woff2", tmp_path)
    css = render.font_css(tmp_path)
    assert css.count("@font-face") == 1
    assert (tmp_path / "ibm-plex-mono-latin-600-normal.woff2").as_uri() in css
    assert render.font_css(tmp_path / "missing") == ""


ID = render.REPORT_ID


@pytest.mark.parametrize(
    "dom, found",
    [
        (f'<html><body><p>card</p><pre id="{ID}">[]</pre></body></html>', []),
        (
            f'<pre id="{ID}">["text is cut off in its box: \\"A &amp; B &lt;1%&gt;\\""]</pre>',
            ['text is cut off in its box: "A & B <1%>"'],
        ),
        (f'<pre id="{ID}">[\n"one",\n"two"\n]</pre>', ["one", "two"]),
        ("<html><body><p>the checker never ran</p></body></html>", None),
        (f'<pre id="{ID}">["cut off</pre>', None),
        (f'<pre id="{ID}">{{"problems": []}}</pre>', None),
        ("", None),
    ],
)
def test_parse_report_tells_an_empty_report_from_none(dom, found):
    assert render.parse_report(dom) == found


def test_browser_argv_blocks_the_network_and_sizes_the_window(monkeypatch):
    monkeypatch.setattr(render, "_needs_no_sandbox", lambda: False)
    argv = render.browser_argv("/opt/chrome", (1600, 900), ["--lang=en-US"])
    assert argv[0] == "/opt/chrome" and argv[-1] == "--lang=en-US"
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in argv  # no name resolves
    assert "--proxy-server=127.0.0.1:9" in argv  # and every request goes nowhere
    assert "--window-size=1600,900" in argv
    assert "--force-device-scale-factor=2" in argv and render.SCALE == 2
    assert "--headless=new" in argv and "--no-sandbox" not in argv
    monkeypatch.setattr(render, "_needs_no_sandbox", lambda: True)
    assert "--no-sandbox" in render.browser_argv("/opt/chrome", render.DEFAULT_SIZE)


@pytest.mark.parametrize(
    "platform, euid, needed",
    [("linux", 0, True), ("linux", 1000, False), ("darwin", 0, False)],
)
def test_the_sandbox_is_dropped_only_for_root_on_linux(monkeypatch, platform, euid, needed):
    monkeypatch.setattr(render.sys, "platform", platform)
    monkeypatch.setattr(render.os, "geteuid", lambda: euid, raising=False)
    assert render._needs_no_sandbox() is needed


def _exe(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def no_browsers(tmp_path, monkeypatch):
    """A machine with no browser anywhere: empty PATH, no install locations, no env."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("STUDIO_BROWSER", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(render, "_WINDOWS_CANDIDATES", ())
    monkeypatch.setattr(render, "_MAC_CANDIDATES", ())
    return empty


def test_studio_browser_wins_over_the_configured_path(no_browsers, tmp_path, monkeypatch):
    env = _exe(tmp_path / "a", "my-chrome")
    configured = _exe(tmp_path / "b", "other-chrome")
    monkeypatch.setenv("STUDIO_BROWSER", f"  {env}  ")
    assert render.find_browser(str(configured)) == str(env)
    monkeypatch.setenv("STUDIO_BROWSER", "   ")  # blank is no choice at all
    assert render.find_browser(str(configured)) == str(configured)


@pytest.mark.skipif(sys.platform == "win32", reason="PATH lookup of extensionless names")
def test_an_explicit_browser_may_be_a_name_on_path(no_browsers, monkeypatch):
    found = _exe(no_browsers, "edge-dev")
    monkeypatch.setenv("STUDIO_BROWSER", "edge-dev")
    assert render.find_browser() == str(found)


def test_an_explicit_browser_that_is_missing_raises_instead_of_falling_back(
    no_browsers, tmp_path, monkeypatch
):
    _exe(no_browsers, "chromium")  # a usable browser exists, but was not the one named
    monkeypatch.setenv("STUDIO_BROWSER", str(tmp_path / "gone" / "chrome"))
    with pytest.raises(render.RenderError, match="gone"):
        render.find_browser()
    monkeypatch.delenv("STUDIO_BROWSER")
    with pytest.raises(render.RenderError, match="nope-browser"):
        render.find_browser("nope-browser")


@pytest.mark.skipif(sys.platform == "win32", reason="PATH lookup of extensionless names")
def test_path_names_are_tried_in_order(no_browsers):
    chromium = _exe(no_browsers, "chromium")
    assert render.find_browser() == str(chromium)
    chrome = _exe(no_browsers, "google-chrome")  # earlier in the list than chromium
    assert render.find_browser("") == str(chrome)


@pytest.mark.skipif(sys.platform == "win32", reason="PATH lookup of extensionless names")
def test_on_path_chrome_comes_before_edge(no_browsers):
    # Edge's Linux package often ships a sandbox helper that is not set up (CI runners,
    # containers) and then aborts; Windows finds Edge by its install path first anyway.
    edge = _exe(no_browsers, "msedge")
    assert render.find_browser() == str(edge)  # the only one: it is used
    chrome = _exe(no_browsers, "google-chrome")
    assert render.find_browser("") == str(chrome)


def test_an_install_location_comes_before_path(no_browsers, tmp_path, monkeypatch):
    app = _exe(tmp_path / "Applications", "Google Chrome")
    _exe(no_browsers, "chromium")
    monkeypatch.setattr(render.sys, "platform", "darwin")
    monkeypatch.setattr(render, "_MAC_CANDIDATES", (str(tmp_path / "missing"), str(app)))
    assert render.find_browser() == str(app)


def test_the_newest_playwright_chromium_is_the_last_resort(no_browsers, tmp_path, monkeypatch):
    pw = tmp_path / "pw"
    _exe(pw / "chromium-1100" / "chrome-linux", "chrome")
    newest = _exe(pw / "chromium-1194" / "chrome-linux", "chrome")
    (pw / "chromium-1200" / "chrome-linux").mkdir(parents=True)  # an install with no binary
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(pw))
    assert render.find_browser() == str(newest)


def test_the_playwright_revision_is_compared_as_a_number(no_browsers, tmp_path, monkeypatch):
    pw = tmp_path / "pw"
    _exe(pw / "chromium-939" / "chrome-linux", "chrome")  # sorts after 1194 as text
    newest = _exe(pw / "chromium-1194" / "chrome-linux", "chrome")
    _exe(pw / "chromium-beta" / "chrome-linux", "chrome")  # no number: tried last
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(pw))
    assert render.find_browser() == str(newest)


def test_no_browser_anywhere_raises(no_browsers):
    with pytest.raises(render.RenderError, match="no Chromium-family browser found"):
        render.find_browser()


def test_png_size_reads_the_header_and_nothing_else(tmp_path):
    good = tmp_path / "a.png"
    good.write_bytes(png_header(2160, 2700) + b"rest of the file")
    assert render.png_size(good) == (2160, 2700)
    short = tmp_path / "b.png"
    short.write_bytes(png_header(2160, 2700)[:20])
    assert render.png_size(short) is None
    jpeg = tmp_path / "c.png"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0" + bytes(40))
    assert render.png_size(jpeg) is None
    assert render.png_size(tmp_path / "missing.png") is None


def test_a_result_is_ok_only_without_problems(tmp_path):
    assert render.RenderResult(tmp_path, render.DEFAULT_SIZE).ok
    assert not render.RenderResult(tmp_path, render.DEFAULT_SIZE, ["text overlaps"]).ok


# ---- render_card with a stand-in browser ---------------------------------------------------

STAND_IN = r"""
import json, os, sys, time
from pathlib import Path
from urllib.parse import unquote, urlparse

args = sys.argv[1:]
page = Path(unquote(urlparse(args[-1]).path)).read_text(encoding="utf-8")
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"args": args, "page": page}) + "\n")
time.sleep(float(os.environ.get("FAKE_SLEEP") or 0))
window = [int(n) for a in args if a.startswith("--window-size=") for n in a[14:].split(",")]
if "--dump-dom" in args:
    report = os.environ.get("FAKE_REPORT", "[]")
    viewport = ""
    if os.environ.get("FAKE_CHROME"):  # what the browser keeps of its window: "dw,dh"
        dw, dh = (int(n) for n in os.environ["FAKE_CHROME"].split(","))
        seen = os.environ.get("FAKE_VIEWPORT") or f"{window[0] - dw},{window[1] - dh}"
        viewport = '<pre id="' + VIEWPORT_ID + '">[' + seen + "]</pre>"
    if report != "none":
        text = report.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        print('<html><body><pre id="' + REPORT_ID + '">' + text + "</pre>" + viewport)
        print("</body></html>")
for arg in args:
    if arg.startswith("--screenshot="):
        size = os.environ.get("FAKE_PNG", "2160x2700")
        if size == "none":
            print("starting\nERROR: could not write the screenshot", file=sys.stderr)
            sys.exit(3)
        if size == "window":  # what a real browser draws: the whole window, at 2x
            w, h = 2 * window[0], 2 * window[1]
        else:
            w, h = (int(n) for n in size.split("x"))
        head = b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
        ihdr = w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
        if os.environ.get("FAKE_PNG_FULL"):  # a whole, valid picture: black, RGB
            import zlib
            def chunk(kind, body):
                crc = zlib.crc32(kind + body).to_bytes(4, "big")
                return len(body).to_bytes(4, "big") + kind + body + crc
            rows = (b"\x00" + b"\x00" * 3 * w) * h
            data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            data += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
        else:
            data = head + w.to_bytes(4, "big") + h.to_bytes(4, "big")
        Path(arg.split("=", 1)[1]).write_bytes(data)
"""


@dataclass
class StandIn:
    path: str
    log: Path

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def stand_in(tmp_path, monkeypatch) -> StandIn:
    if sys.platform == "win32":
        pytest.skip("the stand-in browser is a script run through its #! line")
    exe = tmp_path / "stand-in-browser"
    exe.write_text(
        f"#!{sys.executable}\nREPORT_ID = {render.REPORT_ID!r}\n"
        f"VIEWPORT_ID = {render.VIEWPORT_ID!r}\n{STAND_IN}",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    log = tmp_path / "calls.ndjson"
    monkeypatch.setenv("FAKE_LOG", str(log))
    for name in (
        "FAKE_REPORT",
        "FAKE_PNG",
        "FAKE_SLEEP",
        "FAKE_CHROME",
        "FAKE_VIEWPORT",
        "FAKE_PNG_FULL",
    ):
        monkeypatch.delenv(name, raising=False)
    return StandIn(str(exe), log)


def test_a_card_is_checked_then_drawn_by_two_offline_runs_at_its_size(
    stand_in, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_PNG", "3200x1800")
    html = write(tmp_path, page(box(60, 60, "A wide timeline"), size=render.WIDE_SIZE))
    png = tmp_path / "out" / "card_1.png"  # its folder is made on the way
    result = render.render_card(html, png, browser=stand_in.path)
    check, shot = stand_in.calls()
    assert "--dump-dom" in check["args"] and render.REPORT_ID in check["page"]
    assert shot["args"][-2] == f"--screenshot={png.resolve()}"
    assert render.REPORT_ID not in shot["page"]  # the picture is of the card alone
    for call in (check, shot):
        args = call["args"]
        assert "--host-resolver-rules=MAP * ~NOTFOUND" in args
        assert "--proxy-server=127.0.0.1:9" in args
        assert "--window-size=1600,900" in args and "--force-device-scale-factor=2" in args
        [profile] = [a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")]
        assert not Path(profile).parent.exists()  # a fresh profile per card, gone afterwards
    assert result == render.RenderResult(png=png.resolve(), size=render.WIDE_SIZE, problems=[])
    assert render.png_size(result.png) == (3200, 1800)


def _window(call: dict) -> str:
    [size] = [a.split("=", 1)[1] for a in call["args"] if a.startswith("--window-size=")]
    return size


def test_a_browser_that_keeps_part_of_its_window_is_given_a_taller_one(
    stand_in, tmp_path, monkeypatch
):
    # New headless Chromium keeps 87 px of hidden toolbar: a 1080x1350 window gives the
    # page 1080x1263, and the screenshot would lose the card's footer.
    monkeypatch.setenv("FAKE_CHROME", "0,87")
    monkeypatch.setenv("FAKE_PNG", "window")
    monkeypatch.setenv("FAKE_PNG_FULL", "1")
    html = write(tmp_path, page(box(60, 60, "A headline")))
    result = render.render_card(html, tmp_path / "card_1.png", browser=stand_in.path)
    first, second, shot = stand_in.calls()
    assert [_window(c) for c in (first, second, shot)] == ["1080,1350", "1080,1437", "1080,1437"]
    assert "--dump-dom" in first["args"] and "--dump-dom" in second["args"]
    assert render.png_size(result.png) == (2160, 2700)  # cut back to the card
    assert result.problems == []
    # The next card in the same run starts from the measured window: one check, one shot.
    again = render.render_card(html, tmp_path / "card_2.png", browser=stand_in.path)
    assert [_window(c) for c in stand_in.calls()[3:]] == ["1080,1437", "1080,1437"]
    assert render.png_size(again.png) == (2160, 2700)


def test_a_viewport_that_never_matches_is_checked_twice_then_drawn(
    stand_in, tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("FAKE_CHROME", "0,0")
    monkeypatch.setenv("FAKE_VIEWPORT", "1000,1000")  # whatever the window
    html = write(tmp_path, page(box(60, 60, "A headline")))
    with caplog.at_level(logging.WARNING, logger="studio.render"):
        result = render.render_card(html, tmp_path / "card_1.png", browser=stand_in.path)
    assert [c["args"].count("--dump-dom") for c in stand_in.calls()] == [1, 1, 0]
    assert render.png_size(result.png) == (2160, 2700)
    assert "not the card's (1080, 1350)" in caplog.text


def test_a_checker_without_a_viewport_leaves_the_window_alone(stand_in, tmp_path):
    html = write(tmp_path, page(box(60, 60, "A headline")))
    render.render_card(html, tmp_path / "card_1.png", browser=stand_in.path)
    assert [_window(c) for c in stand_in.calls()] == ["1080,1350", "1080,1350"]
    assert stand_in.path not in render._WINDOW_EXTRA


@pytest.mark.parametrize(
    "dom, found",
    [
        (f'<pre id="{render.VIEWPORT_ID}">[1080,1263]</pre>', (1080, 1263)),
        (f'<pre id="{render.VIEWPORT_ID}">[1080]</pre>', None),
        (f'<pre id="{render.VIEWPORT_ID}">nope</pre>', None),
        ("<html></html>", None),
    ],
)
def test_parse_viewport(dom, found):
    assert render.parse_viewport(dom) == found


# ---- crop_png -----------------------------------------------------------------------------


def _png_bytes(image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "LA"])
@pytest.mark.parametrize("size", [(40, 30), (37, 30), (40, 11)])
def test_crop_png_keeps_exactly_the_top_left_pixels(mode, size):
    image_mod = pytest.importorskip("PIL.Image")
    rng = random.Random(f"{mode}{size}")
    channels = len(mode)
    noise = bytes(rng.randrange(256) for _ in range(40 * 30 * channels))
    source = image_mod.frombytes(mode, (40, 30), noise)
    cropped = render.crop_png(_png_bytes(source), *size)
    got = image_mod.open(io.BytesIO(cropped))
    assert got.size == size and got.mode == mode
    assert got.tobytes() == source.crop((0, 0, *size)).tobytes()


def test_crop_png_returns_a_picture_of_the_right_size_untouched_and_refuses_the_rest():
    image_mod = pytest.importorskip("PIL.Image")
    data = _png_bytes(image_mod.new("RGB", (20, 10), (13, 17, 23)))
    assert render.crop_png(data, 20, 10) is data
    with pytest.raises(render.RenderError, match="smaller than the card"):
        render.crop_png(data, 21, 10)
    with pytest.raises(render.RenderError, match="not a PNG"):
        render.crop_png(b"GIF89a" + data, 10, 10)
    interlaced = io.BytesIO()
    image_mod.new("RGB", (20, 10)).save(interlaced, format="PNG", interlace=1)
    if interlaced.getvalue()[28] == 1:  # Pillow wrote an interlaced file
        with pytest.raises(render.RenderError, match="cannot crop"):
            render.crop_png(interlaced.getvalue(), 10, 10)


def test_a_screenshot_already_the_cards_size_or_smaller_is_left_alone(tmp_path):
    exact = tmp_path / "exact.png"
    exact.write_bytes(png_header(2160, 2700))
    small = tmp_path / "small.png"
    small.write_bytes(png_header(100, 100))
    for path in (exact, small):
        before = path.read_bytes()
        render._crop_to(path, 2160, 2700)
        assert path.read_bytes() == before


def _policy(text: str) -> str:
    return re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', text).group(1)


def test_the_policy_and_fonts_open_the_page_and_only_the_checker_may_run(stand_in, tmp_path):
    card = (
        "\ufeff<!-- made by the session -->\n<?xml version='1.0'?>\n<!DOCTYPE html>\n"
        "<html><head><title>card</title></head><body><p>Hi</p>"
        "<script>document.title = 'mine'</script></body></html>"
    )
    render.render_card(write(tmp_path, card), tmp_path / "card_1.png", browser=stand_in.path)
    check, shot = stand_in.calls()
    for text in (check["page"], shot["page"]):
        # Nothing may come before the doctype (quirks mode), and everything the card wrote
        # comes after the policy (a policy covers only what follows it).
        head, rest = text.split("<!DOCTYPE html>", 1)
        assert head == "\ufeff<!-- made by the session -->\n<?xml version='1.0'?>\n"
        assert rest.startswith(
            '<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="'
        )
        assert rest.index("@font-face") < rest.index("<html>") < rest.index("<p>Hi</p>")
        policy = _policy(text)
        assert "default-src 'none'" in policy
        assert "img-src data:;" in policy and "font-src file: data:;" in policy
        assert "style-src 'unsafe-inline'" in policy
        assert re.search(r"script-src 'nonce-[0-9a-f]{32}'$", policy)
    nonce = re.search(r"'nonce-([0-9a-f]+)'", _policy(check["page"])).group(1)
    assert _policy(shot["page"]) == _policy(check["page"])
    assert check["page"].count(f'<script nonce="{nonce}">') == 1  # the checker
    assert "<script>document.title = 'mine'</script>" in check["page"]  # no nonce: blocked
    render.render_card(write(tmp_path, card), tmp_path / "card_1.png", browser=stand_in.path)
    assert _policy(stand_in.calls()[-1]["page"]) != _policy(check["page"])  # fresh each time


def test_a_bare_fragment_gets_its_policy_first_and_its_checker_last(stand_in, tmp_path):
    render.render_card(
        write(tmp_path, "<p>bare</p>"), tmp_path / "card_1.png", browser=stand_in.path
    )
    check, shot = stand_in.calls()
    for call in (check, shot):
        assert call["page"].startswith('<meta charset="utf-8"><meta http-equiv="Content-Sec')
    assert shot["page"].endswith("<style>" + render.font_css() + "</style><p>bare</p>")
    assert check["page"].rstrip().endswith("</script>") and render.REPORT_ID in check["page"]


def test_the_checkers_findings_come_back_as_problems(stand_in, tmp_path, monkeypatch):
    found = ['text overlaps other text: "R&D <spend>" and "Q3"', "the page is larger"]
    monkeypatch.setenv("FAKE_REPORT", json.dumps(found))
    result = render.render_card(
        write(tmp_path, page("x")), tmp_path / "card_1.png", browser=stand_in.path
    )
    assert result.problems == found and not result.ok


def test_a_checker_that_never_reported_is_a_problem_but_the_picture_is_kept(
    stand_in, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_REPORT", "none")
    result = render.render_card(
        write(tmp_path, page("x")), tmp_path / "card_1.png", browser=stand_in.path
    )
    assert result.problems == [NOT_RUN]
    assert result.png.is_file()


def test_no_picture_raises_and_never_leaves_the_last_rounds_picture(
    stand_in, tmp_path, monkeypatch
):
    png = tmp_path / "card_1.png"
    png.write_bytes(png_header(2160, 2700))  # drawn in an earlier polish round
    monkeypatch.setenv("FAKE_PNG", "none")
    with pytest.raises(
        render.RenderError,
        match=r"made no picture \(exit 3\): ERROR: could not write the screenshot",
    ):
        render.render_card(write(tmp_path, page("x")), png, browser=stand_in.path)
    assert not png.exists()


def test_a_browser_that_hangs_is_stopped(stand_in, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    started = time.monotonic()
    with pytest.raises(render.RenderError, match="took longer than 1s"):
        render.render_card(
            write(tmp_path, page("x")), tmp_path / "c.png", browser=stand_in.path, timeout=1
        )
    assert time.monotonic() - started < 15
    assert len(stand_in.calls()) == 1  # no screenshot after the check timed out


def test_a_browser_that_cannot_start_raises(tmp_path):
    with pytest.raises(render.RenderError, match="could not start the browser"):
        render.render_card(
            write(tmp_path, page("x")), tmp_path / "c.png", browser=str(tmp_path / "nothing")
        )


@pytest.mark.parametrize(
    "meta",
    [
        '<meta http-equiv="refresh" content="0;url=file:///home/user/.env">',
        "<META HTTP-EQUIV=Refresh CONTENT='0; url=secrets.html'>",
        '<meta http-equiv = " refresh " content="1">',
        '<meta http-equiv="&#114;efresh" content="0;url=file:///etc/passwd">',
        '<iframe srcdoc="&lt;meta http-equiv=&quot;refresh&quot; content=&quot;0;'
        'url=file:///etc/passwd&quot;&gt;"></iframe>',
    ],
)
def test_a_meta_refresh_is_refused_before_any_browser_runs(stand_in, tmp_path, meta):
    png = tmp_path / "card_1.png"
    with pytest.raises(render.RenderError, match="refresh"):
        render.render_card(write(tmp_path, page("x", head=meta)), png, browser=stand_in.path)
    assert stand_in.calls() == [] and not png.exists()


@pytest.mark.parametrize(
    "meta",
    [
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">',
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">',
    ],
)
def test_other_http_equiv_metas_are_drawn(stand_in, tmp_path, meta):
    render.render_card(
        write(tmp_path, page("x", head=meta)), tmp_path / "card_1.png", browser=stand_in.path
    )
    assert len(stand_in.calls()) == 2


OUTSIDE = (
    "the card loads something from the web or another file (a font, image or script); cards "
    "are self-contained and the renderer blocks both, so inline it or drop it"
)


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="https://example.com/logo.png">',
        "<img src='http://example.com/logo.png'>",
        "<img src=https://example.com/logo.png>",
        '<link rel="stylesheet" href="//fonts.googleapis.com/css2?family=Roboto">',
        "<div style=\"background: url( 'https://example.com/bg.png' )\"></div>",
        '<style>@import url("https://fonts.googleapis.com/css2?family=Roboto");</style>',
        '<img src="file:///home/user/logo.png">',
        '<svg><image xlink:href="https://example.com/x.png"/></svg>',
    ],
)
def test_a_card_that_loads_from_outside_is_told_so(stand_in, tmp_path, markup):
    result = render.render_card(
        write(tmp_path, page(markup)), tmp_path / "card_1.png", browser=stand_in.path
    )
    assert result.problems == [OUTSIDE]


@pytest.mark.parametrize(
    "markup",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(#g)"/></svg>',
        '<img src="data:image/png;base64,iVBORw0KGgo=">',
        "<div style=\"background:url('data:image/svg+xml,%3Csvg%3E%3C/svg%3E')\"></div>",
        "<p>Source: company release (merck.com), see https://example.com</p>",
    ],
)
def test_inline_data_and_references_inside_the_card_are_fine(stand_in, tmp_path, markup):
    result = render.render_card(
        write(tmp_path, page(markup)), tmp_path / "card_1.png", browser=stand_in.path
    )
    assert result.problems == []


@pytest.mark.parametrize("markup", ["<script>layout()</script>", '<SCRIPT type="module"></SCRIPT>'])
def test_a_card_with_a_script_is_told_it_will_not_run(stand_in, tmp_path, markup):
    result = render.render_card(
        write(tmp_path, page(markup)), tmp_path / "card_1.png", browser=stand_in.path
    )
    assert result.problems == [
        "the card has a script, and the renderer never runs a card's scripts; lay it out "
        "with HTML and CSS alone"
    ]


# ---- the real browser ----------------------------------------------------------------------


@functools.cache
def working_browser() -> str | None:
    """A browser that can actually draw here. One that exists but cannot start (a sandbox the
    machine refuses, say) is as good as none, so its tests skip instead of failing."""
    try:
        browser = render.find_browser()
    except render.RenderError:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        smoke = Path(tmp) / "smoke.html"
        smoke.write_text("<p>smoke</p>", encoding="utf-8")
        shot = Path(tmp) / "smoke.png"
        argv = render.browser_argv(browser, render.DEFAULT_SIZE) + [
            f"--user-data-dir={tmp}/profile",
            f"--screenshot={shot}",
            smoke.as_uri(),
        ]
        try:
            subprocess.run(argv, capture_output=True, timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return browser if shot.is_file() else None


@pytest.fixture(scope="module")
def browser() -> str:
    found = working_browser()
    if found is None:
        pytest.skip("no Chromium-family browser that can draw here")
    return found


def draw_all(browser: str, folder: Path, cards: dict[str, str]) -> dict:
    """Every card drawn side by side: name -> RenderResult, or the RenderError raised."""

    def draw(name: str):
        html = write(folder, cards[name], f"{name}.html")
        try:
            return render.render_card(html, folder / f"{name}.png", browser=browser)
        except render.RenderError as exc:
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(cards, pool.map(draw, cards), strict=True))


MONO = "font-family:'IBM Plex Mono';font-size:50px;white-space:nowrap;overflow:hidden;"
KICKER = box(52, 52, "CTLA-4 DEEP DIVE · 2/4", "font-family:'IBM Plex Mono';font-size:14px")
HERO = (
    '<h1 style="position:absolute;left:52px;top:76px;margin:0;font-size:150px;'
    'line-height:0.88;font-weight:900;letter-spacing:-0.03em;text-transform:uppercase">'
    "Twice the<br>response</h1>"
)
CARDS = {
    "clean": page(
        box(56, 56, "A clean headline", "font-size:52px;font-weight:700")
        + box(56, 140, "One line that fits.", "font-size:20px")
    ),
    "wide": page(box(60, 60, "A wide timeline", "font-size:40px"), size=render.WIDE_SIZE),
    "cut_off": page(box(60, 60, LONG, "width:300px;height:40px;overflow:hidden;font-size:30px")),
    "cut_by_parent": page(
        box(
            60,
            60,
            f'<p style="margin:0">{LONG}</p>',
            "width:300px;height:40px;overflow:hidden;font-size:30px",
        )
    ),
    "cut_by_svg": page(
        '<svg style="position:absolute;left:60px;top:100px" width="600" height="200">'
        '<text x="560" y="100" fill="#fff" font-size="30">2026 label</text></svg>'
    ),
    "svg_labels_inside": page(
        '<svg style="position:absolute;left:60px;top:100px" width="600" height="200">'
        '<text x="0" y="14" fill="#fff" font-size="15">2024</text>'
        '<text x="600" y="196" fill="#fff" font-size="15" text-anchor="end">2026</text>'
        '<rect x="0" y="40" width="600" height="24" rx="4" fill="#3987e5"/></svg>'
        '<svg style="position:absolute;left:60px;top:400px;overflow:visible" width="600"'
        ' height="200"><text x="560" y="100" fill="#fff" font-size="30">drawn outside on'
        " purpose</text></svg>"
    ),
    "overlap": page(
        box(100, 100, "First label here", "font-size:40px")
        + box(120, 110, "Second label", "font-size:40px")
    ),
    "near_edge": page(box(5, 300, "Hugging the left edge", "font-size:30px")),
    "off_canvas": page(
        box(900, 300, "Running off the right side", "font-size:30px;white-space:nowrap")
    ),
    "too_tall": (
        "<!doctype html><html><head><style>html,body{margin:0;width:1080px;background:#0d1117;"
        'color:#fff;font-family:Inter}</style></head><body><div style="height:2000px;'
        'padding:60px;box-sizing:border-box">A page taller than the card</div>'
        + rail(1350)
        + "</body></html>"
    ),
    "ellipsis": page(
        box(
            60,
            60,
            LONG,
            "width:300px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:30px",
        )
    ),
    "external": page(
        box(60, 60, "Headline", "font-size:40px")
        + '<img src="https://example.com/logo.png" style="position:absolute;left:60px;top:200px">'
    ),
    "pre": page('<pre style="position:absolute;left:5px;top:60px;margin:0">ORR   44%</pre>'),
    "hero": page(KICKER + HERO + box(52, 370, "Objective response rate", "font-size:21px")),
    "hero_collision": page(KICKER + HERO + box(60, 120, "Collides with it", "font-size:40px")),
    "clipped_decoration": page(
        '<div style="position:absolute;inset:0;overflow:hidden">'
        '<div style="position:absolute;right:-150px;top:-150px;width:400px;height:400px;'
        'border-radius:50%;background:#d9592633"></div>'
        + box(60, 60, "Headline inside", "font-size:40px")
        + "</div>"
    ),
    # IBM Plex Mono advances every glyph 0.6em: ten glyphs at 50px are 300px wide, so ten
    # i's overflow 250px and ten W's fit 320px. A fallback font fails one or the other.
    "mono": page(
        box(60, 100, "iiiiiiiiii", MONO + "width:250px")
        + box(60, 300, "WWWWWWWWWW", MONO + "width:320px")
    ),
    "unclosed_comment": page(box(60, 60, "Headline", "font-size:40px") + "<!-- never closed"),
    # Two rows floating in a tall panel, as the first real studio card was drawn.
    "sparse": page(
        box(56, 56, "The OS miss the FDA will weigh", "font-size:52px;font-weight:700")
        + '<div style="position:absolute;left:56px;top:216px;width:968px;height:1030px;'
        'background:#151a22;border:1px solid #252c37;border-radius:18px">'
        + box(40, 420, "HARMONi (global)", "font-size:24px")
        + box(40, 560, "HARMONi-A (China)", "font-size:24px")
        + "</div>",
        filled=False,
    ),
    # A footer on the card's last lines, where a short viewport used to cut the picture.
    "bottom_edge": page(
        box(56, 56, "A headline", "font-size:52px;font-weight:700")
        + box(56, 1280, "Not investment advice.", "font-size:16px;color:#8a93a3")
        + '<i style="position:absolute;left:56px;top:1306px;width:968px;height:20px;'
        'background:#ff00ff"></i>'
    ),
}


@pytest.fixture(scope="module")
def drawn(browser, tmp_path_factory) -> dict:
    return draw_all(browser, tmp_path_factory.mktemp("cards"), CARDS)


def problems(drawn: dict, name: str) -> list[str]:
    result = drawn[name]
    assert isinstance(result, render.RenderResult), result
    return result.problems


def test_a_clean_card_has_no_problems_and_a_2x_picture(drawn):
    result = drawn["clean"]
    assert result.problems == [] and result.ok and result.size == (1080, 1350)
    assert render.png_size(result.png) == (2160, 2700)


def test_an_empty_band_in_the_middle_or_at_the_bottom_is_reported(drawn):
    found = problems(drawn, "sparse")
    assert len(found) == 2, found
    middle, bottom = found
    assert re.fullmatch(
        r'an empty band (\d+)px tall \((\d+)% of the card\) between "The OS miss the FDA will'
        r' weigh" and "HARMONi \(global\)": fill it .*',
        middle,
    ), middle
    assert int(re.search(r"(\d+)px", middle).group(1)) > 1350 // 4
    assert bottom.startswith("an empty band ") and bottom.split(" between ")[1].startswith(
        '"HARMONi-A (China)" and the bottom of the card'
    )


def test_the_whole_card_is_drawn_down_to_its_last_pixels(drawn):
    image_mod = pytest.importorskip("PIL.Image")
    result = drawn["bottom_edge"]
    assert result.problems == []
    with image_mod.open(result.png) as image:
        assert image.size == (2160, 2700)
        # The magenta bar sits at 1306-1326 CSS px: rows 2612-2652 of the 2x picture.
        assert image.convert("RGB").getpixel((1000, 2632)) == (255, 0, 255)
        assert image.convert("RGB").getpixel((1000, 2690)) == (13, 17, 23)


def test_a_wide_card_is_drawn_at_3200_by_1800(drawn):
    result = drawn["wide"]
    assert result.problems == [] and result.size == (1600, 900)
    assert render.png_size(result.png) == (3200, 1800)


@pytest.mark.parametrize(
    "name, label",
    [
        ("cut_off", LONG),
        ("cut_by_parent", LONG),  # the clipping box holds no text of its own
        ("cut_by_svg", "2026 label"),  # an <svg> cuts its drawing at its own edge
    ],
)
def test_text_cut_off_by_its_box_is_reported(drawn, name, label):
    assert problems(drawn, name) == [f'text is cut off in its box: "{label}"']


def test_text_inside_its_box_or_in_an_unclipped_svg_is_fine(drawn):
    assert problems(drawn, "svg_labels_inside") == []
    assert problems(drawn, "clipped_decoration") == []  # the box hides a shape, not text


def test_overlapping_text_is_reported(drawn):
    assert problems(drawn, "overlap") == [
        'text overlaps other text: "First label here" and "Second label"'
    ]


def test_text_near_or_past_the_edge_is_reported(drawn):
    assert problems(drawn, "near_edge") == [
        'text runs off the card or within 20px of its edge: "Hugging the left edge"'
    ]
    first, second = problems(drawn, "off_canvas")
    assert first == (
        'text runs off the card or within 20px of its edge: "Running off the right side"'
    )
    assert re.fullmatch(r"the page is larger than the 1080x1350 card \(\d{4}x1350\)", second)


def test_a_page_larger_than_the_card_is_reported(drawn):
    assert problems(drawn, "too_tall") == ["the page is larger than the 1080x1350 card (1080x2000)"]


def test_text_truncated_with_an_ellipsis_is_reported(drawn):
    assert problems(drawn, "ellipsis") == [
        f'text is cut off in its box: "{LONG}"',
        f'text is truncated with an ellipsis: "{LONG}"',
    ]


def test_an_external_reference_is_reported_and_the_card_still_drawn(drawn):
    result = drawn["external"]
    assert result.problems == [OUTSIDE]
    assert render.png_size(result.png) == (2160, 2700)


def test_text_in_a_pre_block_is_checked_like_any_other(drawn):
    assert problems(drawn, "pre") == [
        'text runs off the card or within 20px of its edge: "ORR 44%"'
    ]


def test_a_hero_headline_with_tight_leading_is_judged_by_its_lines(drawn):
    # The font's ascent reaches 17px above each 132px line; the kicker sits 6px above it.
    assert problems(drawn, "hero") == []
    assert problems(drawn, "hero_collision") == [
        'text overlaps other text: "Twice theresponse" and "Collides with it"'
    ]


def test_the_house_fonts_are_the_ones_drawn(drawn):
    assert problems(drawn, "mono") == ['text is cut off in its box: "iiiiiiiiii"']


def test_a_page_the_checker_cannot_run_in_still_gets_a_picture(drawn):
    result = drawn["unclosed_comment"]
    assert result.problems == [NOT_RUN]
    assert render.png_size(result.png) == (2160, 2700)


def test_the_browser_flags_keep_every_request_off_the_network(browser, tmp_path):
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        test_page = tmp_path / "net.html"
        test_page.write_text(
            f'<img src="http://127.0.0.1:{port}/by-address">'
            f'<img src="http://localhost:{port}/by-name">'
            f'<script>fetch("http://127.0.0.1:{port}/fetch").catch(() => 0)</script>',
            encoding="utf-8",
        )

        def visit(argv: list[str], profile: str) -> list[str]:
            hits.clear()
            subprocess.run(
                [*argv, f"--user-data-dir={tmp_path / profile}", "--dump-dom", test_page.as_uri()],
                capture_output=True,
                timeout=90,
                check=False,
            )
            return sorted(hits)

        blocked = render.browser_argv(browser, render.DEFAULT_SIZE)
        open_ = [a for a in blocked if not a.startswith(("--host-resolver", "--proxy-server"))]
        if not visit(open_, "open"):
            pytest.skip("this browser reaches no local server even with the network open")
        assert visit(blocked, "blocked") == []
    finally:
        server.shutdown()
        server.server_close()


# A card is HTML the session wrote, possibly after reading pages planted for it. The browser
# can read every file the app can, and the session is told to open the pictures, so a card
# that could draw another file (a .env, a key) could read it. Each leak below draws a magenta
# file; the picture must hold no magenta at all.
LEAKS = {
    "iframe": '<iframe src="{u}/secret.html" style="width:900px;height:900px;border:0"></iframe>',
    "object": '<object data="{u}/secret.html" style="width:900px;height:900px"></object>',
    "embed": '<embed src="{u}/secret.html" style="width:900px;height:900px">',
    "img": '<img src="{u}/secret.png" style="width:900px;height:900px">',
    "css_background": '<div style="width:900px;height:900px;background:url({u}/secret.png)"></div>',
    "stylesheet": '<link rel="stylesheet" href="{u}/secret.css">',
    "svg_image": '<svg width="900" height="900"><image href="{u}/secret.png" width="900" '
    'height="900"/></svg>',
    "frame_in_srcdoc": '<iframe srcdoc="&lt;iframe src=&quot;{u}/secret.html&quot; '
    'width=800 height=800&gt;&lt;/iframe&gt;" style="width:900px;height:900px"></iframe>',
    "script_paints": '<script>document.body.style.background = "#ff00ff";</script>',
    "script_navigates": '<script>location.href = "{u}/secret.html";</script>',
}


@pytest.fixture(scope="module")
def private_files(tmp_path_factory) -> str:
    image = pytest.importorskip("PIL.Image")
    folder = tmp_path_factory.mktemp("private")
    (folder / "secret.html").write_text(
        '<body style="margin:0;background:#ff00ff">SECRET</body>', encoding="utf-8"
    )
    (folder / "secret.css").write_text("html,body{background:#ff00ff !important}", "utf-8")
    image.new("RGB", (40, 40), (255, 0, 255)).save(folder / "secret.png")
    return folder.as_uri()


def magenta(png: Path) -> int:
    from PIL import Image

    with Image.open(png) as im:
        colours = im.convert("RGB").getcolors(maxcolors=im.width * im.height)
    return sum(n for n, (r, g, b) in colours if r > 200 and g < 80 and b > 200)


@pytest.fixture(scope="module")
def leaked(browser, private_files, tmp_path_factory) -> dict:
    cards = {k: page(box(60, 60, v.format(u=private_files))) for k, v in LEAKS.items()}
    return draw_all(browser, tmp_path_factory.mktemp("leaks"), cards)


def test_the_leak_check_sees_a_file_the_browser_draws_unguarded(browser, private_files, tmp_path):
    # The same markup without the renderer's policy: the browser draws the file, so the
    # magenta count is a real witness and the tests below cannot pass by accident.
    for markup in (LEAKS["iframe"], LEAKS["img"]):
        html = write(tmp_path, page(box(60, 60, markup.format(u=private_files))), "raw.html")
        shot = tmp_path / "raw.png"
        subprocess.run(
            render.browser_argv(browser, render.DEFAULT_SIZE)
            + [f"--user-data-dir={tmp_path / 'profile'}", f"--screenshot={shot}", html.as_uri()],
            capture_output=True,
            timeout=90,
            check=False,
        )
        assert magenta(shot) > 10_000


@pytest.mark.parametrize("name", list(LEAKS))
def test_a_card_draws_nothing_but_itself(leaked, name):
    result = leaked[name]
    assert isinstance(result, render.RenderResult), result
    assert render.png_size(result.png) == (2160, 2700)
    assert magenta(result.png) == 0
