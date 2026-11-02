# -*- mode: python ; coding: utf-8 -*-
"""纯净无壳版：无卡密验证、无水印、无 UPX（pure-build-delivery 规范）。"""
from pathlib import Path

project = Path(SPECPATH).resolve()
pydir = project / "staging_pure_20261028_2100"
vendor = pydir / "vendor"

hidden = [
    "runtime", "windows_adapter", "wechat_db_adapter", "kf_dom_adapter",
    "wechat4_adapter", "wechat_accessibility", "wechat_multiselect",
    "message_snapshot", "accessibility_gate", "live_checks", "setup_wizard",
    "wechatauto", "wechatauto.db", "wechatauto.guia",
    "wechatauto.uia_driver", "wechatauto.accessibility",
    "pyperclip", "websockets",
    # zstandard：微信 4.x 长文本 zstd 帧解压（vendor 源码随包分发，静态分析
    # 看不到它的 import；不显式收集则长消息正文退化为「[文本]」占位不被转发）
    "zstandard",
    "winsdk", "winsdk.windows", "winsdk.windows.media.ocr",
    "winsdk.windows.globalization", "winsdk.windows.storage.streams",
]

a = Analysis(
    [str(pydir / "gui.py")],
    pathex=[str(pydir), str(vendor)],
    binaries=[],
    datas=[(str(vendor), "vendor")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'cv2', 'pyautogui', 'pyscreeze', 'pygetwindow',
        'mouseinfo', 'pytweening', 'imageio_ffmpeg',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="YtoForwarder",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
               upx_exclude=[], name="YtoForwarder")
