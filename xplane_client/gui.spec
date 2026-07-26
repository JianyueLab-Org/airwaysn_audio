# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# 收集PyQt6依赖
qt_binaries, qt_datas, qt_hiddenimports = collect_all('PyQt6')

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=qt_binaries,
    datas=[
        ('radio.py', '.'),
        ('settings.py', '.'),
    ] + qt_datas,
    hiddenimports=[
        'pkg_resources',
        'pkgutil',
        'google.protobuf',
        'keyboard',
        'pymumble_py3',
        'PyQt6.sip',
        'numpy',
    ] + qt_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='xplane_radio_gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,

    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['favicon.ico'],
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='xplane_radio_gui',
)
