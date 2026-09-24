# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\mengz\\.grok\\tools\\chat_export_hub.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\mengz\\.grok\\tools\\chat_export_agents.py', '.'), ('C:\\Users\\mengz\\.grok\\tools\\chat_export_i18n.py', '.'), ('C:\\Users\\mengz\\.grok\\tools\\chat_export_search_index.py', '.'), ('C:\\Users\\mengz\\.grok\\tools\\assets\\chat_export_hub.ico', '.')],
    hiddenimports=['chat_export_agents', 'chat_export_i18n', 'chat_export_search_index'],
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
    a.binaries,
    a.datas,
    [],
    name='ChatExportHub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\mengz\\.grok\\tools\\assets\\chat_export_hub.ico'],
)
