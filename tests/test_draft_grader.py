"""The image grader: parsing, knob clamping, the render-grade loop, storage and the queue."""

import json

import pytest

from approval_queue import images, store
from draft import grader
from draft.chart import Chart, Style
from draft.schema import Draft
from tests.conftest import URL, seed_item

pytest.importorskip("matplotlib")

REAL_CALL_GRADER = grader.call_grader  # captured before conftest's autouse stub replaces it

CHART = Chart("Phase 2 outcomes", ["ORR", "Median PFS"], [88.0, 14.6], "", "n=97")


def _draft(chart=None):
    return Draft(f"ORR 88% {URL}", ["a", "b", f"c {URL}"], "v", "w", chart=chart)


def _cfg(**grader_cfg):
    g = {"enabled": True, "model": "grader-model", "min_score": 8, "max_iterations": 4}
    g.update(grader_cfg)
    return {"images": {"enabled": True, "grader": g}}


# --- pure parts --------------------------------------------------------------------------


def test_style_apply_clamps_and_ignores_unknown_keys():
    st = Style().apply(
        {"font_scale": 9, "label_wrap": "12", "highlight_first": 1, "bogus": 3, "track": None}
    )
    assert st.font_scale == 1.6 and st.label_wrap == 16 and st.highlight_first is True
    assert st.track is True and Style().apply(None) == Style()
    assert Style().apply({"row_pitch": "x"}) == Style()  # a non-number is ignored


def test_parse_grade_reads_fenced_json_and_clamps():
    text = (
        '```json\n{"score": 6.4, "flaws": ["too much white"], "fixes": ["raise row_pitch"], '
        '"adjustments": {"row_pitch": 0.5, "title": "NEW"}}\n```'
    )
    g = grader.parse_grade(text, Style(), model="m", iteration=2)
    assert g.score == 6 and g.flaws == ["too much white"] and g.fixes == ["raise row_pitch"]
    assert g.adjustments == {"row_pitch": 0.16} and g.model == "m" and g.iteration == 2
    assert not g.passed(8) and grader.parse_grade('{"score": 11}', Style()).score == 10
    with pytest.raises(grader.GraderError):
        grader.parse_grade("no json here", Style())
    with pytest.raises(grader.GraderError):
        grader.parse_grade('{"flaws": []}', Style())


def test_parse_grade_drops_adjustments_that_change_nothing():
    g = grader.parse_grade('{"score": 5, "adjustments": {"gridlines": true}}', Style())
    assert g.adjustments == {}


def test_user_prompt_carries_spec_knobs_and_previous_grade():
    prev = grader.ImageGrade(score=5, flaws=["cramped"])
    text = grader.build_user_prompt(CHART, Style(), prev)
    assert "Phase 2 outcomes" in text and '"row_pitch"' in text and "5/10" in text
    assert "cramped" in text and "bar chart" in text
    assert "the renderer will not change it" in grader.SYSTEM_PROMPT


def test_grader_settings_resolution(monkeypatch):
    s = grader.grader_settings(_cfg())
    assert s.enabled and s.model == "grader-model" and s.min_score == 8 and s.max_iterations == 4
    assert not grader.grader_settings(_cfg(enabled=False)).enabled
    assert not grader.grader_settings(_cfg(model="")).enabled  # no model, no grader
    monkeypatch.setenv("IMAGE_GRADER_MODEL", "env-model")
    assert grader.grader_settings(_cfg()).model == "env-model"
    assert grader.grader_settings(_cfg(max_iterations=0)).max_iterations == 1
    real = grader.grader_settings()  # the shipped draft/config.yaml
    assert real.min_score == 8 and real.max_iterations == 4 and real.model


# --- the loop ----------------------------------------------------------------------------


