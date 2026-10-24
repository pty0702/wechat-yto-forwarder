# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：卡密验证版 GUI（onedir，免安装）。

构建：.venv\\Scripts\\python.exe -m PyInstaller --noconfirm build.spec
产物：dist/YtoForwarder/（整目录即绿色软件）
"""
from pathlib import Path

project = Path(SPECPATH).resolve()
pydir = project / "python版"
vendor = pydir / "vendor"

hidden = [
    # 项目模块：多数经函数内延迟 import，静态分析可能漏掉
    "runtime", "windows_adapter", "wechat_db_adapter", "kf_dom_adapter",
    "wechat4_adapter", "wechat_accessibility", "wechat_multiselect",
    "message_snapshot", "accessibility_gate", "live_checks",
    "license_client", "setup_wizard",
    # wechatauto（vendor，运行时 sys.path 注入）
    "wechatauto", "wechatauto.db", "wechatauto.guia",
    "wechatauto.uia_driver", "wechatauto.accessibility",
    # 常见 hook 盲区
    "pyperclip", "websockets",
]

a = Analysis(
    [str(pydir / "gui.py")],
    pathex=[str(pydir), str(vendor)],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 朋友圈模块（moment.py）专用依赖，本项目不调用；排除可减 200MB
        'cv2', 'pyautogui', 'pyscreeze', 'pygetwindow',
        'mouseinfo', 'pytweening', 'imageio_ffmpeg',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YtoForwarder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="YtoForwarder",
)
