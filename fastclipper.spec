# -*- mode: python ; coding: utf-8 -*-
import os
import shutil
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(os.path.abspath(SPECPATH))

datas = [
    (str(ROOT / "fastclipper" / "web" / "studio.html"), "fastclipper/web"),
    (str(ROOT / "face_detector.tflite"), "."),
    (
        str(ROOT / "fastclipper" / "assets" / "models" / "face_detection_yunet_2023mar.onnx"),
        "fastclipper/assets/models",
    ),
]
binaries = []
hiddenimports = (
    collect_submodules("fastclipper")
    + collect_submodules("uvicorn")
    + collect_submodules("fastapi")
    + collect_submodules("pydantic")
    + ["cv2"]
)

# Empacota ffmpeg, ffprobe e yt-dlp se presentes no ambiente
for exe_name in ("ffmpeg", "ffprobe", "yt-dlp"):
    found = shutil.which(exe_name)
    if found:
        binaries.append((found, "."))

# Inclui faster_whisper / ctranslate2 e DLLs NVIDIA CUDA se instalados (sem empacotar torch gigante)
for pkg in ("faster_whisper", "ctranslate2"):
    try:
        pkg_datas, pkg_bins, pkg_hidden = collect_all(pkg)
        datas += pkg_datas
        binaries += pkg_bins
        hiddenimports += pkg_hidden
    except Exception:
        pass

import sys as _sys
_nvidia_root = Path(_sys.prefix) / "Lib" / "site-packages" / "nvidia"
if _nvidia_root.is_dir():
    for _dll in _nvidia_root.glob("*/bin/*.dll"):
        binaries.append((str(_dll), "."))

a = Analysis(
    [str(ROOT / "fastclipper" / "desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "torchvision", "torchaudio", "matplotlib", "tkinter", "PyQt5", "PySide6"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FastClipper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="FastClipper",
)
