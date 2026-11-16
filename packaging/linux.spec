"""Package the CLI and server together, sharing bundled runtime libraries."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
contents = []
for name, script in (("git-sync", "cli.py"), ("git-sync-server", "app.py")):
    analysis = Analysis(
        [str(root / script)],
        pathex=[str(root)],
        datas=[(str(root / "templates"), "templates")],
        hiddenimports=collect_submodules("uvicorn") + ["anyio._backends._asyncio"],
        excludes=["pip", "setuptools", "PyInstaller"],
    )
    executable = EXE(
        PYZ(analysis.pure),
        analysis.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=True,
        upx=False,
    )
    contents.extend([executable, analysis.binaries, analysis.datas])

COLLECT(*contents, name="relay-git-sync", upx=False)
