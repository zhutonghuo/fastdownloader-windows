# -*- coding: utf-8 -*-
"""打包为单文件 exe（pyinstaller --onefile，内嵌 assets/lan.html）"""
import os, sys, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
UPX = r"D:\一些小项目\upx.exe"

cmd = [
    PY, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--name", "FastDownloader",
    "--add-data", os.path.join(HERE, "assets", "lan.html") + os.pathsep + os.path.join("assets"),
    "--hidden-import", "PyQt5.QtMultimedia",
    "--hidden-import", "PyQt5.QtMultimediaWidgets",
    "--hidden-import", "qrcode",
    "--hidden-import", "PIL",
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    "--exclude-module", "numpy",
    "--clean",
    "--noconfirm",
]
if os.path.exists(UPX):
    cmd += ["--upx-dir", os.path.dirname(UPX)]
cmd.append(os.path.join(HERE, "main.py"))

print("RUN:", " ".join(cmd))
rc = subprocess.call(cmd, cwd=HERE)
print("RC =", rc)
sys.exit(rc)
