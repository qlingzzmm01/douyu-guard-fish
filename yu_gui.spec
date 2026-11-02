# -*- mode: python ; coding: utf-8 -*-
"""保卫鱼娘挂机助手 · PyInstaller 打包配置（单文件 exe，含 patchright driver）

防封改造说明：
  - 主驱动 patchright（Playwright 反检测分支），因此打包 patchright 自带的 driver。
  - playwright 仍保留 hiddenimports 作为源码环境的降级路径；但**不再打包它的 driver**
    （两个 driver 各约 100MB，双份会让 exe 翻倍）。打包版永远走 patchright。
"""
import os

# ---- patchright driver（node.exe + package/cli.js，约 100MB）----
try:
    import patchright
    PR_DIR = os.path.dirname(patchright.__file__)
except Exception:
    raise SystemExit("打包需要先 pip install patchright")

PR_DRIVER = os.path.join(PR_DIR, "driver")
if not os.path.isdir(PR_DRIVER):
    raise SystemExit("未找到 patchright driver：%s（试试 pip install --force-reinstall patchright）" % PR_DRIVER)

datas = [(PR_DRIVER, os.path.join("patchright", "driver"))]
hiddenimports = [
    "patchright", "patchright.sync_api", "patchright.async_api",
    # 降级路径（源码环境有效；打包版缺 driver，仅供 import 不报错）
    "playwright", "playwright.sync_api", "playwright.async_api",
    "greenlet",
]

a = Analysis(
    ["yu_gui.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas", "PIL", "scipy", "pytest",
              "IPython", "notebook", "tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="保卫鱼娘助手",
    debug=False,
    strip=False,
    upx=False,
    console=False,                 # 纯 GUI，不弹黑色控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
