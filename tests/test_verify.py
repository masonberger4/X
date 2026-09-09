"""Step 2b (claim verification): pure parsing/trust logic, the store, the CLI with a fake
model call, the queue integration, and the CLI wrapper's tool flags. No network."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import claude_cli
import run_verify
from approval_queue import store
from approval_queue.app import app
from draft.schema import Claim, Draft
from tests.conftest import URL, seed_item
from verify import store as vstore
from verify import verifier
from verify.settings import load_verify_config

CFG = {
    "model": "m",
    "effort": "low",
    "max_claims_per_draft": 6,
    "trusted_domains": ["clinicaltrials.gov", "sec.gov"],
}
ROOT = {"companies": {"feeds": {"acme": {"url": "https://ir.acme-bio.com/rss"}}}}


def test_parse_reply_accepts_prose_around_json_and_rejects_bad_verdicts():
    out = verifier.parse_reply(
        "Here you go:\n"
        '{"verdict": "Supported", "source_url": "https://x", "quote": "q", "note": "n"}'
    )
    assert out == {"verdict": "supported", "source_url": "https://x", "quote": "q", "note": "n"}
    with pytest.raises(ValueError, match="verdict"):
        verifier.parse_reply('{"verdict": "maybe"}')
    with pytest.raises(ValueError, match="no JSON"):
        verifier.parse_reply("I could not find anything.")


def test_trusted_hosts_include_config_domains_and_company_feed_hosts():
    hosts = verifier.trusted_hosts(CFG, ROOT)
    assert hosts == {"clinicaltrials.gov", "sec.gov", "ir.acme-bio.com"}
    assert verifier.is_trusted("https://www.clinicaltrials.gov/study/NCT1", hosts)
    assert verifier.is_trusted("https://ir.acme-bio.com/news/x", hosts)
    assert not verifier.is_trusted("https://clinicaltrials.gov.evil.com/", hosts)
    assert not verifier.is_trusted("https://news.example.com/", hosts)
    assert not verifier.is_trusted("", hosts)


def test_verify_claim_marks_trust_and_passes_context(monkeypatch):
    seen = {}

    def call(system, user, model, effort, cfg, root_cfg):
        seen.update(system=system, user=user, model=model, effort=effort)
        return json.dumps(
            {
                "verdict": "supported",
                "source_url": "https://clinicaltrials.gov/study/NCT1",
                "quote": "Phase 2, n=40.",
                "note": "ok",
            }
        )

    hosts = verifier.trusted_hosts(CFG, ROOT)
    c = verifier.verify_claim(
        2,
        "the trial is phase 2",
        title="T",
        url=URL,
        single_post="post",
        published_at=None,
        cfg=CFG,
        root_cfg=ROOT,
        hosts=hosts,
        call=call,
    )
    assert c.claim_index == 2 and c.verdict == "supported" and c.trusted
    assert "the trial is phase 2" in seen["user"] and URL in seen["user"]
    assert seen["model"] == "m" and seen["effort"] == "low"
    assert "PRIMARY" in seen["system"]

    c2 = verifier.verify_claim(
        0,
        "x",
        title="T",
        url=URL,
        single_post="p",
        published_at=None,
        cfg=CFG,
        root_cfg=ROOT,
        hosts=hosts,
        call=lambda *a: json.dumps({"verdict": "contradicted", "source_url": "https://blog.io/x"}),
    )
    assert c2.verdict == "contradicted" and not c2.trusted


def test_call_model_uses_claude_code_with_web_tools(monkeypatch):
    seen = {}

    def fake_run(user, *, system, model, cfg, effort, tools):
        seen.update(cfg=cfg, tools=tools, effort=effort)
        return '{"verdict": "unverified"}'

    monkeypatch.setattr(claude_cli, "run_claude", fake_run)
    out = verifier.call_model(
        "S", "U", "m", "low", {"timeout_seconds": 99}, {"models": {"backend": "claude_code"}}
    )
    assert out == '{"verdict": "unverified"}'
    assert seen["tools"] == ["WebSearch", "WebFetch"]
    assert seen["cfg"]["claude_code"]["timeout_seconds"] == 99


def test_build_argv_enables_and_preapproves_named_tools():
    settings = claude_cli.cli_settings({})
    argv = claude_cli.build_argv("claude", "m", None, settings, None, ["WebSearch", "WebFetch"])
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    plain = claude_cli.build_argv("claude", "m", None, settings)
    assert plain[plain.index("--tools") + 1] == "" and "--allowedTools" not in plain


def test_shipped_verify_config_is_sane():
    cfg = load_verify_config()
    assert cfg["model"].startswith("claude-")
    assert "clinicaltrials.gov" in cfg["trusted_domains"]
    assert cfg["max_claims_per_draft"] >= 1


def _draft_with_claims(conn, item_id="i1", n=2):
    seed_item(conn, item_id)
    d = Draft(
        single_post=f"post {URL}",
        thread=["a", "b", f"c {URL}"],
        suggested_visual="",
        why_it_matters="w",
        claims_to_verify=[Claim(f"claim {i}", "medium") for i in range(n)],
    )
    return store.insert_draft(conn, item_id=item_id, model="m", draft=d)


def test_store_upserts_per_claim_and_detects_contradiction(conn):
    did = _draft_with_claims(conn)
    vstore.ensure_schema(conn)
    assert vstore.checks_for_draft(conn, did) == []
    assert vstore.unchecked_indexes(conn, store.get_draft(conn, did)) == [0, 1]
    vstore.insert_check(
        conn, did, verifier.ClaimCheck(0, "claim 0", "supported", "https://s", "q", "n", True), "m"
    )
    assert vstore.unchecked_indexes(conn, store.get_draft(conn, did)) == [1]
    assert not vstore.has_contradiction(conn, did)
    vstore.insert_check(
        conn,
        did,
        verifier.ClaimCheck(0, "claim 0", "contradicted", "https://s", "q", "n", True),
        "m",
    )
    rows = vstore.checks_for_draft(conn, did)
    assert len(rows) == 1 and rows[0].verdict == "contradicted" and rows[0].label == "contradicted"
    assert vstore.has_contradiction(conn, did)
    vstore.delete_checks(conn, did)
    assert vstore.checks_for_draft(conn, did) == []


def test_run_verify_checks_unchecked_claims_and_survives_one_failure(conn, monkeypatch):
    did = _draft_with_claims(conn, n=3)
    calls = []

    def fake_verify(index, claim, **kw):
        calls.append(index)
        if index == 1:
            raise RuntimeError("boom")
        return verifier.ClaimCheck(index, claim, "supported", "https://sec.gov/x", "q", "n", True)

    monkeypatch.setattr(run_verify, "verify_claim", fake_verify)
    monkeypatch.setattr(run_verify, "_root_config", lambda: ROOT)
    assert run_verify.main([]) == 0
    assert calls == [0, 1, 2]
    rows = vstore.checks_for_draft(conn, did)
    assert [r.claim_index for r in rows] == [0, 2]

    # a plain rerun only retries the failed one; --redo does all three
    calls.clear()
    run_verify.main([])
    assert calls == [1]
    calls.clear()
    run_verify.main(["--redo"])
    assert calls == [0, 1, 2]
    assert [r.claim_index for r in vstore.checks_for_draft(conn, did)] == [0, 2]

    # dry run makes no calls
    calls.clear()
    run_verify.main(["--dry-run", "--redo"])
    assert calls == []


def test_run_verify_respects_limit_and_max_claims(conn, monkeypatch):
    a = _draft_with_claims(conn, "a", n=8)
    _draft_with_claims(conn, "b", n=1)
    calls = []
    monkeypatch.setattr(
        run_verify,
        "verify_claim",
        lambda i, c, **kw: (
            calls.append(i) or verifier.ClaimCheck(i, c, "unverified", "", "", "", False)
        ),
    )
    monkeypatch.setattr(run_verify, "_root_config", lambda: {})
    run_verify.main(["--draft", str(a)])
    assert calls == list(range(6))  # max_claims_per_draft from the shipped config
    calls.clear()
    run_verify.main(["--limit", "1"])
    assert len(calls) == 1  # draft b, the only one with unchecked claims left


@pytest.fixture
def client(db_file):
    return TestClient(app, follow_redirects=False)


def test_queue_shows_verdicts_and_blocks_approve_on_contradiction(client, conn):
    did = _draft_with_claims(conn)
    vstore.insert_check(
        conn,
        did,
        verifier.ClaimCheck(
            0, "claim 0", "contradicted", "https://sec.gov/f", "The deal closed in May.", "n", True
        ),
        "m",
    )
    body = client.get(f"/drafts/{did}").text
    assert (
        "contradicted" in body and "https://sec.gov/f" in body and "The deal closed in May." in body
    )
    assert "not checked yet" in body  # claim 1
    assert "approve anyway" in body
    listing = client.get("/queue").text
    assert "1 contradicted" in listing and "1 unchecked" in listing

    r = client.post(f"/drafts/{did}/approve")
    assert r.status_code == 409
    assert store.get_draft(conn, did).status == "pending"
    r = client.post(f"/drafts/{did}/approve", data={"override": "1"})
    assert r.status_code == 303
    assert store.get_draft(conn, did).status == "approved"


def test_queue_approve_is_unblocked_when_supported(client, conn):
    did = _draft_with_claims(conn, n=1)
    vstore.insert_check(
        conn, did, verifier.ClaimCheck(0, "claim 0", "supported", "https://s", "q", "n", False), "m"
    )
    body = client.get(f"/drafts/{did}").text
    assert "supported (untrusted source)" in body and "approve anyway" not in body
    assert client.post(f"/drafts/{did}/approve").status_code == 303
