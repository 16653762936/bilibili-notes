"""Bilibili Notes backend.

Pipeline: parse -> download -> (B站字幕 | 语音转文字) -> DeepSeek -> 截图 -> 预览 -> 保存
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import pathlib
import re
import time
import uuid
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend import media
from backend.bilibili_handler import BilibiliError, BilibiliHandler
from backend.config import DEFAULT_CONFIG, WHISPER_MODELS, load_config, public_config, save_config
from backend.downloader import DownloadError, download_video
from backend.note_generator import NoteGenerator, format_duration
from backend.screenshotter import take_screenshots
from backend.stt import TranscriptionError, transcribe_audio
from backend.summarizer import (
    DEFAULT_NOTE_STYLE,
    list_styles,
    polish_transcript,
    style_config,
    style_generates_transcript,
    summarize_text,
)

# Windows 注册表通常没有 .webp 映射，mimetypes.guess_type 会返回 None，
# Starlette 的 FileResponse 便兜底成 text/plain —— 笔记里的 webp 截图
# 全被标成 text/plain; charset=utf-8。这里显式注册，顺带补几个常见类型。
_EXTRA_MIME_TYPES = {
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".m4a": "audio/mp4",
    ".opus": "audio/opus",
    ".webm": "video/webm",
}
for _ext, _mime in _EXTRA_MIME_TYPES.items():
    mimetypes.add_type(_mime, _ext)

app = FastAPI(title="Bilibili Notes Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CACHE_DIR = os.path.expanduser("~/.bilibili-notes/cache")
ASSETS_DIR = os.path.join(CACHE_DIR, "assets")
COVERS_DIR = os.path.join(CACHE_DIR, "covers")
for _d in (CACHE_DIR, ASSETS_DIR, COVERS_DIR):
    os.makedirs(_d, exist_ok=True)

app.mount("/api/assets", StaticFiles(directory=ASSETS_DIR), name="assets")
app.mount("/api/covers", StaticFiles(directory=COVERS_DIR), name="covers")

# In-memory task store: task_id -> {queue, result?, save_path?, ...}
tasks: dict[str, dict] = {}


# --------------------------------------------------------------------- models


class ConfigRequest(BaseModel):
    deepseek_api_key: Optional[str] = None
    save_path: Optional[str] = None
    download_path: Optional[str] = None
    max_chars: Optional[int] = None
    prompt_template: Optional[str] = None
    sessdata: Optional[str] = None
    bili_jct: Optional[str] = None
    whisper_model: Optional[str] = None
    keep_media: Optional[bool] = None
    screenshot_enabled: Optional[bool] = None
    polish_transcript: Optional[bool] = None
    note_style: Optional[str] = None
    theme: Optional[str] = None
    auto_save: Optional[bool] = None


class ProcessRequest(BaseModel):
    url: str
    note_style: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    save_path: Optional[str] = None
    max_chars: Optional[int] = 80000
    prompt_template: Optional[str] = None


class InspectRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    """纯下载：只要视频文件，不解析、不做笔记。"""

    url: str
    download_path: Optional[str] = None
    quality: Optional[str] = None


class PickDirRequest(BaseModel):
    """弹原生文件夹选择框。"""

    initial: Optional[str] = ""
    title: Optional[str] = "选择下载目录"


class SaveRequest(BaseModel):
    save_path: Optional[str] = None
    note_title: Optional[str] = None


class PathRequest(BaseModel):
    path: str


class UrlRequest(BaseModel):
    url: str


# ------------------------------------------------------------------- helpers


def resolve_save_path(raw: Optional[str]) -> str:
    """Notes always live in a ``bilibilinote`` folder inside the chosen vault."""
    if not raw:
        base = "~/Documents" if media.IS_WINDOWS else "~/Desktop"
        return os.path.expanduser(os.path.join(base, "bilibilinote"))
    path = os.path.expanduser(str(raw).strip())
    if os.path.basename(path.rstrip("/")) != "bilibilinote":
        path = os.path.join(path, "bilibilinote")
    return path


def resolve_download_path(raw: Optional[str]) -> str:
    """「下载视频」存哪：留空就用系统的「下载」文件夹。

    与笔记目录不同，这里**不**再套一层子目录 —— 用户指定哪儿就存哪儿。
    """
    if not raw or not str(raw).strip():
        return os.path.expanduser("~/Downloads")
    return os.path.expanduser(str(raw).strip())


def build_credential():
    """Optional B站 credential so official subtitles can be fetched."""
    from bilibili_api import Credential

    cfg = load_config()
    sessdata = (cfg.get("sessdata") or "").strip()
    if not sessdata:
        return Credential()
    try:
        return Credential(sessdata=sessdata, bili_jct=(cfg.get("bili_jct") or "").strip())
    except Exception:  # noqa: BLE001
        return Credential()


def format_duration_short(seconds) -> str:
    return format_duration(seconds) or ""


def _read_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip().strip('"')
    return data


# ------------------------------------------------------------------ pipeline


async def process_video_background(
    task_id: str,
    url: str,
    api_key: str,
    save_path: str,
    max_chars: int = 80000,
    prompt_template: str = "",
    note_style: str = DEFAULT_NOTE_STYLE,
):
    """Parse -> download -> transcript -> summarise -> screenshots -> preview."""
    cfg = load_config()
    handler = BilibiliHandler(build_credential())
    task = tasks[task_id]
    started = time.time()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = task["queue"]

    def emit(event_type: str, **payload):
        """Enqueue an SSE event from *any* thread.

        Speech-to-text reports progress from a worker thread, and
        ``asyncio.Queue.put_nowait`` is not thread-safe: without the hop back to
        the loop the reader never gets woken and the UI silently freezes.
        """
        payload["type"] = event_type
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            queue.put_nowait(payload)
        else:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

    def emit_progress(step: str, message: str, progress: int):
        emit("progress", step=step, message=message, progress=progress)

    async def emit_async(event_type: str, **payload):
        payload["type"] = event_type
        await queue.put(payload)

    media_files: list[str] = []

    try:
        # 1 ── parse ---------------------------------------------------------
        emit_progress("parsing", "正在解析视频信息…", 4)
        info = await handler.parse_video(url)
        title = info["title"]
        bvid = info["bvid"] or info.get("epid") or "video"
        clean_url = info["url"]
        task["video"] = info
        emit("video", video=info)
        emit_progress("parsing", f"已解析：{title}", 8)

        # 2 ── download ------------------------------------------------------
        emit_progress("downloading", f"正在下载：{title}", 12)

        # The callback is invoked from the event-loop thread (the yt-dlp stderr
        # reader is an async iterator), so it can emit directly — routing it
        # through call_soon_threadsafe only reordered events.
        def on_download(evt: dict):
            pct = evt.get("percent")
            if pct is None:
                return
            overall = 12 + int(min(max(pct, 0.0), 100.0) * 0.28)  # 12 -> 40
            emit_progress("downloading", evt.get("message") or "下载中…", overall)

        video_path, audio_path = await handler.download_video(info, CACHE_DIR, progress_cb=on_download)
        media_files.extend([video_path, audio_path])

        # 3 ── subtitles, else speech-to-text --------------------------------
        emit_progress("extracting", "正在检查 B 站字幕…", 42)
        subtitle = None
        try:
            subtitle = await asyncio.wait_for(
                handler.get_subtitles(
                    info,
                    cid=info.get("cid"),
                    known_subtitles=info.get("subtitle_list") or [],
                ),
                timeout=45,
            )
        except asyncio.TimeoutError:
            emit_progress("extracting", "查询字幕超时，改用语音识别", 43)
        except BilibiliError as exc:
            emit_progress("extracting", f"字幕不可用（{exc}），改用语音识别", 43)
        except Exception as exc:  # noqa: BLE001
            print(f"[subtitles] failed: {exc}")

        transcript: Optional[str] = None
        transcript_source = ""
        stt_model = cfg.get("whisper_model") or "base"

        if subtitle and subtitle.get("text"):
            transcript = subtitle["text"]
            transcript_source = f"B站字幕（{subtitle.get('source', '')}）"
            emit_progress("extracting", "字幕提取成功", 68)
            emit("transcript", source=transcript_source, chars=len(transcript), segments=subtitle.get("segments", []))
        else:
            hint = "" if cfg.get("sessdata") else "（未配置 B站 Cookie，无法使用官方字幕）"
            emit_progress("transcribing", f"没有可用字幕{hint}，开始语音转文字…", 44)

            def on_stt(value):
                if isinstance(value, (int, float)):
                    pct = min(max(float(value), 0.0), 100.0)
                    overall = 44 + int(pct * 0.24)  # 44 -> 68
                    emit_progress("transcribing", f"语音转写中… {pct:.0f}%", overall)
                else:
                    emit_progress("transcribing", str(value), 44)

            try:
                transcript = await transcribe_audio(
                    audio_path, on_stt, model_size=stt_model
                )
            except TranscriptionError as exc:
                raise BilibiliError(str(exc)) from exc
            transcript_source = f"语音识别（faster-whisper {stt_model}）"
            emit("transcript", source=transcript_source, chars=len(transcript or ""), segments=[])

        if not transcript:
            raise BilibiliError(
                "这段视频没有识别出任何文字：可能是纯音乐/无人声的视频，"
                "或者视频没有字幕。可以换用更大的识别模型（设置 → 语音识别模型）后重试。"
            )
        emit_progress("transcribing", "文字提取完成", 70)

        # 4 ── summarise -----------------------------------------------------
        style = style_config(note_style)
        emit_progress("summarizing", f"AI 正在生成{style['label']}…", 72)

        def on_summarize(stage: str, done: int, total: int):
            if stage == "chunk":
                emit_progress(
                    "summarizing",
                    f"正在分析第 {done}/{total} 段（{style['label']}会完整覆盖全片）…",
                    72 + int(done / max(total, 1) * 7),
                )
            elif stage == "merge":
                emit_progress("summarizing", "正在汇总概括与大纲…", 79)

        result = await summarize_text(
            transcript,
            title,
            clean_url,
            api_key,
            max_chars,
            prompt_template,
            meta={
                "owner": (info.get("owner") or {}).get("name", ""),
                "duration": format_duration_short(info.get("duration")),
                "tname": info.get("tname", ""),
                "desc": info.get("desc", ""),
            },
            note_style=note_style,
            on_progress=on_summarize,
        )
        result["title"] = title
        result["url"] = clean_url
        result["video"] = info
        result["transcript_source"] = transcript_source
        result["transcript_chars"] = len(transcript)
        emit_progress("summarizing", "AI 分析完成", 80)

        # 4b ── 精校文稿：分块处理，长视频也不会被输出上限截断 ---------------
        # 简略类笔记不需要整篇文稿；用户也可以在设置里整体关掉
        if cfg.get("polish_transcript", True) and style_generates_transcript(note_style):
            emit_progress("polishing", "正在精校文稿…", 81)
            try:
                polished = await polish_transcript(
                    transcript,
                    api_key,
                    on_progress=lambda done, total: emit_progress(
                        "polishing",
                        f"正在精校文稿… {done}/{total} 段",
                        81 + int(done / max(total, 1) * 6),
                    ),
                )
                if polished:
                    result["polished_transcript"] = polished
                    emit_progress("polishing", "文稿精校完成", 87)
            except Exception as exc:  # noqa: BLE001 - 精校失败不该毁掉整条笔记
                print(f"[polish] failed: {exc}")
                emit_progress("polishing", f"文稿精校失败（{exc}），已跳过", 87)
        else:
            emit_progress("polishing", "已跳过文稿精校", 87)

        # 5 ── screenshots ---------------------------------------------------
        if cfg.get("screenshot_enabled", True):
            emit_progress("screenshots", "正在截取关键画面…", 88)
            details = await take_screenshots(video_path, result.get("details", []), bvid, CACHE_DIR)
            result["details"] = details
            shot_count = sum(1 for d in details if d.get("screenshot_path"))
            emit_progress("screenshots", f"截图完成（{shot_count} 张）", 94)
        else:
            emit_progress("screenshots", "已跳过截图", 94)

        result["_transcript"] = (transcript or "")[:20000]
        result["_stats"] = {
            "details": len(result.get("details", [])),
            "screenshots": sum(1 for d in result.get("details", []) if d.get("screenshot_path")),
            "terms": len(result.get("terms", [])),
            "summary_chars": len(result.get("summary", "") or ""),
            "polished_chars": len(result.get("polished_transcript", "") or ""),
            "transcript_chars": len(transcript or ""),
            "elapsed": round(time.time() - started, 1),
            "transcript_source": transcript_source,
        }

        # 6 ── cleanup + preview --------------------------------------------
        if not cfg.get("keep_media"):
            for f in set(media_files):
                try:
                    if f and os.path.exists(f) and os.path.abspath(f).startswith(os.path.abspath(CACHE_DIR)):
                        os.remove(f)
                except OSError:
                    pass

        task["result"] = result
        task["save_path"] = save_path

        # 默认生成完就直接写盘：用户的心智是「笔记生成了 = Finder 里应该有」，
        # 原来要点一次保存才落盘，结果就是找不到文件。
        if cfg.get("auto_save", True):
            try:
                generator = NoteGenerator(save_path)
                saved = generator.save_note(result, bvid)
                task["saved_path"] = saved
                emit("saved", saved_path=saved, save_path=save_path, auto=True)
            except OSError as exc:
                print(f"[auto-save] failed: {exc}")
                emit_progress("screenshots", f"自动保存失败（{exc}），请手动保存", 99)

        # Emit the final progress *before* the preview, otherwise the trailing
        # progress event would flip the UI back out of its "preview" state.
        emit_progress("screenshots", "处理完成，请预览后保存", 100)
        emit("preview", result=result)

    except (BilibiliError, TranscriptionError) as exc:
        emit("error", message=str(exc))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        emit("error", message=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await handler.close()
        except BaseException:  # noqa: BLE001 - a cancelled task must still clean up quietly
            pass
        emit("closed")


async def download_only_background(
    task_id: str, url: str, dest_dir: str, quality: str = ""
) -> None:
    """纯下载任务：只把视频文件存下来，不解析、不转写、不生成笔记。"""
    task = tasks.get(task_id) or {}
    queue: asyncio.Queue = task["queue"]

    def emit(event_type: str, **payload):
        payload["type"] = event_type
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:  # pragma: no cover - 队列无上限，兜底
            pass

    try:
        emit("progress", step="downloading", message="正在获取视频信息…", progress=0)

        def on_progress(percent: float, message: str) -> None:
            emit("progress", step="downloading", message=message, progress=int(percent))

        result = await download_video(url, dest_dir, on_progress=on_progress, quality=quality)
        task["download"] = result
        emit(
            "done",
            path=result["path"],
            download_path=result["dir"],
            bytes=result["bytes"],
            title=result["title"],
        )
    except DownloadError as exc:
        emit("error", message=str(exc))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        emit("error", message=f"{type(exc).__name__}: {exc}")
    finally:
        emit("closed")


# -------------------------------------------------------------------- routes


@app.post("/api/process")
async def start_process(req: ProcessRequest):
    cfg = load_config()
    api_key = (req.deepseek_api_key or cfg.get("deepseek_api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="请先在设置中配置 DeepSeek API Key")

    note_style = req.note_style or cfg.get("note_style") or DEFAULT_NOTE_STYLE
    if note_style not in {s["id"] for s in list_styles()}:
        note_style = DEFAULT_NOTE_STYLE
    if note_style != cfg.get("note_style"):
        save_config({**cfg, "note_style": note_style})

    task_id = str(uuid.uuid4())[:8]
    save_path = resolve_save_path(req.save_path or cfg.get("save_path"))
    tasks[task_id] = {"queue": asyncio.Queue(), "created": time.time(), "url": req.url}

    handle = asyncio.create_task(
        process_video_background(
            task_id,
            req.url,
            api_key,
            save_path,
            max_chars=req.max_chars or cfg.get("max_chars") or 80000,
            prompt_template=req.prompt_template if req.prompt_template is not None else cfg.get("prompt_template", ""),
            note_style=(req.note_style or cfg.get("note_style") or DEFAULT_NOTE_STYLE),
        )
    )
    tasks[task_id]["handle"] = handle
    return {"task_id": task_id, "save_path": save_path}


@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    handle: Optional[asyncio.Task] = task.get("handle")
    if handle and not handle.done():
        handle.cancel()
    return {"ok": True}


@app.post("/api/inspect")
async def inspect_video(req: InspectRequest):
    """Metadata only — lets the UI show a rich card before processing."""
    handler = BilibiliHandler(build_credential())
    try:
        info = await handler.parse_video(req.url)
        return {"ok": True, "video": info, "tooling": media.tooling_report()}
    except BilibiliError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"解析失败：{exc}") from exc
    finally:
        await handler.close()


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    """纯下载：把视频存到下载目录，不解析也不做笔记。

    进度复用 ``/api/progress/{task_id}`` 那条 SSE —— 前端已经在用同一套机制。
    """
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请先粘贴视频链接")
    if not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail="只支持 http(s) 开头的视频链接")

    cfg = load_config()
    dest = resolve_download_path(req.download_path or cfg.get("download_path"))
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"下载目录不可用：{dest}（{exc}）") from exc

    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {
        "queue": asyncio.Queue(),
        "created": time.time(),
        "url": url,
        "kind": "download",
    }
    handle = asyncio.create_task(
        download_only_background(task_id, url, dest, (req.quality or "").strip())
    )
    tasks[task_id]["handle"] = handle
    return {"task_id": task_id, "download_path": dest}


PICK_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "pick_folder.ps1"


@app.post("/api/pick-directory")
async def pick_directory(req: PickDirRequest):
    """弹 Windows 原生的文件夹选择框，返回所选目录（取消则空字符串）。

    对话框是阻塞的模态窗口，所以丢到子进程里跑，不占事件循环。
    """
    if not media.IS_WINDOWS:
        raise HTTPException(status_code=501, detail="原生选择框目前只在 Windows 上提供")
    if not PICK_SCRIPT.is_file():
        raise HTTPException(status_code=500, detail=f"缺少选择框脚本：{PICK_SCRIPT}")

    def enc(text: Optional[str]) -> str:
        # 中文走 base64：PowerShell 的命令行参数会被控制台代码页打乱
        return base64.b64encode((text or "").encode("utf-8")).decode("ascii")

    args = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PICK_SCRIPT),
        "-TitleB64",
        enc(req.title),
        "-OkLabelB64",
        enc("选择此文件夹"),
        "-InitialB64",
        enc(req.initial),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="选择框等待超时") from None

    def decode(raw: bytes) -> str:
        text = raw.decode("utf-8", "replace")
        if "\ufffd" in text:  # 输出没走 UTF-8（老式控制台），退回本机代码页
            try:
                text = raw.decode("mbcs")
            except (UnicodeDecodeError, LookupError):
                pass
        return text

    if out:
        lines = [ln.strip() for ln in decode(out).splitlines() if ln.strip()]
        picked = lines[-1] if lines else ""
    else:
        picked = ""
        detail = decode(err or b"").strip()
        if proc.returncode not in (0, None) and detail:
            raise HTTPException(status_code=500, detail=f"选择框没能启动：{detail[:300]}")

    if picked and not os.path.isdir(picked):
        picked = ""
    return {"path": picked, "cancelled": not picked}


@app.get("/api/task/{task_id}")
async def get_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "task_id": task_id,
        "result": task.get("result"),
        "video": task.get("video"),
        "save_path": task.get("save_path"),
    }


@app.post("/api/save/{task_id}")
async def save_note(task_id: str, req: SaveRequest):
    task = tasks.get(task_id)
    if not task or "result" not in task:
        raise HTTPException(status_code=400, detail="没有可保存的内容，请先处理视频")

    result = dict(task["result"])
    if req.note_title:
        result["note_title"] = req.note_title
    save_path = resolve_save_path(req.save_path or task.get("save_path"))
    # 注意：这里**不能**把 save_path 写回全局配置。
    # 保存接口只是「把这次的结果写到这个目录」，改目录的唯一入口是设置面板
    # （PUT /api/config）。之前这里顺手写配置，结果任何一次带临时目录的调用
    # 都会把用户的笔记目录永久改掉。

    previous = task.get("saved_path")
    try:
        generator = NoteGenerator(save_path)
        video = result.get("video") or {}
        bvid = video.get("bvid") or "BV"
        saved_path = generator.save_note(result, bvid)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"写入失败：{exc}") from exc

    # 改了文件名就当作重命名：删掉上一份，避免同一个笔记留两个文件
    if previous and os.path.realpath(previous) != os.path.realpath(saved_path):
        try:
            os.remove(previous)
        except OSError:
            pass

    task["saved_path"] = saved_path
    return {"saved_path": saved_path, "save_path": save_path}


@app.get("/api/progress/{task_id}")
async def progress_stream(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    queue: asyncio.Queue = task["queue"]

    async def event_generator():
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=1800)
            except asyncio.TimeoutError:
                yield {"data": json.dumps({"type": "error", "message": "处理超时"})}
                break
            yield {"data": json.dumps(data, ensure_ascii=False)}
            if data.get("type") == "closed":
                break
        # Keep the task around: the user may still save the preview.

    return EventSourceResponse(event_generator())


@app.get("/api/note-styles")
async def note_styles():
    """可选的笔记类型（前端下拉框的数据源）。"""
    return {"styles": list_styles(), "default": DEFAULT_NOTE_STYLE}


@app.get("/api/health")
async def health():
    return {"status": "ok", "tooling": media.tooling_report()}


@app.get("/api/config")
async def get_config():
    return public_config()


@app.put("/api/config")
async def update_config(req: ConfigRequest):
    current = load_config()
    for key in DEFAULT_CONFIG:
        value = getattr(req, key, None)
        if value is not None:
            current[key] = value
    if current.get("whisper_model") not in WHISPER_MODELS:
        current["whisper_model"] = "base"
    save_config(current)
    return public_config()


@app.get("/api/history")
async def history(limit: int = Query(60, ge=1, le=500)):
    """Recently saved notes, newest first."""
    cfg = load_config()
    root = resolve_save_path(cfg.get("save_path"))
    if not os.path.isdir(root):
        return {"save_path": root, "items": []}

    entries = []
    for name in os.listdir(root):
        if not name.endswith(".md"):
            continue
        full = os.path.join(root, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        entries.append({"name": name, "path": full, "mtime": stat.st_mtime, "size": stat.st_size})
    entries.sort(key=lambda e: e["mtime"], reverse=True)

    items = []
    for entry in entries[:limit]:
        try:
            with open(entry["path"], "r", encoding="utf-8") as f:
                head = f.read(4000)
        except OSError:
            continue
        fm = _read_frontmatter(head)
        body = head.split("---", 2)[-1] if head.startswith("---") else head
        summary = ""
        m = re.search(r"##\s*📌?\s*笔记概括\s*\n+(.+?)(?:\n##|\Z)", body, re.S)
        if m:
            summary = re.sub(r"\s+", " ", m.group(1)).strip()[:160]
        tags = []
        if fm.get("tags"):
            tags = [t.strip() for t in fm["tags"].strip("[]").split(",") if t.strip()]
        items.append(
            {
                "title": fm.get("title") or entry["name"][:-3],
                "path": entry["path"],
                "name": entry["name"],
                "date": fm.get("date") or time.strftime("%Y-%m-%d", time.localtime(entry["mtime"])),
                "mtime": entry["mtime"],
                "source": fm.get("source", ""),
                "uploader": fm.get("uploader", ""),
                "duration": fm.get("duration", ""),
                "note_type": fm.get("note_type", ""),
                "tags": tags,
                "summary": summary,
            }
        )
    return {"save_path": root, "items": items}


@app.get("/api/note")
async def read_note(path: str):
    cfg = load_config()
    root = os.path.realpath(resolve_save_path(cfg.get("save_path")))
    target = os.path.realpath(os.path.expanduser(path))
    if not target.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail="只能读取笔记目录内的文件")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    with open(target, "r", encoding="utf-8") as f:
        return {"path": target, "content": f.read()}


@app.post("/api/reveal")
async def reveal(req: PathRequest):
    """在系统文件管理器里定位文件/文件夹（macOS Finder / Windows 资源管理器）。"""
    if not media.reveal_path(req.path):
        raise HTTPException(status_code=404, detail="路径不存在或无法打开")
    return {"ok": True}


@app.post("/api/open-url")
async def open_url(req: UrlRequest):
    if not re.match(r"^https?://", req.url or ""):
        raise HTTPException(status_code=400, detail="只允许打开 http(s) 链接")
    if not media.open_url(req.url):
        raise HTTPException(status_code=500, detail="无法打开链接")
    return {"ok": True}


@app.get("/api/note-asset")
async def note_asset(note: str, src: str):
    """Serve an image referenced from a saved note (``assets/foo.webp``)."""
    cfg = load_config()
    root = os.path.realpath(resolve_save_path(cfg.get("save_path")))
    note_path = os.path.realpath(os.path.expanduser(note))
    if not note_path.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail="越权访问")

    base = os.path.dirname(note_path)
    clean = src.lstrip("/").replace("\\", "/")
    if clean.startswith("assets/"):
        clean = clean[len("assets/") :]
    target = os.path.realpath(os.path.join(base, "assets", clean))
    if not target.startswith(os.path.realpath(os.path.join(base, "assets")) + os.sep):
        raise HTTPException(status_code=403, detail="越权访问")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(target)


def sniff_image_media_type(path: str) -> str:
    """缓存文件统一叫 ``.img``，扩展名推不出类型，就按文件头嗅探。

    不这么做的话，缓存命中时 FileResponse 会把 .img 兜底成
    ``text/plain``（首次冷缓存反而是对的），封面图类型前后不一致。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return "application/octet-stream"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis", b"heic", b"heif", b"mif1"):
        return "image/avif"
    return "image/jpeg"  # B 站封面基本都是 jpg，兜底


