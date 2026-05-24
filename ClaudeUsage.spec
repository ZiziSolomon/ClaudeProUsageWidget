# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['tray_widget.py'],
    pathex=[],
    binaries=[],
    # Bundle config.json (holds org_id) from the project root so a rebuild
    # always repopulates it into the dist bundle. Without this, COLLECT's
    # "Removing dir dist\ClaudeUsage" wipes the only copy. The frozen exe
    # resolves it via Path(__file__).parent / "config.json" -> _internal/.
    datas=[('config.json', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ClaudeUsage',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
    icon=['claude_usage.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ClaudeUsage',
)
