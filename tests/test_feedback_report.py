"""Report rendering (pure) and run_feedback end to end against a temp SQLite file.

Step 1 tables come from db.py's init, step 2 from approval_queue.store's init, the step 3
posts table is created by hand, and step 4's tables by feedback.store. The X client is
monkeypatched; nothing touches the network.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

import run_feedback
from approval_queue import store as qstore
from db import Database
from draft.schema import Draft
from feedback import analysis as an
from feedback import client, report, store
from feedback.models import Metrics, TweetMetrics, UserMetrics
from feedback.suggest import Suggestion
from tests.conftest import seed_item
from tests.test_feedback_store import POSTS_DDL

NOW = datetime(2026, 6, 8, 15, 0, tzinfo=UTC)


# ---- pure rendering ----------------------------------------------------------------


def empty_analysis():
    return an.analyse([], {}, [], [], window_start=NOW - timedelta(days=7), window_end=NOW)


def test_render_empty_state_and_no_suggestions_cleanly():
    md = report.render(empty_analysis(), [], min_posts=5)
    assert md.startswith("# Feedback report 2026-06-01 to 2026-06-08")
    assert "Posts with metrics: **0**" in md
    assert "delta over window: n/a" in md
    assert "_No posts in the window._" in md
    assert "_No suggestions." in md
    assert "## Caveats" in md and "not exposed by the API" in md
    assert "medical advice" in md  # the caveat says the report contains none


def test_render_suggestions_and_tables():
    rows = [
        an.PostRow(
            draft_id=i,
            tweet_id=str(i),
            posted_at=NOW - timedelta(days=i),
            kind="thread",
            slot="08:30",
            hour=8,
            source="pubmed",
            evidence_level="phase2",
            edited=True,
            topics=["adc"],
            scores={"total": 30 + i, "novelty": 5},
            rating=4,
            title="T | pipe",
            url="u",
            suggested_angle="A",
            head=Metrics(impressions=1000 * (i + 1), likes=10),
            reply_sum=Metrics(impressions=200),
            reply_count=2,
        )
        for i in range(3)
    ]
    a = an.analyse(
        [],
        {},
        [],
        [],
        window_start=NOW - timedelta(days=7),
        window_end=NOW,
        row_builder=lambda *args, **kw: rows,
    )
    s = Suggestion("slot", "publish/config.yaml: slots", "Move it.", "n=5 vs n=5")
    md = report.render(a, [s], min_posts=2)
    assert "| pubmed | 3 | 2,000 | 2,000.0 | 0.50% |  |" in md
    assert "| adc | 3 |" in md
    assert "| total | 3 | +1.00 |  |" in md
    assert "| novelty | 3 | - |  |" in md  # no variance -> rho None
    assert "| 3,000 | 0.33% | 200 / 2 | pubmed | thread | 32 | 4 | T \\| pipe | A |" in md
    assert "1. **[slot]** `publish/config.yaml: slots`" in md
    assert "Evidence: n=5 vs n=5" in md


def test_render_flags_small_groups():
    rows = [
        an.PostRow(
            draft_id=1,
            tweet_id="1",
            posted_at=NOW,
            kind="single",
            slot="08:30",
            hour=8,
            source="fda",
            evidence_level="approval",
            edited=False,
            topics=[],
            scores={},
            rating=None,
            title="t",
            url="u",
            suggested_angle="",
            head=Metrics(impressions=5),
        )
    ]
    a = an.analyse(
        [],
        {},
        [],
        [],
        window_start=NOW - timedelta(days=7),
        window_end=NOW,
        row_builder=lambda *args, **kw: rows,
    )
    md = report.render(a, [], min_posts=5)
    assert "| fda | 1 | 5 | 5.0 | 0.00% | small-n |" in md


# ---- end to end ----------------------------------------------------------------


def seed_post(
    conn,
    item_id,
    draft_n,
    *,
    source,
    tweets,
    kind="single",
    posted_at,
    rating=None,
    total=40,
    edit=False,
    slot="2026-06-02 08:30",
):
    """Seed items/clusters/scores(/ratings)/drafts/decisions and one or more posts rows."""
    cid = seed_item(conn, item_id, source=source, total=total)
    d = Draft(
        single_post=f"Post {item_id}", thread=["a", "b"], suggested_visual="", why_it_matters=""
    )
    did = qstore.insert_draft(conn, item_id=item_id, cluster_id=cid, model="m", draft=d)
    if edit:
        qstore.edit(conn, did, single_post=f"Edited {item_id}", thread=["a", "b"])
    else:
        qstore.approve(conn, did)
    if rating is not None:
        conn.execute(
            "INSERT INTO ratings (cluster_id, rating, note, rated_at) VALUES (?, ?, '', ?)",
            (cid, rating, posted_at.isoformat()),
        )
    conn.execute(POSTS_DDL)
    for pos, tid in enumerate(tweets, start=1):
        conn.execute(
            "INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, slot,"
            " status, error) VALUES (?, ?, 'x', ?, ?, ?, ?, 'posted', NULL)",
            (did, tid, kind, pos, (posted_at + timedelta(minutes=pos)).isoformat(), slot),
        )
    conn.commit()
    return did


@pytest.fixture
def seeded(db_file):
    """Real step 1 + step 2 schema, hand-made posts, and step 4 tables, with 6 posts."""
    Database(str(db_file)).close()
    conn = qstore.connect(db_file)
    store.connect(db_file).close()
    day = NOW - timedelta(days=3)
    seed_post(conn, "p1", 1, source="pubmed", tweets=["t1"], posted_at=day, rating=5, total=45)
    seed_post(conn, "p2", 2, source="pubmed", tweets=["t2"], posted_at=day, rating=3, total=35)
    seed_post(conn, "p3", 3, source="pubmed", tweets=["t3"], posted_at=day, total=30, edit=True)
    seed_post(
        conn,
        "f1",
        4,
        source="fda_press",
        tweets=["t4", "t4b"],
        kind="thread",
        posted_at=day,
        rating=4,
        total=42,
        slot=None,
    )
    seed_post(conn, "f2", 5, source="fda_press", tweets=["t5"], posted_at=day, total=38)
    seed_post(conn, "gone", 6, source="fda_press", tweets=["t6"], posted_at=day, total=20)
    # an old post outside any 1-week window, and a failed post: never fetched
    seed_post(conn, "old", 7, source="pubmed", tweets=["t7"], posted_at=NOW - timedelta(days=200))
    conn.execute(
        "INSERT INTO posts (draft_id, tweet_id, text, kind, position, posted_at, slot, status,"
        " error) VALUES (1, NULL, 'x', 'single', 1, NULL, NULL, 'failed', 'HTTP 500')"
    )
    conn.commit()
    conn.close()
    return db_file


METRICS = {
    "t1": Metrics(impressions=5000, likes=50, reposts=5, replies=2, quotes=1, bookmarks=9),
    "t2": Metrics(impressions=3000, likes=20),
    "t3": Metrics(impressions=2000, likes=10),
    "t4": Metrics(impressions=900, likes=9),
    "t4b": Metrics(impressions=300, likes=3),
    "t5": Metrics(impressions=700, likes=7),
}


class FakeClient:
    def __init__(self):
        self.tweet_calls: list[list[str]] = []
        self.user_calls: list[str] = []
        self.followers = 100

    def get_tweet_metrics(self, ids, cfg=None):
        ids = list(ids)
        self.tweet_calls.append(ids)
        out = {}
        for tid in ids:
            if tid in METRICS:
                out[tid] = TweetMetrics(tid, METRICS[tid])
            else:
                out[tid] = TweetMetrics(tid, deleted=True)
        return out

    def get_user_metrics(self, username, cfg=None):
        self.user_calls.append(username)
        return UserMetrics(username, self.followers, 10, 50)


@pytest.fixture
def fake_client(monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(client, "get_tweet_metrics", fc.get_tweet_metrics)
    monkeypatch.setattr(client, "get_user_metrics", fc.get_user_metrics)
    return fc


@pytest.fixture
def cfg_file(tmp_path):
    cfg = run_feedback.load_feedback_config()
    cfg["username"] = "oncwatch"
    cfg["report"]["min_posts_per_group"] = 3
    cfg["snapshot"]["daily_for_days"] = 5
    p = tmp_path / "feedback.yaml"
    p.write_text(json.dumps(cfg))  # JSON is valid YAML
    return str(p)


def test_snapshot_then_report_end_to_end(seeded, fake_client, cfg_file, capsys, tmp_path):
    # Day 0 snapshot: 7 live tweets in the window are due (t7 is > stop_after_days old).
    assert run_feedback.main(["--config", cfg_file, "snapshot"], now=NOW) == 0
    assert sorted(fake_client.tweet_calls[0]) == ["t1", "t2", "t3", "t4", "t4b", "t5", "t6"]
    assert fake_client.user_calls == ["oncwatch"]
    # Same day again: idempotent, nothing fetched.
    assert run_feedback.main(["--config", cfg_file, "snapshot"], now=NOW + timedelta(hours=3)) == 0
    assert len(fake_client.tweet_calls) == 1 and len(fake_client.user_calls) == 1
    # Next day: the deleted tweet is never fetched again; followers snapshot once more.
    fake_client.followers = 130
    assert run_feedback.main(["--config", cfg_file, "snapshot"], now=NOW + timedelta(days=1)) == 0
    assert "t6" not in fake_client.tweet_calls[1] and len(fake_client.tweet_calls[1]) == 6
    conn = store.connect(seeded)
    t6 = store.fetch_tweet_snapshots(conn, ["t6"])
    assert len(t6) == 1 and t6[0].deleted
    conn.close()

    out = tmp_path / "report.md"
    rc = run_feedback.main(
        ["--config", cfg_file, "report", "--weeks", "1", "--out", str(out)],
        now=NOW + timedelta(days=1),
    )
    assert rc == 0
    md = out.read_text()
    assert "Posts with metrics: **5** (1 threads)" in md
    # group rows: pubmed n=3, fda_press n=2 (small-n at min 3)
    assert "| pubmed | 3 | 3,000 | 3,333.3 |" in md
    assert "| fda_press | 2 | 800 | 800.0 |" in md and "small-n" in md
    assert "| thread | 1 |" in md and "| single | 4 |" in md
    assert "| edited | 1 |" in md and "| unedited | 4 |" in md
    assert "| off-slot | 1 |" in md and "| 08:30 | 4 |" in md
    # thread row shows head + reply sum separately
    assert "| 900 | 1.00% | 300 / 1 | fda_press | thread | 42 | 4 |" in md
    assert "| rating | 3 |" in md
    assert "delta over window: +30 (from 100 on 2026-06-08)" in md
    assert "Followers: 130" in md
    assert "## Caveats" in md
    # small-n warnings are logged, not fatal
    conn = store.connect(seeded)
    rows = store.list_reports(conn)
    conn.close()
    assert len(rows) == 1 and rows[0]["report_md"] == md
    assert json.loads(rows[0]["suggestions_json"]) == []  # nothing clear-cut at n=3 vs n=2

    # followers subcommand prints the series
    assert run_feedback.main(["--config", cfg_file, "followers"], now=NOW) == 0
    captured = capsys.readouterr().out
    assert "2026-06-08\t100\t-" in captured and "2026-06-09\t130\t+30" in captured


def test_snapshot_dry_run_fetches_nothing(seeded, fake_client, cfg_file, capsys):
    assert run_feedback.main(["--config", cfg_file, "snapshot", "--dry-run"], now=NOW) == 0
    out = capsys.readouterr().out
    assert "DRY RUN: 7 tweet(s) would be fetched" in out and "followers\t@oncwatch" in out
    assert fake_client.tweet_calls == [] and fake_client.user_calls == []
    conn = store.connect(seeded)
    assert store.fetch_tweet_snapshots(conn) == []
    conn.close()


def test_snapshot_all_ignores_schedule_but_not_deleted(seeded, fake_client, cfg_file):
    assert run_feedback.main(["--config", cfg_file, "snapshot"], now=NOW) == 0
    day5 = NOW + timedelta(days=5)  # weekly phase: nothing due normally
    assert run_feedback.main(["--config", cfg_file, "snapshot"], now=day5) == 0
    assert len(fake_client.tweet_calls) == 1
    assert run_feedback.main(["--config", cfg_file, "snapshot", "--all"], now=day5) == 0
    assert sorted(fake_client.tweet_calls[1]) == ["t1", "t2", "t3", "t4", "t4b", "t5"]


def test_snapshot_api_failure_is_logged_and_nonzero(seeded, cfg_file, monkeypatch, caplog):
    def boom(ids, cfg=None):
        raise client.FeedbackAPIError("HTTP 401 from X API", status=401)

    monkeypatch.setattr(client, "get_tweet_metrics", boom)
    monkeypatch.setattr(client, "get_user_metrics", boom)
    assert run_feedback.main(["--config", cfg_file, "snapshot"], now=NOW) == 1
    assert "tweet metrics fetch failed" in caplog.text
    conn = store.connect(seeded)
    assert store.fetch_tweet_snapshots(conn) == []
    conn.close()


def test_report_without_step3_table(db_file, cfg_file, capsys):
    Database(str(db_file)).close()
    qstore.connect(db_file).close()
    assert run_feedback.main(["--config", cfg_file, "report"], now=NOW) == 0
    assert "Posts with metrics: **0**" in capsys.readouterr().out
