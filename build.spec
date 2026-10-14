# -*- mode: python ; coding: utf-8 -*-
"""AutoClick PyInstaller 打包配置（免环境独立 EXE）。

打包目标：产出一个**双击即可运行、目标机器无需安装 Python 与任何依赖**的 EXE。

必须一并打入的三类内容（见 DESIGN 4.7）：
1. Python 运行时 + tkinter / tcl / tk —— PyInstaller 自动收集；
2. OpenCV + numpy —— 完整版内置（目标机装不上依赖，而「找图」必须可用）；
3. adb.exe + AdbWinApi.dll + AdbWinUsbApi.dll —— 作为 data 打入，运行时经
   `sys._MEIPASS/resources/adb/` 释放到临时解包目录（`core/device.py` 的
   `bundled_adb()` 即按此路径探测，优先级高于环境变量与 PATH）。

两种构建产物（通过环境变量 AUTOCLICK_LIGHT 切换，由 build.ps1 传入）：
- 完整版 AutoClick.exe（默认）：内置 OpenCV，支持「找图」；
- 轻量版 AutoClick_Lite.exe（AUTOCLICK_LIGHT=1）：不含 OpenCV / numpy，仅支持「找控件」。

用户数据（flows/ 流程 JSON、templates/ 找图模板）**不打入 EXE**，运行时在 EXE
同级目录自动创建，保证可写、可备份、升级 EXE 不丢数据。
"""
import os

from PyInstaller.utils.hooks import collect_submodules

# 是否构建轻量版（不含 OpenCV / numpy）
LIGHT = os.environ.get("AUTOCLICK_LIGHT", "") == "1"

# 内置只读资源：adb 三件套（相对路径基于 build.spec 所在目录，即项目根）
datas = [
    ("resources/adb/adb.exe", "resources/adb"),
    ("resources/adb/AdbWinApi.dll", "resources/adb"),
    ("resources/adb/AdbWinUsbApi.dll", "resources/adb"),
]

hiddenimports = []
excludes = []

if LIGHT:
    # 轻量版：显式排除 OpenCV / numpy，避免被 main.py 的间接引用带进来
    excludes += ["cv2", "numpy"]
else:
    hiddenimports += collect_submodules("cv2")

a = Analysis(
    ["main.py"],
    pathex=[os.path.abspath(os.getcwd())],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AutoClick_Lite" if LIGHT else "AutoClick",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # 未安装 UPX，避免打包失败
    runtime_tmpdir=None,
    console=False,             # GUI 程序，隐藏控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
