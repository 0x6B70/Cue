# PyInstaller spec for the backend.
#
# Built with --onedir rather than --onefile: onefile re-extracts the whole
# bundle to a temp directory on every launch, which adds seconds to startup
# for no benefit when the output is going inside an installer anyway.
#
# yt-dlp is deliberately NOT frozen in here. It ships as a separate binary in
# vendor/ so backend.updater can replace it at runtime, and freezing it would
# also mean fighting its dynamic extractor imports.

from pathlib import Path

block_cipher = None
project_root = Path(SPECPATH).parent

datas = [
    (str(project_root / "frontend" / "ui"), "frontend/ui"),
    (str(project_root / "frontend" / "potoken"), "frontend/potoken"),
]

# Helper binaries (mpv, yt-dlp, deno, ffmpeg) land next to the frozen app so
# config.find_binary picks them up via bundle_dir()/vendor. Optional at build
# time: without them the app falls back to whatever is on PATH.
vendor_dir = project_root / "vendor"
if vendor_dir.is_dir():
    datas.append((str(vendor_dir), "vendor"))
else:
    print("spec: vendor/ not found; relying on PATH for mpv and yt-dlp")

analysis = Analysis(
    # run_backend.py, not backend/main.py: PyInstaller executes the entry
    # script as __main__, which would strip the package context that
    # backend's relative imports need.
    [str(project_root / "run_backend.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # uvicorn resolves these by name at runtime, so static analysis misses
        # them and the server dies on start without them.
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="youtube-queue-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console window on Windows. That leaves stdout unusable there, which
    # is why the Electron shell reads the handshake file rather than relying
    # on the child's stdout.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="youtube-queue-backend",
)