@app.get("/api/cover")
async def cover(url: str):
    """Proxy B站 cover images (and cache them) so hotlink rules never bite."""
    if not re.match(r"^https?://", url or ""):
        raise HTTPException(status_code=400, detail="bad url")
    if not re.search(r"\.(hdslb|bilibili)\.com$", httpx.URL(url).host or ""):
        raise HTTPException(status_code=400, detail="only bilibili images are proxied")

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    cached = os.path.join(COVERS_DIR, digest + ".img")
    if os.path.exists(cached):
        return FileResponse(cached, media_type=sniff_image_media_type(cached))

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://www.bilibili.com/",
                },
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"封面获取失败：{exc}") from exc

    with open(cached, "wb") as f:
        f.write(resp.content)
    return FileResponse(cached, media_type=resp.headers.get("content-type", "image/jpeg"))


@app.get("/api/tooling")
async def tooling():
    return media.tooling_report()


# ---------------------------------------------------- built frontend (Tauri)

frontend_dist = pathlib.Path(__file__).parent.parent / "dist"
if frontend_dist.exists():

    @app.get("/")
    @app.get("/index.html")
    async def index():
        html = (frontend_dist / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    assets = frontend_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="frontend_assets")

    # 图标（favicon / 应用 logo）。文件名走白名单，避免这个路由退化成任意文件读取。
    ICON_NAMES = {
        "favicon.ico",
        "logo-32.png",
        "logo-128.png",
        "logo-180.png",
        "logo-192.png",
        "logo-512.png",
    }

    def _icon_response(name: str):
        if name not in ICON_NAMES:
            return JSONResponse({"ok": False}, status_code=404)
        path = frontend_dist / name
        if not path.exists():
            return JSONResponse({"ok": False}, status_code=404)
        media = "image/x-icon" if path.suffix == ".ico" else "image/png"
        return FileResponse(path, media_type=media)

    @app.get("/favicon.ico")
    async def favicon():
        return _icon_response("favicon.ico")

    @app.get("/logo-{size}.png")
    async def logo(size: str):
        return _icon_response(f"logo-{size}.png")

    @app.get("/vite.svg")
    async def legacy_favicon():
        # 旧的占位图标路由，保留以便兼容；界面已改用上面的 PNG / ICO
        icon = frontend_dist / "vite.svg"
        if icon.exists():
            return FileResponse(icon)
        return JSONResponse({"ok": False}, status_code=404)
