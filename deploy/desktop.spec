# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the desktop build. From the repo root:
#
#   pip install -e ".[desktop]"
#   pyinstaller deploy/desktop.spec
#
# Produces dist/Pipeline/ containing Pipeline.exe (the window, no console) and
# pipeline-cli.exe (a console build the runs page launches steps with), sharing one
# _internal/ folder. What goes into the bundle is decided in one place,
# panel/frozen.py:bundle_manifest, which a test checks against the files on disk.
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # SPECPATH is set by PyInstaller: this file's folder
sys.path.insert(0, str(ROOT))

from panel.frozen import bundle_manifest  # noqa: E402

datas, hiddenimports = bundle_manifest(ROOT)

window = Analysis(
    [str(ROOT / "run_desktop.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
)
cli = Analysis(
    [str(ROOT / "pipeline_cli.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
)

window_exe = EXE(
    PYZ(window.pure),
    window.scripts,
    [],
    exclude_binaries=True,
    name="Pipeline",
    console=False,
)
cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    [],
    exclude_binaries=True,
    name="pipeline-cli",
    console=True,
)

COLLECT(
    window_exe,
    window.binaries,
    window.datas,
    cli_exe,
    cli.binaries,
    cli.datas,
    name="Pipeline",
)
