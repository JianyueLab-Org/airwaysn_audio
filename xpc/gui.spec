# -*- mode: python ; coding: utf-8 -*-

import ctypes.util
import os
import sys

# build 号在这里固化。打包之后程序里没有 .git，运行时再问 git 只会得到"不是
# 仓库"——所以打包时把 git 状态写成 buildinfo.json 一起打进去。
sys.path.insert(0, os.path.dirname(os.path.abspath(SPEC)))
import version

version.freeze(os.path.dirname(os.path.abspath(SPEC)))

excludes = ['torch', 'transformers', 'scipy', 'pyttsx3', 'matplotlib']


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
          '把 opus.dll 放到 xpc 目录下再打包即可。')
    return None


opus_path = find_opus()
opus_binaries = [(opus_path, '.')] if opus_path else []

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=opus_binaries,
    datas=[
        # 窗口图标要随程序分发，否则打包后运行时取不到
        ('favicon.ico', '.'),
        # X-Plane 插件。它不在这个进程里跑——是给用户复制到
        # <X-Plane>/Resources/plugins/PythonPlugins/ 的，所以只是随包带上。
        ('plugin/PI_XpcTraffic.py', 'plugin'),
        ('buildinfo.json', '.'),
    ],
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
    name='xpc-for-can',
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
    name='xpc-for-can',
)
