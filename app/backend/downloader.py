"""纯下载：把视频文件存到本地，不解析、不做笔记。

「下载视频」按钮用它。底层交给 yt-dlp —— 便携包里本来就带了，
格式选择、音视频合并、分片并发这些都由它负责，我们只负责
把进度解析出来、把最终文件路径找出来。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

from . import media

# yt-dlp 的输出里，这几行决定了最终文件名
_PERCENT = re.compile(r"\[download\]\s+([\d.]+)%")
_DESTINATION = re.compile(r"\[download\]\s+Destination:\s*(.+?)\s*$")
_MERGING = re.compile(r'\[Merger\]\s+Merging formats into\s+"(.+?)"\s*$')
_ALREADY = re.compile(r"\[download\]\s+(.+?)\s+has already been downloaded")
_TOTAL = re.compile(r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\s*\w+)")

_BOILERPLATE = re.compile(
    r";?\s*please report this issue on\s+https?://\S+.*$"
    r"|Confirm you are on the latest version using\s+yt-dlp -U\s*",
    re.I | re.S,
)

# 常见失败 -> 一句能照着做的话
_FRIENDLY: tuple[tuple[re.Pattern, str], ...] = (
    (
        re.compile(r"Unsupported URL", re.I),
        "这个链接 yt-dlp 不认识，下不了。目前稳定可用的是 B 站；"
        "其它站点能不能下取决于 yt-dlp 的支持情况。",
    ),
    (
        re.compile(r"Fresh cookies|Uifid|ArgusSecurityPlugin|aweme/detail", re.I),
        "抖音的反爬（Argus）挡住了这个请求，yt-dlp 目前解析不了抖音。"
        "可以先试试在设置里配置抖音 Cookie；仍然不行的话这条路暂时走不通。",
    ),
    (
        re.compile(r"Sign in to confirm|login required|需要登录|Login required", re.I),
        "该视频需要登录才能下载。可以在设置里补齐对应站点的 Cookie 后重试。",
    ),
    (
        re.compile(r"HTTP Error 403", re.I),
        "站点返回 403（拒绝访问）。多半是防盗链或需要 Cookie，换一个视频或补 Cookie 再试。",
    ),
    (
        re.compile(r"HTTP Error 404|Video unavailable|not available|已删除|稿件不可见", re.I),
        "这个视频当前不可访问（可能已删除、私有或限制地区）。",
    ),
    (
        re.compile(r"Too Many Requests|HTTP Error 429|HTTP Error 412", re.I),
        "站点限流了，等 1~2 分钟再试；频繁下载同一个站点容易被拦。",
    ),
    (
        re.compile(r"Requested format is not available", re.I),
        "这个视频没有合适的清晰度，换一个视频试试。",
    ),
)


class DownloadError(Exception):
    """带可直接展示给用户的中文消息。"""


def friendly_error(raw: str, url: str = "") -> str:
    text = (raw or "").replace("ERROR:", " ").strip()
    text = _BOILERPLATE.sub("", text).strip()

    # 抖音的短链（v.douyin.com/xxx）yt-dlp 直接判定为 Unsupported URL，
    # 但用户真正想知道的是「抖音下不了」，所以按域名先行判断。
    host = ""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return (
            "抖音的反爬（Argus）挡住了这个请求，yt-dlp 目前解析不了抖音。"
            "可以先去设置里配置抖音 Cookie 再试；仍然不行的话这条路暂时走不通。"
        )

    for pattern, message in _FRIENDLY:
        if pattern.search(text):
            return message
    first = next((line.strip() for line in text.splitlines() if line.strip()), text)
    return first[:200] or "下载失败，但 yt-dlp 没有给出原因"


def ytdlp_command() -> list[str]:
    """优先用独立可执行文件，没有就退回 ``python -m yt_dlp``。"""
    exe = media.find_tool("yt-dlp")
    return [exe] if exe else [sys.executable, "-m", "yt_dlp"]


def _format_selector(quality: str) -> str:
    quality = (quality or "").strip().lower()
    if quality.isdigit():
        h = int(quality)
        return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
    return "bv*+ba/b"


async def download_video(
    url: str,
    dest_dir: str,
    on_progress: Optional[Callable[[float, str], None]] = None,
    quality: str = "",
) -> dict:
    """把 ``url`` 下到 ``dest_dir``，返回 ``{"path", "bytes", "title"}``。"""
    if not url or not re.match(r"^https?://", url.strip()):
        raise DownloadError("请粘贴 http(s) 开头的视频链接")
    url = url.strip()

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        raise DownloadError(f"下载目录不可用：{dest_dir}（{exc}）") from exc

    ffmpeg_dir = media.ffmpeg_location()
    if not ffmpeg_dir:
        raise DownloadError(
            "找不到 ffmpeg，无法合并音视频。请把完整压缩包里的 ffmpeg 目录放回原位，"
            "或设置环境变量 MYBOX_FFMPEG 指向可执行文件。"
        )

    def notify(percent: float, message: str) -> None:
        if on_progress:
            try:
                on_progress(max(0.0, min(100.0, percent)), message)
            except Exception:  # noqa: BLE001 - 进度回调不能弄挂下载
                pass

    # 文件名交给 yt-dlp 的模板：截断到 120 字节，避免 Windows 路径过长
    output_template = os.path.join(dest_dir, "%(title).120B.%(ext)s")
    cmd = [
        *ytdlp_command(),
        "-f", _format_selector(quality),
        "--merge-output-format", "mp4",
        "--ffmpeg-location", ffmpeg_dir,
        "-o", output_template,
        "--no-playlist",
        "--no-mtime",
        "--no-overwrites",
        "--windows-filenames",
        "--newline",
        "--progress",
        "--retries", "5",
        "--fragment-retries", "5",
        "--concurrent-fragments", "4",
        url,
    ]

    started = time.time()
    destinations: list[str] = []
    merges: list[str] = []
    alreadies: list[str] = []
    errors: list[str] = []

    # yt-dlp 在 DASH 站点上会先下视频、再下音频，两条进度各自从 0% 开始。
    # 这里按阶段把两条进度映射到 0-45% / 45-90%，并且保证整体进度不回退。
    state = {"last_pct": 0.0, "last_int": -1, "phase": ""}

    def report(overall: float, message: str, force: bool = False) -> None:
        pct = max(0.0, min(100.0, overall))
        if pct < state["last_pct"]:
            pct = state["last_pct"]  # 进度绝不回退
        ival = int(pct)
        if not force and ival == state["last_int"]:
            return  # 同一个整数百分比不重复推
        state["last_int"] = ival
        state["last_pct"] = pct
        notify(pct, message)

    _AUDIO_PART = re.compile(r"\.f\d+\.(?:m4a|aac|mp3|opus|ogg|weba)$", re.I)
    _DASH_PART = re.compile(r"\.f\d+\.", re.I)

    def handle_line(line: str) -> None:
        line = line.strip()
        if not line:
            return
        m = _MERGING.search(line)
        if m:
            merges.append(m.group(1))
            report(98.0, "合并音视频…", force=True)
            return
        m = _ALREADY.search(line)
        if m:
            alreadies.append(m.group(1))
            return
        m = _DESTINATION.search(line)
        if m:
            name = m.group(1)
            destinations.append(name)
            if _AUDIO_PART.search(name):
                state["phase"] = "audio"
                report(45.0, "开始下载音频…", force=True)
            elif _DASH_PART.search(name):
                state["phase"] = "video"
                report(1.0, "开始下载视频…", force=True)
            else:
                state["phase"] = "single"
                report(1.0, "开始下载…", force=True)
            return
        m = _PERCENT.search(line)
        if m:
            pct = float(m.group(1))
            size = _TOTAL.search(line)
            suffix = f"（{size.group(2)}）" if size else ""
            phase = state["phase"]
            if phase == "audio":
                report(45.0 + pct * 0.45, f"下载中（音频）… {pct:.0f}%{suffix}")
            elif phase == "video":
                report(pct * 0.45, f"下载中（视频）… {pct:.0f}%{suffix}")
            else:
                report(pct * 0.95, f"下载中… {pct:.0f}%{suffix}")
            return
        if line.startswith("ERROR:"):
            errors.append(line)
        elif line.startswith("WARNING:"):
            return
        elif not line.startswith("["):
            errors.append(line)

    notify(0.0, "正在获取视频信息…")

    log_path = ""
    try:
        with tempfile.NamedTemporaryFile("wb", prefix="dl-", suffix=".log", delete=False) as sink:
            log_path = sink.name
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=sink,
                stderr=subprocess.STDOUT,
                env=media.child_env(),
            )

        read_pos = 0
        buffer = ""

        def consume() -> None:
            nonlocal read_pos, buffer
            try:
                with open(log_path, "rb") as fh:
                    fh.seek(read_pos)
                    chunk = fh.read()
                    read_pos = fh.tell()
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk.decode("utf-8", "replace").replace("\r", "\n")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                handle_line(line)

        while proc.returncode is None:
            await asyncio.sleep(0.35)
            consume()
        await proc.wait()
        consume()
        handle_line(buffer)

        if errors and proc.returncode != 0:
            raise DownloadError(friendly_error(errors[0], url))

        path = await _resolve_output(
            dest_dir, merges, destinations, alreadies, started
        )
        if not path:
            raise DownloadError(
                "下载进程结束了，但没在下载目录里找到视频文件。"
                "可能是站点返回了页面而不是视频，换个链接试试。"
            )
    finally:
        if log_path:
            try:
                os.unlink(log_path)
            except OSError:
                pass

    size = 0
    try:
        size = os.path.getsize(path)
    except OSError:
        pass
    notify(100.0, f"下载完成（{media.human_size(size)}）")
    return {
        "path": path,
        "bytes": size,
        "title": os.path.splitext(os.path.basename(path))[0],
        "dir": dest_dir,
    }


async def _resolve_output(
    dest_dir: str,
    merges: list[str],
    destinations: list[str],
    alreadies: list[str],
    started: float,
) -> Optional[str]:
    """从 yt-dlp 的日志里定位最终文件；不行就在目录里找最新的视频。"""
    for candidate in (list(reversed(merges)) + list(reversed(alreadies))
                      + list(reversed(destinations))):
        candidate = candidate.strip().strip('"')
        if candidate and os.path.isfile(candidate) and candidate.lower().endswith(media.VIDEO_EXTS):
            return candidate

    # 合并失败时，最后一条 Destination 可能是纯音频分片，所以再兜一层：
    # 找本次运行期间新产生（或刚更新）的视频文件，取最大的那个。
    best: Optional[str] = None
    best_size = -1
    try:
        entries = list(os.scandir(dest_dir))
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith(media.VIDEO_EXTS):
                continue
            if entry.stat().st_mtime < started - 5:
                continue
            size = entry.stat().st_size
        except OSError:
            continue
        if size > best_size:
            best, best_size = entry.path, size
    return best
