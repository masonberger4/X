"""Documentation must keep up with the CLIs. These checks fail a PR that adds a CLI, a
flag, an orchestrator step or a settings file without documenting it.

Rules:
- every run_*.py and digest.py is named in HOWTO.md, README.md and CLAUDE.md;
- every --flag a CLI accepts appears in HOWTO.md, README.md, or that CLI's own usage
  docstring;
- every step in ops/config.yaml is named in HOWTO.md;
- every settings file (config.yaml, */config.yaml) is named in HOWTO.md's
  "Changing settings" section and in README.md.
The .github/workflows/ci.yml "docs" job is the other half: a PR that touches pipeline
code must also touch a doc file unless its description says `docs-not-needed`.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CLIS = sorted(p.name for p in ROOT.glob("run_*.py")) + ["digest.py"]
DOCS = {name: (ROOT / name).read_text(encoding="utf-8") for name in ("HOWTO.md", "README.md")}
CLAUDE_MD = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
FLAG_RE = re.compile(r'add_argument\(\s*"(--[\w-]+)"')


def _flags(cli: str) -> list[str]:
    return sorted(set(FLAG_RE.findall((ROOT / cli).read_text(encoding="utf-8"))))


@pytest.mark.parametrize("cli", CLIS)
def test_every_cli_is_named_in_the_docs(cli):
    for doc, text in DOCS.items():
        assert cli in text, f"{cli} is not mentioned in {doc}"
    assert cli in CLAUDE_MD, f"{cli} is not mentioned in CLAUDE.md"


@pytest.mark.parametrize("cli", CLIS)
def test_every_flag_is_documented(cli):
    usage = importlib.import_module(cli[:-3]).__doc__ or ""
    haystack = "\n".join(DOCS.values()) + "\n" + usage
    missing = [f for f in _flags(cli) if f not in haystack]
    assert not missing, f"{cli} flags missing from HOWTO.md, README.md and its docstring: {missing}"


def test_every_ops_step_is_in_the_howto():
    cfg = yaml.safe_load((ROOT / "ops" / "config.yaml").read_text(encoding="utf-8"))
    names = [s["name"] for s in cfg["steps"]]
    part6 = DOCS["HOWTO.md"].split("## Part 6")[1]
    missing = [n for n in names if n not in part6]
    assert not missing, f"ops steps not described in HOWTO part 6: {missing}"


def test_every_settings_file_is_in_the_howto_and_readme():
    files = ["config.yaml"] + sorted(str(p.relative_to(ROOT)) for p in ROOT.glob("*/config.yaml"))
    settings = DOCS["HOWTO.md"].split("## Changing settings")[1]
    for f in files:
        win = f.replace("/", "\\")
        assert f in settings or win in settings, f"{f} missing from HOWTO 'Changing settings'"
        assert f in DOCS["README.md"], f"{f} missing from README.md"
