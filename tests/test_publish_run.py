"""End-to-end run_publish tests against a temp SQLite file seeded via step 1/2's real schema.

client.post_tweet is monkeypatched everywhere; tweepy is never imported.
"""

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import run_publish
from approval_queue import store as qstore
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
    def __init__(self, fail_at: int | None = None):
        self.calls: list[tuple[str, str | None]] = []
        self.fail_at = fail_at

    def __call__(self, text, in_reply_to=None):
        self.calls.append((text, in_reply_to))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise client.PublishError("HTTP 500", retryable=True, status=500)
        return f"tw{len(self.calls)}"


@pytest.fixture
def fake_x(monkeypatch):
    fx = FakeX()
    monkeypatch.setattr(client, "post_tweet", fx)
    monkeypatch.setenv("BIO_DISCLOSURE_CONFIRMED", "1")
    monkeypatch.delenv("PUBLISH_ENABLED", raising=False)
    return fx


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
    assert run_publish.main(["--live"], now=SLOT_TIME) == 0
    assert fake_x.calls == []
    monkeypatch.setenv("PUBLISH_ENABLED", "0")
    assert run_publish.main(["--live"], now=SLOT_TIME) == 0
    assert fake_x.calls == []
    # env alone (no --live) is not enough either
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    assert run_publish.main([], now=SLOT_TIME) == 0
    assert fake_x.calls == []


def test_live_posts_single_and_is_idempotent(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a")
    assert run_publish.main(["--live"], now=SLOT_TIME) == 0
    assert len(fake_x.calls) == 1 and fake_x.calls[0] == (f"Post a {URL}", None)
    rows = store.list_posts(conn, 1)
    assert rows[0]["tweet_id"] == "tw1" and rows[0]["slot"] == "2026-06-01 08:30"
    assert store.get_schedule(conn, 1)["status"] == "posted"
    # cron fires again in the same slot window: same draft is never re-posted
    assert run_publish.main(["--live"], now=SLOT_TIME) == 0
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
    assert run_publish.main(["--live"], now=OFF_SLOT) == 0
    assert fake_x.calls == []
    assert "next slot 2026-06-01 12:15" in caplog.text


def test_now_flag_ignores_slots(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a")
    assert run_publish.main(["--live", "--now"], now=OFF_SLOT) == 0
    assert len(fake_x.calls) == 1


def test_breaking_posts_outside_slots_but_respects_gap(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    seed_draft(conn, "a")  # regular
    seed_draft(conn, "b", source="fda_oce_approvals")
    assert run_publish.main(["--live", "--breaking"], now=OFF_SLOT) == 0
    assert fake_x.calls == [(f"Post b {URL}", None)]
    # a second breaking item minutes later is blocked by min_gap
    seed_draft(conn, "c", source="fda_press")
    assert run_publish.main(["--live"], now=OFF_SLOT) == 0
    assert len(fake_x.calls) == 1
    assert store.get_schedule(conn, 3) is None


def test_daily_cap(conn, fake_x, monkeypatch):
    monkeypatch.setenv("PUBLISH_ENABLED", "1")
    for i in range(4):
        seed_draft(conn, f"d{i}", source="fda_press")
    times = [datetime(2026, 6, 1, h, 0, tzinfo=NY) for h in (6, 9, 12, 15, 18)]
    for t in times:
        run_publish.main(["--live"], now=t)
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
    assert run_publish.main(["--live"], now=SLOT_TIME) == 0
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

    fake = types.SimpleNamespace(
        TooManyRequests=TooManyRequests,
        TwitterServerError=TwitterServerError,
        Unauthorized=Unauthorized,
        Forbidden=Forbidden,
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
