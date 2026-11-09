# -*- mode: python ; coding: utf-8 -*-
# SnapPin 打包配置：onefile exe（vendor 引擎不进包，随 zip 放 exe 旁边）
# 构建：python -m PyInstaller --noconfirm snappin.spec

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["PySide6.QtSvg", "PySide6.QtNetwork"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SnapPin",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="snappin.ico",
    version=None,
)
