"""抖音支持：借真实浏览器取流，不实现签名。

抖音的取流接口要求 ``a_bogus`` 签名，而这个签名是网页里的 JS 现算的。
纯 HTTP 路已经全被堵死（分享页不含地址、老 iteminfo 接口返回空、
新 detail 接口 403 ArgusSecurityPlugin，yt-dlp 也卡在这个 TODO 上）。

所以这里换了个思路：启动一个无头 Edge 打开视频页，让抖音自己的 JS
去签名、去发请求，我们从 CDP 的网络层把**已经拿到的**音视频地址截下来，
再带着 Referer 直接下载两个轨，交给后面的流水线。

这样不需要用户登录、也不需要 Cookie；代价是要起一个浏览器（几秒）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Callable, Optional

import httpx

from . import media

SHORT_HOST = re.compile(r"(?:v\.douyin\.com|douyin\.com/[A-Za-z0-9]+/?$)", re.I)
AWEME_IN_URL = re.compile(r"/video/(\d{15,25})")
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REFERER = "https://www.douyin.com/"

_BROWSER_CANDIDATES = (
    os.environ.get("MYBOX_BROWSER", ""),
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
)


class DouyinError(Exception):
    """可直接展示给用户的中文消息。"""


def is_douyin(url: str) -> bool:
    return bool(re.search(r"(?:^|\.)douyin\.com|iesdouyin\.com", url or "", re.I))


def find_browser() -> str:
    for path in _BROWSER_CANDIDATES:
        if path and os.path.isfile(path) and path.lower().endswith(".exe"):
            return path
    raise DouyinError(
        "没找到可用的浏览器。收起抖音需要一个 Edge 或 Chrome，"
        "请确认系统里装了其中一个。"
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill(proc: Optional[subprocess.Popen]) -> None:
    if not proc:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                       capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


async def resolve_aweme_id(raw: str) -> str:
    """从短链或完整链接里拿到作品 id。"""
    text = (raw or "").strip()
    m = AWEME_IN_URL.search(text)
    if m:
        return m.group(1)

    urls = re.findall(r"https?://[^\s\"'）)】\]，。]+", text)
    target = urls[0].rstrip("。，,.、；;！!？?）)】]}>\"'") if urls else ""
    if not target:
        raise DouyinError("没从这段内容里找到抖音链接")

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20,
                                     headers={"User-Agent": DESKTOP_UA}) as client:
            resp = await client.get(target)
        m = AWEME_IN_URL.search(str(resp.url))
        if m:
            return m.group(1)
    except Exception as exc:  # noqa: BLE001
        raise DouyinError(f"抖音短链打不开：{exc}") from exc
    raise DouyinError("这个抖音链接里没有找到作品 id，可能不是视频链接")


async def _capture(video_id: str, wait_seconds: float = 30.0) -> dict:
    """启动无头浏览器，截获页面自己请求到的音视频地址。"""
    try:
        import websockets  # noqa: F401  延迟导入：缺了也不该拖垮整个后端
    except ImportError as exc:  # pragma: no cover
        raise DouyinError("缺少 websockets 依赖，无法驱动浏览器取流") from exc

    import websockets  # 上面已确认可用

    browser = find_browser()
    port = _free_port()
    profile = os.path.join(tempfile.gettempdir(), f"bnotes-douyin-{port}")
    shutil.rmtree(profile, ignore_errors=True)

    url = f"https://www.douyin.com/video/{video_id}"
    proc = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--mute-audio",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--window-size=1280,900",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=media.child_env() if hasattr(media, "child_env") else None,
    )

    cdp = f"http://127.0.0.1:{port}"
    page = None
    media_urls = {"video": None, "audio": None}
    meta: dict = {"title": "", "cover": ""}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            for _ in range(60):
                try:
                    targets = (await client.get(f"{cdp}/json/list")).json()
                    page = next((t for t in targets if t.get("type") == "page"), None)
                    if page and page.get("webSocketDebuggerUrl"):
                        break
                except Exception:  # noqa: BLE001
                    pass
                page = None
                await asyncio.sleep(0.5)

        if not page:
            raise DouyinError("浏览器没能启动（可能是被安全软件拦了）")

        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 << 20,
                                      open_timeout=20, close_timeout=5) as ws:
            counter = 0

            async def call(method: str, **params):
                nonlocal counter
                counter += 1
                mine = counter
                await ws.send(json.dumps({"id": mine, "method": method, "params": params}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    msg = json.loads(raw)
                    if msg.get("method") == "Network.responseReceived":
                        resp = msg["params"]["response"]
                        link = resp.get("url", "")
                        if "douyinvod" in link:
                            if "/media-video-" in link and not media_urls["video"]:
                                media_urls["video"] = link
                            elif "/media-audio-" in link and not media_urls["audio"]:
                                media_urls["audio"] = link
                    if msg.get("id") == mine:
                        return msg

            await call("Network.enable")
            await call("Runtime.enable")
            await call("Page.enable")
            await call("Page.navigate", url=url)

            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                if media_urls["video"] and media_urls["audio"]:
                    break
                await asyncio.sleep(1.0)

            # 顺手把标题、封面和时长捞出来（时长读播放器自身的元数据）
            try:
                result = await call(
                    "Runtime.evaluate",
                    expression=(
                        "(()=>{const og=document.querySelector('meta[property=\"og:image\"]');"
                        "const t=document.querySelector('meta[property=\"og:title\"]');"
                        "const v=document.querySelector('video');"
                        "return JSON.stringify({"
                        "title:(t&&t.content)||document.title||'',"
                        "cover:(og&&og.content)||'',"
                        "duration:(v&&isFinite(v.duration))?Math.round(v.duration):0});})()"
                    ),
                    returnByValue=True,
                )
                raw = result.get("result", {}).get("result", {}).get("value") or "{}"
                meta.update(json.loads(raw))
            except Exception:  # noqa: BLE001
                pass
    finally:
        _kill(proc)
        await asyncio.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)

    if not media_urls["video"]:
        raise DouyinError(
            "没能在页面里截到视频流。可能是这条作品被删了、需要登录，"
            "或者抖音又改版了。可以先用浏览器打开确认视频能播。"
        )

    title = re.sub(r"\s*-\s*抖音\s*$", "", (meta.get("title") or "").strip())
    return {
        "video": media_urls["video"],
        "audio": media_urls["audio"],
        "title": title or f"抖音作品 {video_id}",
        "cover": meta.get("cover") or "",
        "duration": int(meta.get("duration") or 0) or None,
    }


def _safe_name(name: str, limit: int = 110) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name or "").strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return (cleaned or "抖音视频")[:limit].rstrip(" .")


async def _download(url: str, dest: str, on_bytes: Optional[Callable[[int], None]] = None) -> int:
    total = 0
    headers = {"User-Agent": DESKTOP_UA, "Referer": REFERER}
    async with httpx.AsyncClient(timeout=180, follow_redirects=True, headers=headers) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(262144):
                    f.write(chunk)
                    total += len(chunk)
                    if on_bytes:
                        on_bytes(total)
    return total


async def _mux(video_path: str, audio_path: Optional[str], out_path: str) -> None:
    if not audio_path:
        shutil.copy2(video_path, out_path)
        return
    ok = await media.mux_async(video_path, audio_path, out_path)
    if not ok or not os.path.isfile(out_path):
        raise DouyinError("音视频合成失败，可以检查一下 ffmpeg 是否完整")


async def _probe_duration(path: str) -> Optional[int]:
    ffprobe = media.ffprobe_path()
    if not ffprobe:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return int(float((out or b"").decode().strip()))
    except Exception:  # noqa: BLE001
        return None


class DouyinHandler:
    """接口与 BilibiliHandler 对齐，方便直接插进现有流水线。"""

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    async def parse_video(self, raw: str) -> dict:
        video_id = await resolve_aweme_id(raw)
        captured = await _capture(video_id)
        self._cache[video_id] = captured

        info = {
            "platform": "Douyin",
            "bvid": video_id,          # 用作文件名去重键与截图前缀
            "aweme_id": video_id,
            "title": captured["title"],
            "url": f"https://www.douyin.com/video/{video_id}",
            "desc": "",
            "cover": captured["cover"],
            "pic": captured["cover"],   # 界面卡片读的是 pic
            "duration": captured.get("duration"),
            "owner": {"name": ""},
            "stat": {},
            "cid": None,
            "subtitle_list": [],
            "tname": "",
        }
        return info

    async def download_video(
        self,
        info: dict,
        dest_dir: str,
        progress_cb: Optional[Callable[[dict], None]] = None,
    ) -> tuple[str, Optional[str]]:
        video_id = info.get("aweme_id") or info.get("bvid") or ""
        captured = self._cache.get(video_id)
        if not captured:
            captured = await _capture(video_id)
            self._cache[video_id] = captured

        os.makedirs(dest_dir, exist_ok=True)
        stem = _safe_name(info.get("title") or "douyin")
        video_path = os.path.join(dest_dir, f"{stem}.video.mp4")
        audio_path = os.path.join(dest_dir, f"{stem}.audio.m4a") if captured["audio"] else None

        def report(message: str, pct: Optional[float] = None):
            if progress_cb:
                progress_cb({"percent": pct, "message": message})

        report("正在下载抖音视频…", 5.0)
        await _download(captured["video"], video_path)
        report("视频轨完成，正在下载音频…", 55.0)
        if audio_path:
            await _download(captured["audio"], audio_path)
        report("下载完成", 96.0)

        duration = await _probe_duration(video_path)
        if duration:
            info["duration"] = duration

        return video_path, audio_path

    async def get_subtitles(self, *args, **kwargs) -> None:
        return None

    async def close(self) -> None:
        self._cache.clear()


async def download_merged(
    raw: str,
    dest_dir: str,
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> dict:
    """纯下载用：取流 → 下两轨 → 合成一个 mp4 落到目标目录。"""
    def notify(pct: float, msg: str) -> None:
        if on_progress:
            on_progress(pct, msg)

    handler = DouyinHandler()
    try:
        notify(2.0, "正在打开抖音页面…")
        info = await handler.parse_video(raw)
        os.makedirs(dest_dir, exist_ok=True)

        video_id = info["aweme_id"]
        captured = handler._cache[video_id]

        notify(8.0, "正在下载视频轨…")
        tmp_dir = tempfile.mkdtemp(prefix="bnotes-dy-")
        v_tmp = os.path.join(tmp_dir, "v.mp4")
        a_tmp = os.path.join(tmp_dir, "a.m4a") if captured["audio"] else None
        try:
            await _download(captured["video"], v_tmp)
            notify(45.0, "正在下载音频轨…")
            if a_tmp:
                await _download(captured["audio"], a_tmp)
            notify(92.0, "正在合成…")
            final = os.path.join(dest_dir, _safe_name(info["title"]) + ".mp4")
            await _mux(v_tmp, a_tmp, final)
            notify(100.0, "下载完成")
            return {
                "path": final,
                "bytes": os.path.getsize(final),
                "title": info["title"],
                "dir": dest_dir,
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    finally:
        await handler.close()
