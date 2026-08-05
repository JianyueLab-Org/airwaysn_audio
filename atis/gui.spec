# -*- mode: python ; coding: utf-8 -*-

import ctypes.util
import os
import shutil
import subprocess
import sys

# build 号在这里固化。打包之后程序里没有 .git，运行时再问 git 只会得到"不是
# 仓库"——所以打包时把 git 状态写成 buildinfo.json 一起打进去。
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
sys.path.insert(0, SPEC_DIR)
import version

version.freeze(SPEC_DIR)

# 打 Windows 包还是 macOS 包。PyInstaller 不支持交叉编译，所以这里就是"在哪台
# 机器上打"，不是一个可选项。
MACOS = sys.platform == 'darwin'

APP_NAME = 'atis-for-can'
BUNDLE_ID = 'org.airwaysn.atis-for-can'

# scipy 只被用来重采样，已经换成 numpy 的线性插值了；排除掉能省一半体积
# 不要用 collect_submodules('qfluentwidgets')：它会把 scipy / pillow / colorthief
# 那几个可选依赖一起拖进来，包会涨出上百兆。这里显式排掉。
excludes = ['torch', 'transformers', 'scipy', 'PIL', 'colorthief']


def find_opus():
    """定位 opus 原生库。

    pymumble 的 opuslib 是运行时用 ctypes 去加载 opus 的，PyInstaller 的静态
    分析看不到这条依赖，不显式打进来的话，装机上没有 opus 的用户一启动就会
    报 "Could not find Opus library"。

    macOS 上叫 libopus.dylib，而且系统里根本不自带——多半来自 Homebrew，
    Apple 芯片在 /opt/homebrew，Intel 在 /usr/local，两个都试。
    """
    found = ctypes.util.find_library('opus')
    if found and os.path.isfile(found):
        return found

    if MACOS:
        names = ('libopus.dylib', 'libopus.0.dylib')
        candidates = [os.path.join(SPEC_DIR, name) for name in names]
        for prefix in ('/opt/homebrew', '/usr/local'):
            candidates += [os.path.join(prefix, 'lib', name) for name in names]
    else:
        candidates = [os.path.join(SPEC_DIR, 'opus.dll')]
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

    if MACOS:
        print('警告: 没有找到 libopus.dylib，打出来的程序一启动就会报'
              ' "Could not find Opus library"。先 brew install opus 再打包。')
    else:
        print('警告: 没有找到 opus.dll，打出来的程序在没有 opus 的机器上无法启动。'
              '把 opus.dll 放到 atis 目录下再打包即可。')
    return None


def macos_icns():
    """给 .app 现做一个 .icns。

    favicon.ico 其实是个 64x64 的 PNG，只是扩展名写成了 .ico（见 CLAUDE.md）。
    Windows 那边靠 pillow 把它转成真的 ico；macOS 要的是 .icns，用系统自带的
    sips + iconutil 现做——这两个工具每台 Mac 上都有，不用多一个 Python 依赖。

    64 往上是放大的，512 那一档会有点糊。源图就这么大，换清楚的图标要另找素材，
    不该卡住打包。做不出来就返回 None：**图标难看是小事，为它打不出包是大事。**
    """
    source = os.path.join(SPEC_DIR, 'favicon.ico')
    if not os.path.isfile(source):
        return None
    iconset = os.path.join(SPEC_DIR, 'build', APP_NAME + '.iconset')
    target = os.path.join(SPEC_DIR, 'build', APP_NAME + '.icns')
    try:
        shutil.rmtree(iconset, ignore_errors=True)
        os.makedirs(iconset, exist_ok=True)
        # iconutil 只认这一组固定的文件名，少一档它就拒绝整个 iconset
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ''), (2, '@2x')):
                pixels = size * scale
                out = os.path.join(
                    iconset, 'icon_%dx%d%s.png' % (size, size, suffix))
                subprocess.run(['sips', '-z', str(pixels), str(pixels),
                                source, '--out', out],
                               check=True, capture_output=True)
        subprocess.run(['iconutil', '-c', 'icns', iconset, '-o', target],
                       check=True, capture_output=True)
        return target
    except Exception as e:
        print('警告: 生成 .icns 失败，程序坞里会是默认图标: %s' % e)
        return None


opus_path = find_opus()
opus_binaries = [(opus_path, '.')] if opus_path else []

icon_file = macos_icns() if MACOS else os.path.join(SPEC_DIR, 'favicon.ico')

# 语音合成的隐式导入。pyttsx3 的驱动是运行时按名字加载的，静态分析找不到。
# **macOS 上一条都不要**：那里 broadcast.py 走的是系统自带的 `say`，根本不调
# pyttsx3.init()（nsss 驱动写出来的是 AIFF，读不回来，见 broadcast.py 的注释）。
# 把 sapi5 / comtypes 列进去只会让 PyInstaller 在 mac 上报一串找不到的模块。
tts_hiddenimports = [] if MACOS else [
    'pyttsx3.drivers',
    'pyttsx3.drivers.sapi5',
    'comtypes',
]

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=opus_binaries,
    # 窗口图标要随程序分发，否则打包后运行时取不到
    # 机场坐标表：席位位置要用，不带上就会落在 0/0
    datas=[('favicon.ico', '.'), ('airports.json', '.'), ('buildinfo.json', '.')],
    hiddenimports=[
        'pymumble_py3',
        'google.protobuf',
        'qfluentwidgets',
        'qframelesswindow',
    ] + tts_hiddenimports,
    hookspath=[],
    hooksconfig={},
    # macOS 上必须有这个钩子，否则没装 Homebrew 的机器一启动就报
    # "Could not find Opus library"——原因见 rthook_opus.py。
    runtime_hooks=['rthook_opus.py'],
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
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 在 macOS 上不能开：压过的 Mach-O 会让代码签名对不上，Apple 芯片上
    # 未签名的二进制**根本不给运行**，症状是双击没反应、控制台里一条
    # "Killed: 9"。Windows 上照旧压。
    upx=not MACOS,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[icon_file] if icon_file else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=not MACOS,
    upx_exclude=[],
    name=APP_NAME,
)

if MACOS:
    # 没有 BUNDLE 就只有 dist/atis-for-can/ 一个目录和里面一个可执行文件——
    # 能从终端跑，但在 Finder 里不是"一个应用"：拖不进"应用程序"，也没有程序坞
    # 图标。
    #
    # **这里没有 NSMicrophoneUsageDescription，是有意的。** 通播只发合成出来的
    # 语音，一个本地音频设备都不开（broadcast.py 的模块注释就是这么说的），
    # 所以它不该向用户要麦克风权限——要一个用不到的权限，比不要更可疑。
    app = BUNDLE(
        coll,
        name=APP_NAME + '.app',
        icon=icon_file,
        bundle_identifier=BUNDLE_ID,
        version=version.version(),
        info_plist={
            'CFBundleShortVersionString': version.version(),
            # build 号形如 "71.667d9b4"，带字母。CFBundleVersion 要求是点分
            # 数字，塞字母进去 Finder 会显示不出版本，公证也会挑刺——完整的
            # build 号另放一个自定义键，界面上显示的那个由 version.py 管。
            'CFBundleVersion': version.version(),
            'AirwaysnBuild': version.build(),
            # 不设这个的话，整个界面会被当成低分辨率位图拉伸，在 Retina 上糊成
            # 一片，看起来像 Qt 坏了
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '11.0',
            'LSApplicationCategoryType': 'public.app-category.utilities',
        },
    )
