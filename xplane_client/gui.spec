# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# build 号在这里固化。打包之后程序里没有 .git，运行时再问 git 只会得到"不是
# 仓库"——所以打包时把 git 状态写成 buildinfo.json 一起打进去。
sys.path.insert(0, os.path.dirname(os.path.abspath(SPEC)))
import version

version.freeze(os.path.dirname(os.path.abspath(SPEC)))

# 收集PyQt6依赖
qt_binaries, qt_datas, qt_hiddenimports = collect_all('PyQt6')

import ctypes.util


def find_opus():
    """定位 opus 原生库。

    pymumble 的 opuslib 是运行时用 ctypes 加载 opus 的，PyInstaller 的静态分析
    看不到这条依赖。不显式打进来的话，装机上没有 opus 的用户一启动就会报
    "Could not find Opus library"。
    """
    found = ctypes.util.find_library('opus')
    if found and os.path.isfile(found):
        return found

    candidates = [os.path.join(os.path.dirname(os.path.abspath(SPEC)), 'opus.dll')]
    try:
        import pyogg
        pyogg_dir = os.path.dirname(pyogg.__file__)
        candidates.append(os.path.join(pyogg_dir, 'opus.dll'))
        candidates.append(os.path.join(pyogg_dir, 'libs', 'opus.dll'))
    except Exception:
        pass

    for path in candidates:
        if os.path.isfile(path):
            return path

    print('警告: 没有找到 opus.dll，打出来的程序在没有 opus 的机器上无法启动。')
    return None


opus_path = find_opus()
opus_binaries = [(opus_path, '.')] if opus_path else []


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=qt_binaries + opus_binaries,
    datas=[
        ('radio.py', '.'),
        ('settings.py', '.'),
        ('buildinfo.json', '.'),
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
