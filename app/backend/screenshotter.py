import asyncio
import os
import re
import subprocess
from typing import List, Optional

from . import media


async def take_screenshots(
    video_path: str,
    sections: List[dict],
    bvid: str,
    cache_dir: str,
) -> List[dict]:
    """
    Take screenshots at specified timestamps using ffmpeg.
    Returns sections with screenshot paths filled in.
    """
    if not video_path or not os.path.exists(video_path):
        print(f"[screenshots] video not found: {video_path}")
        return sections

    ffmpeg = media.ffmpeg_path()
    if not ffmpeg:
        print("[screenshots] ffmpeg not found; skipping screenshots")
        return sections

    assets_dir = os.path.join(cache_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    for index, section in enumerate(sections):
        if not isinstance(section, dict) or not section.get("need_screenshot"):
            continue
        time_str = section.get("time") or "00:00:00"
        screenshot_path = await _take_single_screenshot(
            ffmpeg, video_path, time_str, bvid, assets_dir, index + 1
        )
        if screenshot_path:
            section["screenshot_path"] = screenshot_path

    return sections


def _to_seconds(time_str: str) -> Optional[float]:
    """Parse ``HH:MM:SS`` / ``MM:SS`` / ``SS`` (tolerating a decimal part)."""
    if not time_str:
        return None
    parts = str(time_str).strip().replace("：", ":").split(":")
    try:
        values = [float(p) for p in parts if p != ""]
    except ValueError:
        return None
    if not values:
        return None
    seconds = 0.0
    for value in values:
        seconds = seconds * 60 + value
    return seconds


def _stamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def _take_single_screenshot(
    ffmpeg: str,
    video_path: str,
    time_str: str,
    bvid: str,
    assets_dir: str,
    index: int,
) -> str:
    """
    Capture one frame, retrying at small offsets when a timestamp lands on a
    blank transition. Returns the filename relative to assets_dir, or "".
    """
    seconds = _to_seconds(time_str)
    if seconds is None:
        seconds = 0.0

    candidates = [seconds] + [max(0.0, seconds + off) for off in (2, -2, 4, -4, 1, 0.5)]
    for attempt, moment in enumerate(candidates):
        stamp = _stamp(moment)
        filename = f"{bvid}_{index:02d}_{stamp.replace(':', '_')}.webp"
        output_path = os.path.join(assets_dir, filename)
        if await _ffmpeg_screenshot(ffmpeg, video_path, moment, output_path):
            return filename
    return ""


async def _ffmpeg_screenshot(ffmpeg: str, video_path: str, seconds: float, output_path: str) -> bool:
    """Use ffmpeg to capture a single frame."""
    try:
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", f"{max(0.0, seconds):.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "3",
            "-f", "image2",
            output_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=media.child_env(),
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=45)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return (
            proc.returncode == 0
            and os.path.exists(output_path)
            and os.path.getsize(output_path) > 0
        )
    except Exception:  # noqa: BLE001
        return False
