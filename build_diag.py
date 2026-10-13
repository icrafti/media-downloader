"""Диагностика portable-сборки: что реально попало внутрь exe."""
import os
import shutil
import sys

print("frozen:", getattr(sys, "frozen", False))
print("MEIPASS:", getattr(sys, "_MEIPASS", "-"))
if getattr(sys, "frozen", False):
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")
    print("APP_DIR would be:", os.path.dirname(sys.executable))

import yt_dlp
print("yt_dlp:", yt_dlp.version.__version__)

import libtorrent as lt
print("libtorrent:", lt.version)

ff = shutil.which("ffmpeg")
fp = shutil.which("ffprobe")
print("ffmpeg:", ff)
print("ffprobe:", fp)
assert ff and fp, "ffmpeg/ffprobe не найдены в сборке!"
assert yt_dlp.version.__version__, "yt_dlp не импортирован!"
print("DIAG OK")
