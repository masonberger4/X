"""The desktop build: where things are when frozen, the CLI dispatcher, the launcher, and
the manifest the PyInstaller spec bundles from."""

import sys
import threading
import urllib.request
from pathlib import Path

import pytest

import pipeline_cli
import run_desktop
from ops import runner
from panel import frozen


@pytest.fixture
def frozen_at(tmp_path, monkeypatch):
    """Pretend to be a PyInstaller onedir build rooted at tmp_path/Pipeline."""
    app = tmp_path / "Pipeline"
    bundle = app / "_internal"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "Pipeline.exe"))
    return app, bundle


# ---- panel.frozen -------------------------------------------------------------


def test_a_checkout_uses_the_repo_root_and_the_current_interpreter():
    assert not frozen.is_frozen()
    assert frozen.bundle_dir() is None
    assert frozen.data_dir() == Path(__file__).resolve().parent.parent
    assert frozen.step_interpreter() == sys.executable


def test_a_frozen_build_uses_the_exe_folder_and_the_console_build(frozen_at):
    app, bundle = frozen_at
    assert frozen.is_frozen()
    assert frozen.bundle_dir() == bundle
    assert frozen.data_dir() == app
    assert frozen.step_interpreter() == str(app / "pipeline-cli.exe")


def test_the_manifest_names_every_settings_file_template_and_cli():
    root = frozen.REPO_ROOT
    datas, hidden = frozen.bundle_manifest(root)
    sources = {Path(src) for src, _ in datas}
    for missing in sources:
        assert missing.exists(), f"manifest names a file that does not exist: {missing}"
    assert root / "config.yaml" in sources
    for cfg in root.glob("*/config.yaml"):
        assert cfg in sources, f"{cfg} is not bundled"
    assert root / "draft" / "voice.md" in sources
    assert root / "approval_queue" / "templates" in sources
    assert root / "panel" / "templates" in sources
    for cli in frozen.CLIS:
        assert root / f"{cli}.py" in sources and cli in hidden
    assert "panel.app" in hidden


def test_the_manifest_covers_every_cli_the_docs_test_knows_about():
    root = frozen.REPO_ROOT
    on_disk = {p.stem for p in root.glob("run_*.py")} | {"digest"}
    on_disk -= {"run_desktop"}  # the window itself is the entry point, not a step
    assert on_disk == set(frozen.CLIS)


def test_the_spec_reads_the_manifest():
    spec = (frozen.REPO_ROOT / "deploy" / "desktop.spec").read_text(encoding="utf-8")
    assert "bundle_manifest" in spec
    assert 'name="Pipeline"' in spec and 'name="pipeline-cli"' in spec
    assert "console=False" in spec and "console=True" in spec


# ---- ops.runner in a frozen build ----------------------------------------------


def test_cli_missing_also_looks_in_the_bundle_when_frozen(frozen_at, tmp_path):
    app, bundle = frozen_at
    argv = ["pipeline-cli.exe", "run_ingest.py"]
    assert runner.cli_missing(argv, app)  # not in the data dir, not in the bundle yet
    (bundle / "run_ingest.py").write_text("")
    assert not runner.cli_missing(argv, app)


def test_cli_missing_ignores_the_bundle_from_a_checkout(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert runner.cli_missing(["python", "nope.py"], tmp_path)


# ---- pipeline_cli ------------------------------------------------------------------


def test_the_dispatcher_hands_the_arguments_to_the_named_cli(monkeypatch):
    seen = {}

    class Fake:
        @staticmethod
        def main():
            seen["argv"] = list(sys.argv)
            return 3

    monkeypatch.setattr(pipeline_cli.importlib, "import_module", lambda name: Fake)
    assert pipeline_cli.main(["run_ops.py", "status", "--db", "x.db"]) == 3
    assert seen["argv"] == ["run_ops.py", "status", "--db", "x.db"]


def test_the_dispatcher_refuses_anything_that_is_not_a_project_cli(capsys):
    assert pipeline_cli.main(["evil.py"]) == 2
    assert pipeline_cli.main([]) == 2
    assert pipeline_cli.main(["config.yaml"]) == 2
    assert "known scripts" in capsys.readouterr().err


def test_the_dispatcher_only_knows_the_manifest_clis():
    assert pipeline_cli.CLIS is frozen.CLIS


# ---- run_desktop ---------------------------------------------------------------


def test_pick_port_keeps_a_fixed_port_and_finds_a_free_one():
    assert run_desktop.pick_port("127.0.0.1", 8123) == 8123
    port = run_desktop.pick_port("127.0.0.1", 0)
    assert 1024 < port < 65536


def test_no_window_serves_the_panel_and_stops_when_the_wait_returns(db_file, monkeypatch, capsys):
    """The whole launcher, minus the native window: start, serve a real page, stop."""
    fetched = {}

    def fake_wait(thread):
        url = capsys.readouterr().out.strip()
        with urllib.request.urlopen(url + "/sources") as r:
            fetched["status"] = r.status
            fetched["body"] = r.read().decode()

    monkeypatch.setattr(run_desktop, "wait_forever", fake_wait)
    assert run_desktop.main(["--no-window"]) == 0
    assert fetched["status"] == 200 and "Sources" in fetched["body"]
    assert not any(t.name == "uvicorn" and t.is_alive() for t in threading.enumerate())


def test_the_window_gets_the_same_url_the_server_listens_on(db_file, monkeypatch):
    opened = {}
    monkeypatch.setattr(run_desktop, "open_window", lambda url: opened.setdefault("url", url))
    assert run_desktop.main(["--port", "0"]) == 0
    assert opened["url"].startswith("http://127.0.0.1:")


def test_a_windowed_process_logs_to_a_file_beside_the_data(frozen_at, monkeypatch):
    app, _ = frozen_at
    import logging

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    run_desktop.configure_logging()
    try:
        assert any(getattr(h, "baseFilename", "").endswith("desktop.log") for h in root.handlers)
        assert (app / "desktop.log").exists()
    finally:
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)


def test_a_missing_pywebview_is_one_clear_line_before_any_server_starts(monkeypatch, capsys):
    monkeypatch.setattr(run_desktop, "window_available", lambda: False)
    started = []
    monkeypatch.setattr(run_desktop, "start_server", lambda *a: started.append(a))
    assert run_desktop.main([]) == 1
    assert 'pip install -e ".[desktop]"' in capsys.readouterr().err
    assert started == []


def test_no_window_does_not_need_pywebview(monkeypatch, db_file):
    monkeypatch.setattr(run_desktop, "window_available", lambda: False)
    monkeypatch.setattr(run_desktop, "wait_forever", lambda thread: None)
    assert run_desktop.main(["--no-window"]) == 0
