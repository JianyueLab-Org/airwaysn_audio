# -*- mode: python ; coding: utf-8 -*-

import ctypes.util
import os

excludes = ['torch', 'transformers']


def find_opus():
    """定位 opus 原生库。

    pymumble 的 opuslib 是运行时用 ctypes 去加载 opus 的，PyInstaller 的静态
    分析看不到这条依赖，不显式打进来的话，装机上没有 opus 的用户一启动就会
    报 "Could not find Opus library"。
    """
    found = ctypes.util.find_library('opus')
    if found and os.path.isfile(found):
        return found

    candidates = [os.path.join(os.path.dirname(os.path.abspath(SPEC)), 'opus.dll')]
    try:
        # pyogg 的 wheel 里带了 Windows 版 opus.dll，装了就直接拿来用
        import pyogg
        pyogg_dir = os.path.dirname(pyogg.__file__)
        candidates.append(os.path.join(pyogg_dir, 'opus.dll'))
        candidates.append(os.path.join(pyogg_dir, 'libs', 'opus.dll'))
    except Exception:
        pass

    for path in candidates:
        if os.path.isfile(path):
            return path

    print('警告: 没有找到 opus.dll，打出来的程序在没有 opus 的机器上无法启动。'
          '把 opus.dll 放到 controller 目录下再打包即可。')
    return None


opus_path = find_opus()
opus_binaries = [(opus_path, '.')] if opus_path else []

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=opus_binaries,
    datas=[],
    # scipy 的那几条隐式导入是给 ATIS 的语音合成用的，ATIS 删掉之后管制端不再
    # 依赖 scipy，就不用再打进来了
    hiddenimports=[
        'pymumble_py3',
        'google.protobuf',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='gui',
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
    icon=['favicon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='gui',
)