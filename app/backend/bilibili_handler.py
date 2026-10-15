"""Bilibili video parsing, downloading and subtitle extraction.

Rewritten to be defensive: every step that used to crash with an opaque
``IndexError: tuple index out of range`` now either recovers or raises a
message a human can act on.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
from typing import Callable, Optional

import httpx
from bilibili_api import video, Credential
from bilibili_api.utils.aid_bvid_transformer import aid2bvid

from . import media

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_DOWNLOAD_PERCENT = re.compile(r"\[download\]\s+([\d.]+)%")
_DESTINATION = re.compile(r"\[download\] Destination:\s*(.+)")


class BilibiliError(Exception):
    """Error with a message that is safe to show in the UI."""


_YTDLP_BOILERPLATE = re.compile(
    r";?\s*please report this issue on\s+https?://\S+.*$"
    r"|Confirm you are on the latest version using\s+yt-dlp -U\s*",
    re.I | re.S,
)

_FRIENDLY_ERRORS: tuple[tuple[re.Pattern, str], ...] = (
    (
        re.compile(r"No video formats found|HTTP Error 412|Too Many Requests", re.I),
        "B 站接口暂时拒绝了请求（访问过于频繁或需要登录）。等 1~2 分钟再试；"
        "如果一直失败，请在设置里填入 B 站 Cookie。",
    ),
    (
        re.compile(r"This video is unavailable|稿件不可见|not available in your country|地区", re.I),
        "这个视频当前不可访问（可能已删除、仅会员可见，或限制地区）。",
    ),
    (
        re.compile(r"Sign in to confirm|Premium member|大会员", re.I),
        "该清晰度需要大会员。已在设置里配置 B 站 Cookie 后可下载更高清晰度。",
    ),
    (
        re.compile(r"Unsupported URL|is not a valid URL", re.I),
        "无法识别这个链接，请确认是 B 站视频地址。",
    ),
)


def friendly_download_error(raw: str) -> str:
    """Turn yt-dlp's multi-line English report into one actionable sentence."""
    text = (raw or "").replace("ERROR:", " ").strip()
    text = _YTDLP_BOILERPLATE.sub("", text).strip()
    for pattern, message in _FRIENDLY_ERRORS:
        if pattern.search(text):
            return message
    # Keep the short remainder so nothing is silently swallowed.
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), text)
    return first_line[:200]


