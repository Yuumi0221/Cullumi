# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

msvcp140 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "msvcp140.dll")
fallback_internal = os.environ.get("CULLUMI_BUILD_INTERNAL", "")
runtime_datas = []
runtime_datas.extend(collect_data_files("imageio_ffmpeg"))
runtime_binaries = collect_dynamic_libs("onnxruntime")
if fallback_internal:
    for runtime_dir in ("clr_loader", "pythonnet", "webview", "rawpy"):
        source = os.path.join(fallback_internal, runtime_dir)
        if os.path.isdir(source):
            runtime_datas.append((source, runtime_dir))

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[(msvcp140, "."), *runtime_binaries],
    datas=[("web", "web"), ("models", "models"), *runtime_datas],
    hiddenimports=[
        "clr",
        "webview",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "pillow_heif",
        "rawpy",
        "imageio_ffmpeg",
        "onnxruntime",
        "onnxruntime.capi._pybind_state",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Cullumi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="web/assets/icons/brand-icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Cullumi",
)
