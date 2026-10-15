"""Robust location of ffmpeg / ffprobe and small media helpers.

Why this module exists
----------------------
Bilibili Notes is normally launched from Finder (MyBox -> ``open BilibiliNotes.app``).
Apps launched that way inherit the *minimal* launchd environment, whose PATH is
``/usr/bin:/bin:/usr/sbin:/sbin`` — it does **not** contain Homebrew or
``~/.local/bin`` where ffmpeg usually lives on macOS.

Without ffmpeg on PATH, yt-dlp silently skips the "merge video+audio" step and
leaves ``<bvid>.f100026.mp4`` (video only) and ``<bvid>.f30280.m4a`` (audio only)
behind.  The old downloader then picked the video-only file, handed it to
faster-whisper, and PyAV blew up with::

    IndexError: tuple index out of range

which surfaced in the UI as the useless message "tuple index out of range".

Everything here is defensive: never raise on a missing tool, just return ``None``
and let the caller produce a human readable error.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
from typing import Iterable, Optional

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

# Directories that commonly hold ffmpeg but are missing from PATH.
# On macOS the app is launched from Finder (minimal launchd PATH, no Homebrew or
# ~/.local/bin); on Windows it may be a portable bundle with ffmpeg beside the
# app.  Order matters: first match wins.
_EXTRA_DIRS: tuple[str, ...] = (
    "~/.local/bin",
    "~/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
    "/bin",
    # Windows
    r"C:\ffmpeg\bin",
    r"~\scoop\shims",
    r"~\AppData\Local\Microsoft\WinGet\Links",
    r"~\AppData\Local\Programs\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ProgramData\chocolatey\bin",
)

VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".flv", ".ts", ".m4v")


def bundle_root() -> Optional[str]:
    """Portable 包的根目录（里面有 ffmpeg/、models/、app/）。

    通过 ``MYBOX_HOME`` 指定；否则从本文件位置往上找——打包后结构是
    ``<root>/app/backend/media.py``，所以上溯两级就是根目录。
    """
    explicit = os.environ.get("MYBOX_HOME")
    if explicit and os.path.isdir(explicit):
        return os.path.abspath(explicit)
    here = os.path.dirname(os.path.abspath(__file__))          # .../app/backend
    candidate = os.path.dirname(os.path.dirname(here))          # .../<root>
    if os.path.isdir(os.path.join(candidate, "ffmpeg")) or os.path.isdir(
        os.path.join(candidate, "models")
    ):
        return candidate
    return None


def search_paths() -> list[str]:
    """The PATH that subprocesses (yt-dlp, ffmpeg) should see."""
    dirs: list[str] = []
    root = bundle_root()
    if root:
        # 便携包：ffmpeg 和 python 都在里面，优先用自带的
        for sub in ("ffmpeg\\bin", "ffmpeg", "python", os.path.join("python", "Scripts")):
            path = os.path.join(root, sub)
            if os.path.isdir(path):
                dirs.append(path)
    # The running interpreter's own bin directory: that is where yt-dlp lives
    # when it was pip-installed into the project's virtualenv.
    try:
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    except Exception:  # noqa: BLE001
        pass
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        if entry and entry not in dirs:
            dirs.append(entry)
    for raw in _EXTRA_DIRS:
        path = os.path.expanduser(raw)
        if path not in dirs:
            dirs.append(path)
    return dirs


def child_env() -> dict[str, str]:
    """Environment for child processes, with ffmpeg-friendly PATH prepended."""
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(search_paths())
    if not IS_WINDOWS:
        # yt-dlp/ffmpeg are much happier with a UTF-8 locale; the app may have
        # been started without one.  Windows 上设置这些反而会让 Python 的
        # stdio 编码出问题（中文路径/输出乱码），所以跳过。
        env.setdefault("LANG", "zh_CN.UTF-8")
        env.setdefault("LC_ALL", "zh_CN.UTF-8")
    else:
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
    return env


@functools.lru_cache(maxsize=None)
def find_tool(name: str) -> Optional[str]:
    """Locate an executable by name, ignoring PATH limitations.

    ``MYBOX_FFMPEG`` / ``MYBOX_FFPROBE`` override the lookup when set.
    """
    override = os.environ.get(f"MYBOX_{name.upper()}")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override

    found = shutil.which(name, path=os.pathsep.join(search_paths()))
    if found:
        return found
    if IS_WINDOWS:
        found = shutil.which(name + ".exe", path=os.pathsep.join(search_paths()))
        if found:
            return found

    suffixes = ("", ".exe", ".bat", ".cmd") if IS_WINDOWS else ("",)
    for raw in _EXTRA_DIRS:
        for suffix in suffixes:
            candidate = os.path.join(os.path.expanduser(raw), name + suffix)
            if os.path.isfile(candidate):
                return candidate

    # 便携包里自带的 ffmpeg
    root = bundle_root()
    if root:
        for sub in ("ffmpeg\\bin", "ffmpeg"):
            for suffix in suffixes:
                candidate = os.path.join(root, sub, name + suffix)
                if os.path.isfile(candidate):
                    return candidate
    return None


def ffmpeg_path() -> Optional[str]:
    return find_tool("ffmpeg")


def ffprobe_path() -> Optional[str]:
    return find_tool("ffprobe")


def ffmpeg_location() -> Optional[str]:
    """Directory to hand to yt-dlp's ``--ffmpeg-location``."""
    exe = ffmpeg_path()
    return os.path.dirname(exe) if exe else None


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a media tool with every hang-avoidance guard in place.

    ``stdin=DEVNULL`` and ffmpeg's ``-nostdin`` matter more than they look:
    ffmpeg reads keyboard commands from stdin by default, so a child that
    inherits our stdin can sit there forever waiting for input — which used to
    freeze the whole asyncio event loop, because these calls run synchronously.
    """
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=child_env(),
    )


async def probe_streams_async(path: str) -> list[dict]:
    import asyncio

    return await asyncio.to_thread(probe_streams, path)


_STREAM_RE = re.compile(
    r"Stream #\d+:\d+(?:[\[(][^\])]*[\])])*:\s*(Video|Audio|Subtitle)\s*:",
)


@functools.lru_cache(maxsize=512)
def _probe_signature(path: str, mtime: float, size: int) -> tuple[str, ...]:
    """Return stream kinds (``Video``/``Audio``/...) for a media file.

    Prefers ffprobe; falls back to parsing ``ffmpeg -i`` output, because a lot
    of macOS installs ship a standalone ffmpeg with no ffprobe beside it.
    """
    if not path or not os.path.exists(path):
        return ()

    probe = ffprobe_path()
    if probe:
        try:
            proc = _run(
                [
                    probe, "-v", "error",
                    "-show_entries", "stream=codec_type",
                    "-of", "csv=p=0",
                    path,
                ],
                timeout=25,
            )
            if proc.returncode == 0:
                kinds = tuple(
                    line.strip().capitalize()
                    for line in proc.stdout.decode("utf-8", "replace").splitlines()
                    if line.strip()
                )
                if kinds:
                    return kinds
        except Exception:  # noqa: BLE001
            pass

    exe = ffmpeg_path()
    if not exe:
        return ()
    try:
        proc = _run([exe, "-hide_banner", "-nostdin", "-i", path], timeout=25)
        text = proc.stderr.decode("utf-8", "replace")
        return tuple(m.group(1) for m in _STREAM_RE.finditer(text))
    except Exception:  # noqa: BLE001
        return ()


def probe_signature_cached(path: str) -> tuple[str, ...]:
    """Cached, cheap stream probe (path + mtime + size keyed)."""
    if not path or not os.path.exists(path):
        return ()
    try:
        stat = os.stat(path)
    except OSError:
        return ()
    return _probe_signature(path, stat.st_mtime, stat.st_size)


async def probe_signature(path: str) -> tuple[str, ...]:
    """Async wrapper so probing never blocks the event loop."""
    import asyncio

    return await asyncio.to_thread(probe_signature_cached, path)


async def has_audio_stream_async(path: str) -> bool:
    return "Audio" in await probe_signature(path)


async def has_video_stream_async(path: str) -> bool:
    return "Video" in await probe_signature(path)


def probe_streams(path: str) -> list[dict]:
    """Stream descriptors for *path* (``[{"codec_type": "Audio"}, ...]``)."""
    if not path or not os.path.exists(path):
        return []
    try:
        stat = os.stat(path)
    except OSError:
        return []
    return [{"codec_type": kind.lower()} for kind in _probe_signature(path, stat.st_mtime, stat.st_size)]


def has_audio_stream(path: str) -> bool:
    """True when *path* really contains a decodable audio track."""
    return any(s.get("codec_type") == "audio" for s in probe_streams(path))


def has_video_stream(path: str) -> bool:
    return any(s.get("codec_type") == "video" for s in probe_streams(path))


def mux(video_path: str, audio_path: str, out_path: str) -> bool:
    """Combine a video-only and an audio-only file. Returns success."""
    exe = ffmpeg_path()
    if not exe:
        return False
    try:
        proc = _run(
            [
                exe, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-i", video_path, "-i", audio_path,
                "-map", "0:v:0?", "-map", "1:a:0?",
                "-c", "copy", "-movflags", "+faststart",
                out_path,
            ],
            timeout=600,
        )
        return proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:  # noqa: BLE001
        return False


async def mux_async(video_path: str, audio_path: str, out_path: str) -> bool:
    import asyncio

    return await asyncio.to_thread(mux, video_path, audio_path, out_path)


def tooling_report() -> dict:
    """Diagnostics surfaced in the UI so PATH problems are visible, not silent."""
    return {
        "ffmpeg": ffmpeg_path(),
        "ffprobe": ffprobe_path(),
        "yt_dlp": find_tool("yt-dlp"),
        "platform": "windows" if IS_WINDOWS else ("macos" if IS_MACOS else sys.platform),
        "bundle_root": bundle_root(),
    }


def reveal_command(path: str) -> list[str]:
    """返回「在文件管理器里定位这个路径」的命令（纯函数，方便测试）。"""
    if os.path.isdir(path):
        if IS_WINDOWS:
            return ["explorer", path]
        if IS_MACOS:
            return ["open", path]
        return ["xdg-open", path]
    if IS_WINDOWS:
        return ["explorer", "/select,", path]
    if IS_MACOS:
        return ["open", "-R", path]
    return ["xdg-open", os.path.dirname(path)]


def browser_command(url: str) -> list[str]:
    """返回「用默认浏览器打开链接」的命令。"""
    if IS_WINDOWS:
        return ["cmd", "/c", "start", "", url]
    if IS_MACOS:
        return ["open", url]
    return ["xdg-open", url]


def reveal_path(path: str) -> bool:
    """在系统文件管理器里定位一个文件/文件夹。"""
    import subprocess as _sp

    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(target):
        return False
    try:
        _sp.Popen(reveal_command(target))  # noqa: S603
        return True
    except Exception:  # noqa: BLE001
        return False


def open_url(url: str) -> bool:
    """用系统默认浏览器打开链接。"""
    import subprocess as _sp

    if not url.lower().startswith(("http://", "https://")):
        return False
    try:
        if IS_WINDOWS:
            # start 是 cmd 内建命令，直接 os.startfile 更干净（不弹控制台）
            os.startfile(url)  # type: ignore[attr-defined]  # noqa: S606
        else:
            _sp.Popen(browser_command(url))  # noqa: S603
        return True
    except Exception:  # noqa: BLE001
        return False


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}GB"


def pick_media_file(cache_dir: str, bvid: str, exts: Iterable[str] = VIDEO_EXTS) -> Optional[str]:
    """Find the *merged* media file for *bvid*, ignoring yt-dlp fragments.

    yt-dlp writes ``<bvid>.f100026.mp4`` for the raw video stream and
    ``<bvid>.f30280.m4a`` for the raw audio stream.  Those must never be chosen:
    the first has no audio, the second has no picture.
    """
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return None

    allowed = {e.lower() for e in exts}
    best: Optional[str] = None
    best_size = -1
    for name in names:
        if not name.startswith(bvid + "."):
            continue
        if ".f" in name or name.endswith(".part") or name.endswith(".ytdl"):
            continue
        if os.path.splitext(name)[1].lower() not in allowed:
            continue
        full = os.path.join(cache_dir, name)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        # Prefer a file that actually contains audio; else the biggest one.
        if has_audio_stream(full):
            size += 1 << 40
        if size > best_size:
            best, best_size = full, size
    return best
