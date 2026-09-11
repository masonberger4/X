"""End-to-end run_publish tests against a temp SQLite file seeded via step 1/2's real schema.

client.post_tweet is monkeypatched everywhere; tweepy is never imported.
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import run_publish
from approval_queue import store as qstore
from draft.chart import Chart
from draft.schema import Draft
from publish import client, store
from tests.conftest import URL, seed_item

NY = ZoneInfo("America/New_York")
SLOT_TIME = datetime(2026, 6, 1, 8, 35, tzinfo=NY)  # inside the 08:30 slot window
OFF_SLOT = datetime(2026, 6, 1, 10, 0, tzinfo=NY)


def seed_draft(conn, item_id, *, source="pubmed", approve=True, thread=None, edit_to=None):
    cid = seed_item(conn, item_id, source=source)
    thread = thread or [f"one {URL}", "two", f"three {URL}"]
    d = Draft(
        single_post=f"Post {item_id} {URL}", thread=thread, suggested_visual="", why_it_matters=""
    )
    did = qstore.insert_draft(conn, item_id=item_id, cluster_id=cid, model="m", draft=d)
    if edit_to:
        qstore.edit(conn, did, single_post=edit_to, thread=thread)
    elif approve:
        qstore.approve(conn, did)
    return did


class FakeX:
    def __init__(self, fail_at: int | None = None, fail_upload: bool = False):
        self.calls: list[tuple[str, str | None]] = []
        self.media: list[tuple[str, str | None]] = []  # (text, media_id) per tweet
        self.uploads: list[tuple[str, str]] = []  # (path, alt text)
        self.fail_at = fail_at
        self.fail_upload = fail_upload

    def __call__(self, text, in_reply_to=None, media_ids=None):
        self.calls.append((text, in_reply_to))
        self.media.append((text, media_ids[0] if media_ids else None))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise client.PublishError("HTTP 500", retryable=True, status=500)
        return f"tw{len(self.calls)}"

    def upload(self, path, alt_text=""):
        if self.fail_upload:
            raise client.PublishError("media_upload: HTTP 400", status=400)
        self.uploads.append((path, alt_text))
        return f"m{len(self.uploads)}"


@pytest.fixture
def fake_x(monkeypatch):
    fx = FakeX()
    monkeypatch.setattr(client, "post_tweet", fx)
    monkeypatch.setattr(client, "upload_media", fx.upload)
    monkeypatch.setenv("BIO_DISCLOSURE_CONFIRMED", "1")
    monkeypatch.delenv("PUBLISH_ENABLED", raising=False)
    return fx


@pytest.fixture
def keep_failed(tmp_path):
    """--config args for a publish config with automatic release of failed claims off."""
    cfg = tmp_path / "publish.yaml"
    cfg.write_text(
        "timezone: America/New_York\nslots: ['08:30', '12:15']\n"
        "retry:\n  auto_release_failed: false\n"
    )
    return ["--config", str(cfg)]


def seed_image(conn, did, *, chart=None):
    """Give a draft a chart spec and a (fake) rendered PNG in the images folder."""
    chart = chart or Chart("ORR by arm", ["A", "B"], [88.0, 14.6], "%", "n=97")
    conn.execute(
        "UPDATE drafts SET chart_json = ? WHERE id = ?", (json.dumps(chart.to_dict()), did)
    )
    conn.commit()
    path = qstore.image_file(did)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG fake")
    qstore.set_image(conn, did, path)
    return path


def test_tweepy_not_imported_at_module_import():
    assert "tweepy" not in sys.modules


def test_fetch_approved_uses_real_schema_and_prefers_edit(conn):
    seed_draft(conn, "a", approve=False)  # pending: excluded
    seed_draft(conn, "b")
    seed_draft(conn, "c", source="fda_press", edit_to=f"Edited c {URL}")
    got = store.fetch_approved(10, conn=conn)
    assert [a.item_id for a in got] == ["b", "c"]
    assert got[0].source == "pubmed" and got[0].url == URL and got[0].score == 8.5
    assert got[1].single_post == f"Edited c {URL}" and got[1].edited
    assert got[1].source == "fda_press"


def test_dry_run_default_never_posts(conn, fake_x, capsys):
    seed_draft(conn, "a")
    assert run_publish.main([], now=SLOT_TIME) == 0
    assert fake_x.calls == []
    assert "DRY RUN" in capsys.readouterr().out
    assert store.get_schedule(conn, 1) is None  # dry run does not claim


def test_live_without_env_gate_stays_dry(conn, fake_x, monkeypatch):
    seed_draft(conn, "a")
    assert run_publish.main(["--live", "--format", "single"], now=SLOT_TIME) == 0
    assert fake_x.calls == []
    monkeypatch.setenv("PUBLISH_ENABLED", "0")
    assert run_publish.main(["--live", "--format", "single"], now=SLOT_TIME) == 0
    assert fake_x.calls == []
    # env alone (no --live) is not enough either
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    assert run_publish.main([], now=SLOT_TIME) == 0
    assert fake_x.calls == []


def test_live_posts_single_and_is_idempotent(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a")
    assert run_publish.main(["--live", "--format", "single"], now=SLOT_TIME) == 0
    assert len(fake_x.calls) == 1 and fake_x.calls[0] == (f"Post a {URL}", None)
    rows = store.list_posts(conn, 1)
    assert rows[0]["tweet_id"] == "tw1" and rows[0]["slot"] == "2026-06-01 08:30"
    assert store.get_schedule(conn, 1)["status"] == "posted"
    # cron fires again in the same slot window: same draft is never re-posted
    assert run_publish.main(["--live", "--format", "single"], now=SLOT_TIME) == 0
    assert len(fake_x.calls) == 1


def test_claim_is_atomic_under_double_run(conn):
    other = store.connect(conn.execute("PRAGMA database_list").fetchone()["file"])
    assert store.claim(conn, 7, "2026-06-01 08:30") is True
    assert store.claim(other, 7, "2026-06-01 08:30") is False
    assert store.claim(conn, 7) is False
    other.close()


def test_no_open_slot_posts_nothing(conn, fake_x, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a")
    assert run_publish.main(["--live", "--format", "single"], now=OFF_SLOT) == 0
    assert fake_x.calls == []
    assert "next slot 2026-06-01 12:15" in caplog.text


def test_now_flag_ignores_slots(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a")
    assert run_publish.main(["--live", "--now", "--format", "single"], now=OFF_SLOT) == 0
    assert len(fake_x.calls) == 1


def test_breaking_posts_outside_slots_but_respects_gap(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a")  # regular
    seed_draft(conn, "b", source="fda_oce_approvals")
    assert run_publish.main(["--live", "--breaking", "--format", "single"], now=OFF_SLOT) == 0
    assert fake_x.calls == [(f"Post b {URL}", None)]
    # a second breaking item minutes later is blocked by min_gap
    seed_draft(conn, "c", source="fda_press")
    assert run_publish.main(["--live", "--format", "single"], now=OFF_SLOT) == 0
    assert len(fake_x.calls) == 1
    assert store.get_schedule(conn, 3) is None


def test_daily_cap(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    for i in range(4):
        seed_draft(conn, f"d{i}", source="fda_press")
    times = [datetime(2026, 6, 1, h, 0, tzinfo=NY) for h in (6, 9, 12, 15, 18)]
    for t in times:
        run_publish.main(["--live", "--format", "single"], now=t)
    assert len(fake_x.calls) == 3  # max_posts_per_day


def test_thread_posts_in_order_with_reply_chain(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a")
    assert run_publish.main(["--live", "--format", "thread"], now=SLOT_TIME) == 0
    assert [c[1] for c in fake_x.calls] == [None, "tw1", "tw2"]
    assert fake_x.calls[0][0].endswith("(1/3)") and URL in fake_x.calls[2][0]
    rows = store.list_posts(conn, 1)
    assert [r["position"] for r in rows] == [1, 2, 3]
    assert all(r["kind"] == "thread" for r in rows)


def test_partial_thread_failure_is_recorded_and_not_retried(conn, monkeypatch, caplog):
    fx = FakeX(fail_at=2)
    monkeypatch.setattr(client, "post_tweet", fx)
    monkeypatch.setenv("BIO_DISCLOSURE_CONFIRMED", "1")
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a")
    assert run_publish.main(["--live", "--format", "thread"], now=SLOT_TIME) == 2
    assert len(fx.calls) == 2
    rows = store.list_posts(conn, 1)
    assert [(r["position"], r["tweet_id"], r["status"]) for r in rows] == [
        (1, "tw1", "posted"),
        (2, None, "failed"),
    ]
    assert "HTTP 500" in rows[1]["error"]
    assert store.get_schedule(conn, 1)["status"] == "partial"
    assert "THREAD PARTIAL" in caplog.text
    # next run: the draft is claimed, so it is not retried
    assert run_publish.main(["--live", "--format", "thread"], now=SLOT_TIME) == 0
    assert len(fx.calls) == 2


def test_refuses_content_that_fails_hard_check(conn, fake_x, monkeypatch, caplog):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a", edit_to="edited without the link")
    assert run_publish.main(["--live", "--format", "single"], now=SLOT_TIME) == 0
    assert fake_x.calls == []
    assert store.get_schedule(conn, 1)["status"] == "refused"
    assert "missing source URL" in caplog.text


def test_bio_warning_when_not_confirmed(conn, fake_x, monkeypatch, caplog):
    monkeypatch.delenv("BIO_DISCLOSURE_CONFIRMED")
    run_publish.main([], now=OFF_SLOT)
    assert "BIO_DISCLOSURE_CONFIRMED" in caplog.text


def test_client_retry_classification(monkeypatch):
    """Retry on 429/5xx, never on 401/403, without importing tweepy."""
    import types

    class TooManyRequests(Exception): ...

    class TwitterServerError(Exception): ...

    class Unauthorized(Exception): ...

    class Forbidden(Exception): ...

    class TweepyException(Exception): ...

    fake = types.SimpleNamespace(
        TooManyRequests=TooManyRequests,
        TwitterServerError=TwitterServerError,
        Unauthorized=Unauthorized,
        Forbidden=Forbidden,
        TweepyException=TweepyException,
    )
    monkeypatch.setitem(sys.modules, "tweepy", fake)
    slept = []
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TooManyRequests("slow down")
        return "ok"

    assert client._with_retries(flaky, "op", sleep=slept.append) == "ok"
    assert slept == [2.0, 4.0]

    def forbidden():
        raise Forbidden("duplicate")

    with pytest.raises(client.PublishError) as ei:
        client._with_retries(forbidden, "op", sleep=slept.append)
    assert ei.value.status == 403 and not ei.value.retryable and len(slept) == 2

    def unauthorized():
        raise Unauthorized("bad key")

    with pytest.raises(client.PublishError) as ei:
        client._with_retries(unauthorized, "op", sleep=slept.append)
    assert ei.value.status == 401 and len(slept) == 2

    # a transport error (connection reset, timeout) never reached X: retry it
    attempts["n"] = 0

    def reset():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise TweepyException("Failed to send request: Connection aborted.")
        return "ok"

    assert client._with_retries(reset, "op", sleep=slept.append) == "ok"
    assert slept == [2.0, 4.0, 2.0]

    # tweepy.Client (v2) lets requests' raw ConnectionError through: an OSError, also retried
    attempts["n"] = 0

    def raw_reset():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ConnectionResetError(10054, "An existing connection was forcibly closed")
        return "ok"

    assert client._with_retries(raw_reset, "op", sleep=slept.append) == "ok"
    assert slept == [2.0, 4.0, 2.0, 2.0]
    monkeypatch.delitem(sys.modules, "tweepy")


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"x" if payload else b""

    def json(self):
        return self._payload


def _fake_tweepy_for_upload(monkeypatch, responses):
    """A tweepy stand-in whose API.session.request records calls and pops canned responses."""
    import types

    calls = []

    class Session:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    class Auth:
        def __init__(self, *a):
            self.keys = a

        def apply_auth(self):
            return "oauth1-signature"

    class API:
        def __init__(self, auth):
            self.auth = auth
            self.session = Session()

    class HTTPException(Exception):
        def __init__(self, resp):
            super().__init__(f"HTTP {resp.status_code}")
            self.response = resp

    fake = types.SimpleNamespace(
        OAuth1UserHandler=Auth,
        API=API,
        TweepyException=type("TweepyException", (Exception,), {}),
        HTTPException=HTTPException,
        BadRequest=type("BadRequest", (HTTPException,), {}),
        Unauthorized=type("Unauthorized", (HTTPException,), {}),
        Forbidden=type("Forbidden", (HTTPException,), {}),
        NotFound=type("NotFound", (HTTPException,), {}),
        TooManyRequests=type("TooManyRequests", (HTTPException,), {}),
        TwitterServerError=type("TwitterServerError", (HTTPException,), {}),
    )
    monkeypatch.setitem(sys.modules, "tweepy", fake)
    for k, v in {
        "X_API_KEY": "k",
        "X_API_SECRET": "s",
        "X_ACCESS_TOKEN": "t",
        "X_ACCESS_TOKEN_SECRET": "ts",
    }.items():
        monkeypatch.setenv(k, v)
    return calls


def test_upload_media_uses_v2_endpoints_with_alt_text(monkeypatch, tmp_path):
    png = tmp_path / "draft_19.png"
    png.write_bytes(b"\x89PNG fake")
    calls = _fake_tweepy_for_upload(
        monkeypatch,
        [_FakeResponse(200, {"data": {"id": "777", "media_key": "3_777"}}), _FakeResponse(200)],
    )
    assert client.upload_media(str(png), "Table: pCR vs survival") == "777"
    assert [(m, u) for m, u, _ in calls] == [
        ("POST", client.MEDIA_UPLOAD_URL),
        ("POST", client.MEDIA_METADATA_URL),
    ]
    up = calls[0][2]
    assert up["auth"] == "oauth1-signature"
    assert up["data"] == {"media_category": "tweet_image"}
    assert up["files"]["media"][0] == "draft_19.png"
    assert calls[1][2]["json"] == {
        "id": "777",
        "metadata": {"alt_text": {"text": "Table: pCR vs survival"}},
    }
    monkeypatch.delitem(sys.modules, "tweepy")


def test_upload_media_skips_metadata_without_alt_text(monkeypatch, tmp_path):
    png = tmp_path / "d.png"
    png.write_bytes(b"\x89PNG fake")
    calls = _fake_tweepy_for_upload(monkeypatch, [_FakeResponse(200, {"data": {"id": "1"}})])
    assert client.upload_media(str(png)) == "1"
    assert len(calls) == 1
    monkeypatch.delitem(sys.modules, "tweepy")


def test_upload_media_retries_connection_reset_then_fails_on_403(monkeypatch, tmp_path):
    png = tmp_path / "d.png"
    png.write_bytes(b"\x89PNG fake")
    calls = _fake_tweepy_for_upload(
        monkeypatch,
        [ConnectionResetError(10054, "forcibly closed"), _FakeResponse(200, {"data": {"id": "5"}})],
    )
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    assert client.upload_media(str(png)) == "5"
    assert len(calls) == 2

    calls = _fake_tweepy_for_upload(monkeypatch, [_FakeResponse(403, {"title": "Forbidden"})])
    with pytest.raises(client.PublishError) as ei:
        client.upload_media(str(png))
    assert ei.value.status == 403 and not ei.value.retryable and len(calls) == 1
    monkeypatch.delitem(sys.modules, "tweepy")


def test_missing_keys_raise_without_network(monkeypatch):
    for k in (
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_SECRET",
        "X_ACCESS_TOKEN_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(client.PublishError, match="missing X credentials"):
        client._keys()


def test_fetch_approved_uses_ai_revision_over_older_human_edit(conn):
    seed_draft(conn, "a", approve=False)
    qstore.edit(
        conn, 1, single_post=f"Human edit {URL}", thread=["1", "2", f"3 {URL}"], approve_after=False
    )
    revised = Draft(
        single_post=f"Revised {URL}",
        thread=["r1", "r2", f"r3 {URL}"],
        suggested_visual="",
        why_it_matters="w",
        claims_to_verify=[],
    )
    qstore.revise(conn, 1, draft=revised, model="m", note="tighter")
    qstore.approve(conn, 1)
    (got,) = store.fetch_approved(10, conn=conn)
    assert got.single_post == f"Revised {URL}" and got.thread == ["r1", "r2", f"r3 {URL}"]


# --- images ------------------------------------------------------------------


def test_fetch_approved_carries_image_and_alt_text(conn):
    did = seed_draft(conn, "a")
    path = seed_image(conn, did)
    got = store.fetch_approved(10, conn=conn)
    assert got[0].image_path == str(path)
    assert got[0].image_alt.startswith("Bar chart: ORR by arm. A 88%; B 14.6%.")
    assert "n=97." in got[0].image_alt and "Source: doi.org." in got[0].image_alt
    # file gone (or dropped): publish as text-only rather than fail
    path.unlink()
    assert store.fetch_approved(10, conn=conn)[0].image_path is None


def test_live_attaches_image_to_first_post_only(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    did = seed_draft(conn, "a")
    path = seed_image(conn, did)
    assert run_publish.main(["--live", "--format", "thread"], now=SLOT_TIME) == 0
    assert fake_x.uploads == [
        (
            str(path),
            store.fetch_approved.__globals__["alt_text"](
                Chart("ORR by arm", ["A", "B"], [88.0, 14.6], "%", "n=97"), URL
            ),
        )
    ]
    assert [m for _, m in fake_x.media] == ["m1", None, None]
    assert store.get_schedule(conn, did)["status"] == "posted"


def test_dry_run_prints_image(conn, fake_x, capsys):
    did = seed_draft(conn, "a")
    path = seed_image(conn, did)
    run_publish.main([], now=SLOT_TIME)
    out = capsys.readouterr().out
    assert f"image on post 1: {path}" in out and "alt text: Bar chart" in out
    assert fake_x.uploads == [] and fake_x.calls == []


def test_attach_images_false_posts_text_only(conn, fake_x, monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    did = seed_draft(conn, "a")
    seed_image(conn, did)
    cfg = tmp_path / "publish.yaml"
    cfg.write_text("timezone: America/New_York\nslots: ['08:30']\nmedia:\n  attach_images: false\n")
    assert run_publish.main(["--live", "--config", str(cfg)], now=SLOT_TIME) == 0
    assert fake_x.uploads == [] and [m for _, m in fake_x.media] == [None]


def test_upload_failure_posts_nothing(conn, monkeypatch, caplog, keep_failed):
    fx = FakeX(fail_upload=True)
    monkeypatch.setattr(client, "post_tweet", fx)
    monkeypatch.setattr(client, "upload_media", fx.upload)
    monkeypatch.setenv("BIO_DISCLOSURE_CONFIRMED", "1")
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    did = seed_draft(conn, "a")
    seed_image(conn, did)
    assert run_publish.main(["--live", "--format", "single", *keep_failed], now=SLOT_TIME) == 2
    assert fx.calls == []
    assert store.get_schedule(conn, did)["status"] == "failed"
    assert "media_upload" in store.get_schedule(conn, did)["error"]


def test_release_failed_reopens_failed_and_refused_only(
    conn, monkeypatch, caplog, capsys, keep_failed
):
    fx = FakeX(fail_at=1)
    monkeypatch.setattr(client, "post_tweet", fx)
    monkeypatch.setenv("BIO_DISCLOSURE_CONFIRMED", "1")
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    failed = seed_draft(conn, "a")
    args = ["--live", "--now", "--format", "single", *keep_failed]
    assert run_publish.main(args, now=OFF_SLOT) == 2
    assert store.get_schedule(conn, failed)["status"] == "failed"
    refused = seed_draft(conn, "b", edit_to="edited without the link")
    assert run_publish.main(["--live", "--now", "--format", "single"], now=OFF_SLOT) == 0
    assert store.get_schedule(conn, refused)["status"] == "refused"
    # neither is a candidate while claimed
    assert [a.draft_id for a in store.fetch_approved(10, conn=conn)] == []

    assert run_publish.main(["--release-failed"]) == 0
    out = capsys.readouterr().out
    assert f"released draft {failed}" in out and f"released draft {refused}" in out
    assert store.get_schedule(conn, failed) is None
    assert store.get_schedule(conn, refused) is None
    assert {a.draft_id for a in store.fetch_approved(10, conn=conn)} == {failed, refused}
    # the failed attempt stays in the posts log
    assert [r["status"] for r in store.list_posts(conn, failed)] == ["failed"]
    # nothing was posted by the release itself
    assert len(fx.calls) == 1


def test_release_failed_by_id_and_never_partial_or_posted(conn, monkeypatch, capsys, keep_failed):
    monkeypatch.setenv("BIO_DISCLOSURE_CONFIRMED", "1")
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    # partial thread: post 2 of a's thread fails
    fx = FakeX(fail_at=2)
    monkeypatch.setattr(client, "post_tweet", fx)
    partial = seed_draft(conn, "a")
    assert run_publish.main(["--live", "--now", "--format", "thread"], now=OFF_SLOT) == 2
    assert store.get_schedule(conn, partial)["status"] == "partial"
    # posted single
    fx2 = FakeX()
    monkeypatch.setattr(client, "post_tweet", fx2)
    posted = seed_draft(conn, "b")
    assert (
        run_publish.main(
            ["--live", "--now", "--format", "single"], now=OFF_SLOT + timedelta(hours=3)
        )
        == 0
    )
    assert store.get_schedule(conn, posted)["status"] == "posted"
    # two failed
    fx3 = FakeX(fail_at=1)
    monkeypatch.setattr(client, "post_tweet", fx3)
    f1 = seed_draft(conn, "c")
    assert (
        run_publish.main(
            ["--live", "--now", "--format", "single", *keep_failed],
            now=OFF_SLOT + timedelta(hours=6),
        )
        == 2
    )
    fx4 = FakeX(fail_at=1)
    monkeypatch.setattr(client, "post_tweet", fx4)
    f2 = seed_draft(conn, "d")
    assert (
        run_publish.main(
            ["--live", "--now", "--format", "single", *keep_failed],
            now=OFF_SLOT + timedelta(hours=9),
        )
        == 2
    )

    # asking for the partial, the posted and one failed releases only the failed one
    assert run_publish.main(["--release-failed", str(partial), str(posted), str(f1)]) == 0
    out = capsys.readouterr().out
    assert f"released draft {f1}" in out
    assert str(partial) not in out.replace(f"draft {f1}", "")
    assert store.get_schedule(conn, partial)["status"] == "partial"
    assert store.get_schedule(conn, posted)["status"] == "posted"
    assert store.get_schedule(conn, f1) is None
    assert store.get_schedule(conn, f2)["status"] == "failed"

    # nothing eligible among the given ids
    assert run_publish.main(["--release-failed", str(partial)]) == 0
    assert "nothing to release" in capsys.readouterr().out
    assert store.get_schedule(conn, partial)["status"] == "partial"


def test_post_tweet_uses_v2_tweets_on_api_x_com(monkeypatch):
    calls = _fake_tweepy_for_upload(
        monkeypatch,
        [_FakeResponse(201, {"data": {"id": "42", "text": "hi"}})],
    )
    assert client.post_tweet("hi", in_reply_to="41", media_ids=["777"]) == "42"
    method, url, kw = calls[0]
    assert (method, url) == ("POST", "https://api.x.com/2/tweets")
    assert kw["auth"] == "oauth1-signature"
    assert kw["json"] == {
        "text": "hi",
        "reply": {"in_reply_to_tweet_id": "41"},
        "media": {"media_ids": ["777"]},
    }
    monkeypatch.delitem(sys.modules, "tweepy")


def test_post_tweet_plain_body_and_verify_credentials(monkeypatch):
    calls = _fake_tweepy_for_upload(
        monkeypatch,
        [
            _FakeResponse(201, {"data": {"id": "1"}}),
            _FakeResponse(200, {"data": {"id": "9", "username": "biotech_leads"}}),
        ],
    )
    assert client.post_tweet("solo") == "1"
    assert calls[0][2]["json"] == {"text": "solo"}
    assert client.verify_credentials() == "biotech_leads"
    assert (calls[1][0], calls[1][1]) == ("GET", "https://api.x.com/2/users/me")
    assert not any("api.twitter.com" in u for _, u, _ in calls)
    monkeypatch.delitem(sys.modules, "tweepy")


def test_failed_attempt_is_released_automatically_until_the_cap(conn, monkeypatch, caplog):
    monkeypatch.setenv("BIO_DISCLOSURE_CONFIRMED", "1")
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    did = seed_draft(conn, "a")
    for attempt in (1, 2):
        fx = FakeX(fail_at=1)
        monkeypatch.setattr(client, "post_tweet", fx)
        assert run_publish.main(["--live", "--now", "--format", "single"], now=OFF_SLOT) == 2
        # released: no claim row, approved again, the failed attempt logged
        assert store.get_schedule(conn, did) is None
        assert [a.draft_id for a in store.fetch_approved(10, conn=conn)] == [did]
        assert store.failed_attempts(conn, did) == attempt
        assert f"released back to approved after failed attempt {attempt}/3" in caplog.text
    # third failure hits max_attempts: stays failed for a human
    fx = FakeX(fail_at=1)
    monkeypatch.setattr(client, "post_tweet", fx)
    assert run_publish.main(["--live", "--now", "--format", "single"], now=OFF_SLOT) == 2
    assert store.get_schedule(conn, did)["status"] == "failed"
    assert store.fetch_approved(10, conn=conn) == []
    assert "3 failed attempts, staying failed" in caplog.text
    # a later success after a manual release counts the earlier failures, posts fine
    assert run_publish.main(["--release-failed", str(did)]) == 0
    ok = FakeX()
    monkeypatch.setattr(client, "post_tweet", ok)
    assert run_publish.main(["--live", "--now", "--format", "single"], now=OFF_SLOT) == 0
    assert store.get_schedule(conn, did)["status"] == "posted"


def test_partial_and_refused_are_not_released_automatically(conn, monkeypatch):
    monkeypatch.setenv("BIO_DISCLOSURE_CONFIRMED", "1")
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    fx = FakeX(fail_at=2)
    monkeypatch.setattr(client, "post_tweet", fx)
    partial = seed_draft(conn, "a")
    assert run_publish.main(["--live", "--now", "--format", "thread"], now=OFF_SLOT) == 2
    assert store.get_schedule(conn, partial)["status"] == "partial"
    refused = seed_draft(conn, "b", edit_to="edited without the link")
    later = OFF_SLOT + timedelta(hours=3)
    assert run_publish.main(["--live", "--now", "--format", "single"], now=later) == 0
    assert store.get_schedule(conn, refused)["status"] == "refused"
    assert store.fetch_approved(10, conn=conn) == []


def test_auto_release_can_be_turned_off(conn, monkeypatch, tmp_path):
    monkeypatch.setenv("BIO_DISCLOSURE_CONFIRMED", "1")
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    cfg = tmp_path / "publish.yaml"
    cfg.write_text("timezone: America/New_York\nretry:\n  auto_release_failed: false\n")
    fx = FakeX(fail_at=1)
    monkeypatch.setattr(client, "post_tweet", fx)
    did = seed_draft(conn, "a")
    args = ["--live", "--now", "--format", "single", "--config", str(cfg)]
    assert run_publish.main(args, now=OFF_SLOT) == 2
    assert store.get_schedule(conn, did)["status"] == "failed"
    assert store.fetch_approved(10, conn=conn) == []
