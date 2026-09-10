"""The project's CLIs behind one entry point, for the desktop build.

Usage: pipeline-cli run_ingest.py [--force]     (any run_*.py or digest.py, with its flags)
       python pipeline_cli.py run_ops.py status  (the same thing from a checkout)

The desktop build has no python.exe, so the runs page launches a step as
`pipeline-cli.exe run_ingest.py ...` and this maps the script name back to its module and
hands over the remaining arguments. Nothing else changes: each CLI parses its own flags,
loads .env from the working directory and exits with its own code.
"""

from __future__ import annotations

import importlib
import sys

from panel.frozen import CLIS


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    script = args[0] if args else ""
    name = script[:-3] if script.endswith(".py") else script
    if name not in CLIS:
        known = ", ".join(f"{c}.py" for c in CLIS)
        sys.stderr.write(f"usage: pipeline-cli <script.py> [args]\nknown scripts: {known}\n")
        return 2
    module = importlib.import_module(name)
    sys.argv = [script, *args[1:]]  # the CLI's argparse reads sys.argv when given None
    result = module.main()
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