class FakeGrader:
    """Scripted replies; records the styles it was shown."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.styles = []
        self.previous = []

    def __call__(self, path, visual, style, *, model, iteration, previous):
        self.styles.append(style)
        self.previous.append(previous)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return grader.parse_grade(json.dumps(reply), style, model=model, iteration=iteration)


def _seeded(conn):
    seed_item(conn, "i1")
    return store.insert_draft(conn, item_id="i1", model="m", draft=_draft(CHART))


def test_loop_stops_at_min_score(conn, monkeypatch, tmp_path):
    did = _seeded(conn)
    fake = FakeGrader([{"score": 9, "flaws": [], "fixes": []}])
    monkeypatch.setattr(grader, "grade_image", fake)
    path = images.attach_chart(conn, did, CHART, source_url=URL, cfg=_cfg())
    assert path is not None and path.is_file()
    grades = store.list_image_grades(conn, did)
    assert [g.score for g in grades] == [9] and grades[0].kept and grades[0].iteration == 1
    assert grades[0].model == "grader-model" and grades[0].style == Style().to_dict()
    assert fake.previous == [None]


def test_loop_iterates_with_adjustments_and_keeps_the_best(conn, monkeypatch):
    did = _seeded(conn)
    fake = FakeGrader(
        [
            {"score": 5, "flaws": ["white"], "adjustments": {"row_pitch": 0.14}},
            {"score": 7, "flaws": ["small"], "adjustments": {"font_scale": 1.3}},
            {"score": 6, "flaws": ["worse"], "adjustments": {"bar_height": 0.7}},
            {"score": 4, "flaws": ["bad"], "adjustments": {"gridlines": False}},
        ]
    )
    monkeypatch.setattr(grader, "grade_image", fake)
    draws = []
    real = images.render_chart

    def spy(chart, path, **kw):
        draws.append(kw["style"])
        return real(chart, path, **kw)

    monkeypatch.setattr(images, "render_chart", spy)
    images.attach_chart(conn, did, CHART, source_url=URL, cfg=_cfg())
    grades = store.list_image_grades(conn, did)
    assert [g.score for g in grades] == [5, 7, 6, 4]  # max_iterations reached
    assert [g.kept for g in grades] == [False, True, False, False]
    assert grades[1].adjustments == {"font_scale": 1.3}
    # the grader saw the adjusted styles in sequence, with the previous grade each time
    assert fake.styles[1].row_pitch == 0.14 and fake.styles[2].font_scale == 1.3
    assert fake.previous[1].score == 5 and fake.previous[3].score == 6
    # 1 first draw + 3 redraws + 1 final redraw of the best (render 2's style)
    assert len(draws) == 5 and draws[-1] == fake.styles[1] and draws[-1].row_pitch == 0.14


def test_loop_ends_when_grader_offers_no_change(conn, monkeypatch):
    did = _seeded(conn)
    fake = FakeGrader([{"score": 6, "flaws": ["meh"]}, {"score": 9}])
    monkeypatch.setattr(grader, "grade_image", fake)
    images.attach_chart(conn, did, CHART, source_url=URL, cfg=_cfg())
    grades = store.list_image_grades(conn, did)
    assert [g.score for g in grades] == [6] and grades[0].kept and fake.replies  # no 2nd call


def test_loop_fails_soft_and_keeps_the_render(conn, monkeypatch, caplog):
    did = _seeded(conn)
    fake = FakeGrader(
        [{"score": 3, "adjustments": {"row_pitch": 0.12}}, RuntimeError("model down")]
    )
    monkeypatch.setattr(grader, "grade_image", fake)
    path = images.attach_chart(conn, did, CHART, source_url=URL, cfg=_cfg())
    assert path is not None and store.get_draft(conn, did).image_path == path.name
    assert "image grader failed" in caplog.text
    grades = store.list_image_grades(conn, did)
    assert [g.score for g in grades] == [3] and grades[0].kept


def test_default_test_stub_keeps_render_without_network(conn, caplog):
    # conftest replaces call_grader; the shipped config has the grader on, so the
    # first call fails and the render is kept, with no image_grades row.
    did = _seeded(conn)
    path = images.attach_chart(conn, did, CHART, source_url=URL)
    assert path is not None and store.list_image_grades(conn, did) == []
    assert "image grader failed" in caplog.text


def test_grader_disabled_skips_the_call(conn, monkeypatch):
    did = _seeded(conn)
    monkeypatch.setattr(
        grader, "grade_image", lambda *a, **k: pytest.fail("grader must not be called")
    )
    assert images.attach_chart(conn, did, CHART, cfg=_cfg(enabled=False)) is not None
    assert store.list_image_grades(conn, did) == []


def test_re_render_clears_old_grades_and_queue_shows_them(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from approval_queue.app import app

    did = _seeded(conn)
    fake = FakeGrader(
        [
            {"score": 5, "flaws": ["pale"], "adjustments": {"track": False}},
            {"score": 8, "fixes": ["ok"]},
            {"score": 9},
        ]
    )
    monkeypatch.setattr(grader, "grade_image", fake)
    images.attach_chart(conn, did, CHART, source_url=URL, cfg=_cfg())
    assert [g.score for g in store.list_image_grades(conn, did)] == [5, 8]
    images.attach_chart(conn, did, CHART, source_url=URL, cfg=_cfg())  # e.g. after a revise
    assert [g.score for g in store.list_image_grades(conn, did)] == [9]
    monkeypatch.setattr(store, "connect", lambda *a, **k: conn)
    page = TestClient(app).get(f"/drafts/{did}").text
    assert "Image grader:" in page and "9/10" in page and "(kept)" in page


def test_call_grader_api_path_sends_the_png(monkeypatch, tmp_path):
    import sys
    import types

    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(
        grader,
        "call_grader",
        grader.call_grader.__wrapped__
        if hasattr(grader.call_grader, "__wrapped__")
        else grader.call_grader,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    captured = {}

    class Messages:
        def create(self, **kw):
            captured.update(kw)
            return types.SimpleNamespace(content=[types.SimpleNamespace(text='{"score": 8}')])

    class Client:
        def __init__(self, api_key):
            captured["key"] = api_key
            self.messages = Messages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    monkeypatch.setitem(
        sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: False)
    )
    monkeypatch.setattr("claude_cli.llm_backend", lambda cfg: "api")
    out = REAL_CALL_GRADER(png, "sys", "user", "m")
    assert out == '{"score": 8}' and captured["model"] == "m" and captured["key"] == "k"
    blocks = captured["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["media_type"] == "image/png"
    assert blocks[1] == {"type": "text", "text": "user"}


def test_call_grader_cli_path_uses_read_tool(monkeypatch, tmp_path):
    import sys
    import types

    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG fake")
    monkeypatch.setitem(
        sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: False)
    )
    monkeypatch.setattr("claude_cli.llm_backend", lambda cfg: "claude_code")
    seen = {}

    def fake_run(prompt, *, system, model, cfg, tools):
        seen.update(prompt=prompt, tools=tools, model=model)
        return '{"score": 9}'

    monkeypatch.setattr("claude_cli.run_claude", fake_run)
    assert REAL_CALL_GRADER(png, "sys", "user", "m") == '{"score": 9}'
    assert seen["tools"] == ["Read"] and str(png.resolve()) in seen["prompt"]
