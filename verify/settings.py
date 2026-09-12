"""Load verify/config.yaml (this step's own settings; the root config.yaml still names the
LLM backend and the company feeds)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).with_name("config.yaml")

DEFAULTS: dict[str, Any] = {
    "model": "",
    "effort": "medium",
    "max_claims_per_draft": 6,
    "max_searches_per_claim": 5,
    "timeout_seconds": 240,
    "trusted_domains": [],
    "tables": {"enabled": True, "max_cells_per_draft": 30, "min_supported_ratio": 0.6},
    "auto_revise": {"enabled": False, "max_rounds": 3, "max_rounds_per_draft": 6},
}


@lru_cache(maxsize=1)
def load_verify_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    out = dict(DEFAULTS)
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            out.update(yaml.safe_load(fh) or {})
    out["trusted_domains"] = [str(d).lower().strip() for d in out.get("trusted_domains") or []]
    out["tables"] = {**DEFAULTS["tables"], **(out.get("tables") or {})}
    out["auto_revise"] = {**DEFAULTS["auto_revise"], **(out.get("auto_revise") or {})}
    return out


HOST_RE = re.compile(r"^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")


def normalize_host(value: str) -> str:
    """A bare host from what the human sent: lowercased, scheme, path and a leading `www.`
    stripped. Raises ValueError for anything that is not a domain name."""
    host = (value or "").strip().lower()
    if "//" in host:
        host = host.split("//", 1)[1]
    host = host.split("/", 1)[0].split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    if not HOST_RE.match(host):
        raise ValueError(f"not a domain name: {value!r}")
    return host


def add_trusted_domain(host: str, path: str | Path | None = None) -> bool:
    """Append one host to `trusted_domains:` in verify/config.yaml (the queue's "Trust this
    source" button calls this). A line edit: the list gains one `  - host` item after its
    last one and every comment survives; a missing key is appended. Returns False when the
    host is already listed. Clears the config cache so the next load sees it."""
    host = normalize_host(host)
    target = Path(path or CONFIG_PATH)
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True) if target.exists() else []
    key_at = next((i for i, ln in enumerate(lines) if re.match(r"^trusted_domains\s*:", ln)), None)
    if key_at is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += ["trusted_domains:\n", f"  - {host}\n"]
    else:
        last = key_at
        for i in range(key_at + 1, len(lines)):
            ln = lines[i]
            if re.match(r"^\s+-\s", ln):
                if re.sub(r"#.*", "", ln).split("-", 1)[1].strip().lower() == host:
                    return False
                last = i
            elif ln.strip() == "" or ln.lstrip().startswith("#"):
                continue
            else:
                break
        indent = re.match(r"^\s*", lines[last]).group(0) if last != key_at else "  "
        lines.insert(last + 1, f"{indent}- {host}\n")
    target.write_text("".join(lines), encoding="utf-8")
    load_verify_config.cache_clear()
    return True
