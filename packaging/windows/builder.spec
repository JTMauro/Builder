# PyInstaller spec: one folder (dist\Builder) with two programs that share everything else:
#   Builder.exe      windowed: the server with a tray icon
#   builder-cli.exe  console:  mcp / serve / token / user add / doctor / app --selftest
# Run from packaging\windows:  pyinstaller --noconfirm builder.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

HERE = Path(SPECPATH)
ROOT = HERE.parent.parent

hidden = (collect_submodules("uvicorn") + collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli")) + collect_submodules("media_backend")
          + collect_submodules("webview", filter=lambda n: not any(x in n for x in ("gtk", "qt", "cocoa", "android", "cef")))
          + ["pystray._win32", "PIL.Image", "PIL.ImageDraw", "multipart", "python_multipart"])
datas = [(str(ROOT / "media_backend" / "static"), "media_backend/static")]
datas += collect_data_files("mcp") + collect_data_files("anthropic") + collect_data_files("webview")
binaries = collect_dynamic_libs("webview")  # WebView2 loader DLLs for Builder's own window

a = Analysis(
    [str(HERE / "entry_app.py"), str(HERE / "entry_cli.py")],
    pathex=[str(ROOT)],
    datas=datas,
    binaries=binaries,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(icon=str(HERE / "builder.ico"), version=str(HERE / "installer" / "version_info.txt"),
              upx=False, debug=False, strip=False, exclude_binaries=True)
app = EXE(pyz, [s for s in a.scripts if s[0] == "entry_app"], name="Builder", console=False, **common)
cli = EXE(pyz, [s for s in a.scripts if s[0] == "entry_cli"], name="builder-cli", console=True, **common)

COLLECT(app, cli, a.binaries, a.datas, name="Builder", upx=False)