class BilibiliHandler:
    """Handle Bilibili video parsing, downloading, and subtitle extraction."""

    def __init__(self, credential: Optional[Credential] = None):
        self.credential = credential or Credential()
        self.http = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        )

    # ------------------------------------------------------------------ URL --

    async def resolve_url(self, raw: str) -> str:
        """Extract a Bilibili URL from mixed text and resolve short links."""
        urls = re.findall(r"https?://[^\s）)】\]]+", raw or "")
        target = urls[0] if urls else (raw or "").strip()
        target = target.rstrip("。，,.、")
        if not target:
            raise BilibiliError("没有找到链接，请粘贴 B 站视频地址")

        # Follow b23.tv / bili2233.cn short links.
        if re.search(r"(b23\.tv|bili2233\.cn|b23\.ws)/", target):
            try:
                resp = await self.http.head(target)
                if resp.status_code >= 400:
                    resp = await self.http.get(target)
                target = str(resp.url)
            except Exception as exc:  # noqa: BLE001
                raise BilibiliError(f"无法解析短链接：{target}（{exc}）") from exc
        return target

    async def parse_target(self, raw: str) -> dict:
        """Return ``{"kind": "video"|"episode", "id": ..., "url": ...}``."""
        url = await self.resolve_url(raw)

        m = re.search(r"bilibili\.com/video/(BV[0-9A-Za-z]+)", url)
        if m:
            return {"kind": "video", "id": m.group(1), "url": f"https://www.bilibili.com/video/{m.group(1)}"}

        m = re.search(r"bilibili\.com/video/av(\d+)", url)
        if m:
            bvid = aid2bvid(int(m.group(1)))
            return {"kind": "video", "id": bvid, "url": f"https://www.bilibili.com/video/{bvid}"}

        m = re.search(r"bilibili\.com/v/av(\d+)", url)
        if m:
            bvid = aid2bvid(int(m.group(1)))
            return {"kind": "video", "id": bvid, "url": f"https://www.bilibili.com/video/{bvid}"}

        m = re.search(r"bilibili\.com/bangumi/play/ep(\d+)", url)
        if m:
            return {"kind": "episode", "id": int(m.group(1)), "url": f"https://www.bilibili.com/bangumi/play/ep{m.group(1)}"}

        # Bare BV id pasted on its own, or a BV id inside leftover share text.
        m = re.search(r"(BV[0-9A-Za-z]{10,})", url)
        if m:
            return {"kind": "video", "id": m.group(1), "url": f"https://www.bilibili.com/video/{m.group(1)}"}

        raise BilibiliError(f"无法解析 B 站链接：{(raw or '').strip()[:80]}")

    async def parse_bvid(self, raw: str) -> str:
        """Backwards compatible helper: return the BV id for a video link."""
        target = await self.parse_target(raw)
        if target["kind"] != "video":
            raise BilibiliError("该链接是番剧/剧集，请直接解析，不要当作 BV 号使用")
        return target["id"]

    async def _as_target(self, value) -> dict:
        """Accept a raw link, a ``parse_target`` dict, or a ``parse_video`` dict."""
        if isinstance(value, str):
            return await self.parse_target(value)
        if isinstance(value, dict):
            if value.get("kind") and value.get("id") is not None:
                return value
            url = value.get("url")
            if url:
                return await self.parse_target(url)
            if value.get("bvid"):
                return {
                    "kind": "video",
                    "id": value["bvid"],
                    "url": f"https://www.bilibili.com/video/{value['bvid']}",
                }
            if value.get("epid"):
                return {
                    "kind": "episode",
                    "id": int(value["epid"]),
                    "url": f"https://www.bilibili.com/bangumi/play/ep{value['epid']}",
                }
        raise BilibiliError("无法识别的视频标识，请重新粘贴链接")

    def _entity(self, target: dict):
        if target["kind"] == "episode":
            from bilibili_api import bangumi

            return bangumi.Episode(epid=target["id"], credential=self.credential)
        return video.Video(bvid=target["id"], credential=self.credential)

    # -------------------------------------------------------------- parsing --

    async def parse_video(self, raw_url: str) -> dict:
        """Get video metadata: title, duration, cover, uploader, stats, pages."""
        target = await self.parse_target(raw_url)
        entity = self._entity(target)
        info = await entity.get_info()

        pages = info.get("pages") or []
        stat = info.get("stat") or {}
        owner = info.get("owner") or {}

        # A multi-part video (合集/分P): the first page is what we process.
        first_page = pages[0] if pages else {}
        cid = info.get("cid") or first_page.get("cid")
        if not cid and target["kind"] == "episode":
            cid = info.get("cid")

        subtitle_list = ((info.get("subtitle") or {}).get("list")) or []

        return {
            "kind": target["kind"],
            "bvid": info.get("bvid") or (target["id"] if target["kind"] == "video" else ""),
            "aid": info.get("aid"),
            "cid": cid,
            "epid": target["id"] if target["kind"] == "episode" else None,
            "title": info.get("title") or info.get("season_title") or "未知标题",
            "desc": (info.get("desc") or "").strip(),
            "duration": info.get("duration") or first_page.get("duration") or 0,
            "pic": info.get("pic") or info.get("cover") or "",
            "pubdate": info.get("pubdate") or 0,
            "tname": info.get("tname") or "",
            "owner": {
                "mid": owner.get("mid"),
                "name": owner.get("name") or "",
                "face": owner.get("face") or "",
            },
            "stat": {
                "view": stat.get("view", 0),
                "danmaku": stat.get("danmaku", 0),
                "reply": stat.get("reply", 0),
                "favorite": stat.get("favorite", 0),
                "coin": stat.get("coin", 0),
                "share": stat.get("share", 0),
                "like": stat.get("like", 0),
            },
            "pages": [
                {
                    "page": p.get("page"),
                    "cid": p.get("cid"),
                    "part": p.get("part") or "",
                    "duration": p.get("duration") or 0,
                }
                for p in pages
            ],
            "has_subtitle": bool(subtitle_list),
            "subtitle_list": subtitle_list,
            "quality": info.get("quality", 80),
            "url": target["url"],
        }

    # ------------------------------------------------------------ download --

    async def download_video(
        self,
        target,
        cache_dir: str,
        quality: int = 80,
        progress_cb: Optional[Callable[[dict], None]] = None,
    ) -> tuple[str, str]:
        """Download a video. Returns ``(video_path, audio_path)``.

        Guarantees that the returned path contains a real audio stream —
        that guarantee is what stops faster-whisper from dying with
        ``IndexError: tuple index out of range``.
        """
        if isinstance(target, str) or not (isinstance(target, dict) and target.get("kind") and target.get("id") is not None):
            target = await self._as_target(target)

        os.makedirs(cache_dir, exist_ok=True)
        bvid = target["id"] if target["kind"] == "video" else f"ep{target['id']}"
        url = target["url"]

        def notify(stage: str, percent: Optional[float], message: str = ""):
            if progress_cb:
                try:
                    progress_cb({"stage": stage, "percent": percent, "message": message})
                except Exception:  # noqa: BLE001 - progress must never break a run
                    pass

        ffmpeg_dir = media.ffmpeg_location()
        if not ffmpeg_dir:
            raise BilibiliError(
                "找不到 ffmpeg。视频与音频无法合并，请把完整压缩包里的 ffmpeg 目录放回原位"
                "（需含 ffmpeg\\bin\\ffmpeg.exe），或设置环境变量 MYBOX_FFMPEG 指向可执行文件。"
            )

        # 找到 yt-dlp 可执行文件就用它；找不到（便携包 wheel 手动解包不生成控制台
        # 脚本）就用 `python -m yt_dlp`，两者行为一致。
        yt_dlp_exe = media.find_tool("yt-dlp")
        yt_dlp_cmd = [yt_dlp_exe] if yt_dlp_exe else [sys.executable, "-m", "yt_dlp"]
        output_template = os.path.join(cache_dir, f"{bvid}.%(ext)s")

        cmd = [
            *yt_dlp_cmd,
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b[ext=mp4]/b",
            "--merge-output-format", "mp4",
            "--ffmpeg-location", ffmpeg_dir,
            "-o", output_template,
            "--no-playlist",
            "--no-mtime",
            "--newline",
            "--progress",
            "--retries", "5",
            "--fragment-retries", "5",
            "--concurrent-fragments", "4",
            "--user-agent", UA,
            "--referer", "https://www.bilibili.com/",
            url,
        ]

        notify("prepare", 0, "准备下载…")

        # A cached file that already has audio means we can skip yt-dlp (and its
        # API round-trips) entirely — re-running a video is common, and Bilibili
        # rate-limits aggressively.
        cached = await asyncio.to_thread(media.pick_media_file, cache_dir, bvid)
        if cached and await media.has_audio_stream_async(cached):
            size = os.path.getsize(cached)
            notify("download", 100.0, f"使用已缓存视频（{media.human_size(size)}）")
            return cached, cached

        # Bilibili answers "No video formats found" / -412 when it rate-limits a
        # burst of API calls. Those failures are transient, so retry with a
        # backoff before giving up.
        attempt_errors: list[str] = []
        for attempt in range(3):
            returncode, errors = await self._run_yt_dlp(cmd, notify)
            if returncode == 0:
                attempt_errors = []
                break
            detail = next(
                (e for e in errors if e.startswith("ERROR:")),
                errors[0] if errors else f"yt-dlp 退出码 {returncode}",
            )
            attempt_errors = [detail]
            transient = re.search(
                r"No video formats found|HTTP Error 412|Too Many Requests|"
                r"Unable to download|read timed out|Connection reset",
                detail,
                re.I,
            )
            if not transient or attempt == 2:
                break
            wait = 3 * (attempt + 1)
            notify("download", 1.0, f"接口被限流，{wait} 秒后重试（第 {attempt + 2} 次）…")
            await asyncio.sleep(wait)

        if attempt_errors:
            raise BilibiliError(f"视频下载失败：{friendly_download_error(attempt_errors[0])}")

        notify("merge", 95.0, "校验媒体文件…")
        video_path = await asyncio.to_thread(media.pick_media_file, cache_dir, bvid)
        if not video_path:
            # yt-dlp can leave *.fNNN.mp4 / *.fNNN.m4a fragments behind when the
            # merge step fails; rebuild the file ourselves instead of giving up.
            video_path = await self._repair_media(cache_dir, bvid, None)
        if not video_path:
            raise BilibiliError(
                "下载完成但没有找到可用的视频文件（音视频可能没有合并成功）。"
                "请检查 ffmpeg 是否可用后重试。"
            )

        if not await media.has_audio_stream_async(video_path):
            video_path = await self._repair_media(cache_dir, bvid, video_path)
        if not video_path or not await media.has_audio_stream_async(video_path):
            raise BilibiliError(
                "下载到的文件没有音轨（视频与音频没有合并成功）。"
                "请安装 ffmpeg 后重试，或删除缓存目录 ~/.bilibili-notes/cache 再试。"
            )

        size = os.path.getsize(video_path)
        notify("download", 100.0, f"下载完成（{media.human_size(size)}）")
        return video_path, video_path

    async def _run_yt_dlp(self, cmd: list[str], notify) -> tuple[int, list[str]]:
        """Run yt-dlp, streaming progress as it happens. Returns (rc, errors).

        Two hard-won details live here:

        * yt-dlp reports download progress on **stdout** (stderr carries only
          diagnostics), so both streams have to be observed — reading stderr
          alone made a multi-minute download look frozen;
        * output goes to a *file*, not a pipe. yt-dlp hands its stdout to the
          ffmpeg it spawns to merge, and asyncio's ``Process.wait()`` does not
          resolve until every pipe closes — so a lingering grandchild could
          deadlock the whole pipeline. A regular file has no EOF semantics: the
          process exits, ``wait()`` returns, and we read whatever landed.
        """
        errors: list[str] = []
        state = {"audio_part_seen": False}
        read_pos = [0]
        buffer = ""

        def handle(part: str) -> None:
            if not part:
                return
            dest = _DESTINATION.search(part)
            if dest:
                # yt-dlp downloads video first, then audio: use that to scale 0-100.
                if re.search(r"\.f\d+\.(m4a|aac|mp3)", dest.group(1)):
                    state["audio_part_seen"] = True
                notify("download", 45.0 if state["audio_part_seen"] else 1.0, "下载中…")
                return

            m = _DOWNLOAD_PERCENT.search(part)
            if m:
                pct = float(m.group(1))
                overall = 45.0 + pct * 0.5 if state["audio_part_seen"] else pct * 0.45
                notify("download", min(overall, 90.0), f"下载中… {pct:.0f}%")
                return

            if "ERROR:" in part:
                errors.append(part)
            elif "[Merger]" in part or "Merging" in part:
                notify("merge", 93.0, "合并音视频…")
            elif part.startswith("WARNING:"):
                return  # never surface yt-dlp warnings as the failure reason
            elif not part.startswith("["):
                errors.append(part)

        def consume() -> None:
            nonlocal buffer
            try:
                with open(log_path, "rb") as handle_file:
                    handle_file.seek(read_pos[0])
                    chunk = handle_file.read()
                    read_pos[0] = handle_file.tell()
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk.decode("utf-8", "replace").replace("\r", "\n")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                handle(line.strip())

        log_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                "wb", prefix="ytdlp-", suffix=".log", delete=False
            ) as sink:
                log_path = sink.name
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    env=media.child_env(),
                )

            while proc.returncode is None:
                await asyncio.sleep(0.4)
                consume()
            await proc.wait()
            consume()
            handle(buffer.strip())
            return proc.returncode, errors
        finally:
            if log_path:
                try:
                    os.unlink(log_path)
                except OSError:
                    pass

    async def _repair_media(self, cache_dir: str, bvid: str, video_path: Optional[str]) -> Optional[str]:
        """Rebuild a playable file from yt-dlp's leftover audio/video fragments."""
        try:
            names = sorted(os.listdir(cache_dir))
        except OSError:
            return None

        best_video: Optional[str] = None
        best_audio: Optional[str] = None
        best_video_size = -1
        best_audio_size = -1

        for name in names:
            if not name.startswith(bvid + ".") or name.endswith(".part"):
                continue
            full = os.path.join(cache_dir, name)
            if not os.path.isfile(full):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if name.endswith(media.VIDEO_EXTS):
                if not await media.has_video_stream_async(full):
                    continue
                if size > best_video_size:
                    best_video, best_video_size = full, size
            elif await media.has_audio_stream_async(full) and not await media.has_video_stream_async(full):
                if size > best_audio_size:
                    best_audio, best_audio_size = full, size

        source_video = video_path
        if not source_video or not await media.has_video_stream_async(source_video):
            source_video = best_video
        if not source_video or not best_audio:
            return None

        out_path = os.path.join(cache_dir, f"{bvid}.merged.mp4")
        if await media.mux_async(source_video, best_audio, out_path) and await media.has_audio_stream_async(out_path):
            print(f"[download] repaired media -> {out_path}")
            return out_path
        return None

    # ------------------------------------------------------------ subtitles --

    async def get_subtitles(
        self,
        target,
        cid: Optional[int] = None,
        known_subtitles: Optional[list] = None,
        timeout: float = 20.0,
    ) -> Optional[dict]:
        """Extract Bilibili subtitles.

        Returns ``{"source": str, "segments": [{"from", "to", "text"}], "text": str}``
        or ``None`` when the video has no usable subtitle track.

        Two things keep this fast and safe:

        * the player endpoint is only queried when a login cookie exists — it
          always answers ``CredentialNoSessdataException`` otherwise, and each
          doomed request pushes Bilibili closer to rate-limiting us (-412);
        * the subtitle list already returned by ``parse_video`` is reused, so a
          video without subtitles costs zero extra API calls.
        """
        if isinstance(target, str) or not (isinstance(target, dict) and target.get("kind") and target.get("id") is not None):
            target = await self._as_target(target)
        entity = self._entity(target)

        authenticated = bool(getattr(self.credential, "sessdata", None))
        subtitle_list: list[dict] = list(known_subtitles or [])
        list_was_provided = known_subtitles is not None

        if not subtitle_list and authenticated and cid:
            payload = await entity.get_subtitle(cid)
            if isinstance(payload, dict):
                subtitle_list = payload.get("subtitles") or []

        # Last resort: ask the view endpoint (it embeds a list on older videos).
        if not subtitle_list and not list_was_provided:
            try:
                info = await entity.get_info()
                subtitle_list = ((info.get("subtitle") or {}).get("list")) or []
            except Exception:  # noqa: BLE001
                subtitle_list = []

        entry = self._pick_subtitle(subtitle_list)
        if not entry:
            return None

        url = entry.get("subtitle_url") or entry.get("subtitleUrl") or ""
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url

        try:
            resp = await self.http.get(
                url,
                headers={"User-Agent": UA, "Referer": target["url"]},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise BilibiliError(f"字幕下载失败：{exc}") from exc

        segments = []
        for item in data.get("body", []) or []:
            content = (item.get("content") or "").strip()
            if not content:
                continue
            segments.append(
                {
                    "from": float(item.get("from") or 0),
                    "to": float(item.get("to") or 0),
                    "text": content,
                }
            )
        if not segments:
            return None

        text = "\n".join(f"[{format_timestamp(s['from'])}] {s['text']}" for s in segments)
        return {
            "source": entry.get("lan_doc") or entry.get("lan") or "B站字幕",
            "segments": segments,
            "text": text,
        }

    @staticmethod
    def _pick_subtitle(subtitle_list: list[dict]) -> Optional[dict]:
        """Prefer a usable Simplified-Chinese track, then any usable track."""
        usable = [s for s in subtitle_list if s.get("subtitle_url") or s.get("subtitleUrl")]
        if not usable:
            return None
        for wanted in ("zh-Hans", "zh-CN", "ai-zh"):
            for sub in usable:
                if str(sub.get("lan", "")) == wanted:
                    return sub
        for sub in usable:
            lan = str(sub.get("lan", ""))
            doc = str(sub.get("lan_doc", ""))
            if lan.startswith("zh") or "中文" in doc or "简体" in doc:
                return sub
        return usable[0]

    async def close(self):
        await self.http.aclose()


def format_timestamp(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
