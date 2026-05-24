# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['tray_widget.py'],
    pathex=[],
    binaries=[],
    # Bundle config.json (holds org_id) as a FALLBACK only. The widget now
    # resolves config from %LOCALAPPDATA%\ClaudeUsage\config.json FIRST and
    # writes any auto-discovered org_id there, so it survives COLLECT's
    # "Removing dir dist\ClaudeUsage" wipe on rebuild. The bundled copy is
    # still read if the per-user one is absent (see _read_config in
    # widget_updater). Most users won't need config.json at all now: missing
    # org_id is auto-discovered from /api/organizations on first run.
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
