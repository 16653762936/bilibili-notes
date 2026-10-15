import asyncio
import os
from typing import Callable, Optional, Union

from . import media

ProgressCallback = Callable[[Union[float, str]], None]

# Loading a Whisper model is expensive (and the first run may hit the network),
# so keep it around between jobs.
_MODEL_CACHE: dict[str, object] = {}


class TranscriptionError(Exception):
    """Human-readable transcription failure."""


def model_source(model_size: str) -> str:
    """模型从哪加载：便携包里给定一个普通目录，否则用 HuggingFace 上的名字。

    便携包不复制 HuggingFace 缓存目录结构（那里 blobs/ 和 snapshots/ 是软链
    关系，解引用后会占双份空间），而是把 4 个模型文件平铺成一个目录，
    再用 ``MYBOX_WHISPER_MODEL_DIR`` 指过来。
    """
    explicit = os.environ.get("MYBOX_WHISPER_MODEL_DIR")
    if explicit:
        path = os.path.abspath(os.path.expanduser(explicit))
        if os.path.isfile(os.path.join(path, "model.bin")):
            return path
    return model_size


def _load_model(model_size: str):
    """Load (and cache) a faster-whisper model. Must run off the event loop.

    ``WhisperModel(...)`` may reach out to HuggingFace to resolve the snapshot;
    doing that on the loop thread froze the entire server on a socket connect
    (faster-whisper shows nothing in the UI and every HTTP request times out).
    """
    source = model_source(model_size)
    cached = _MODEL_CACHE.get(source)
    if cached is not None:
        return cached

    from faster_whisper import WhisperModel

    if os.path.isdir(source):
        # 本地目录：faster-whisper 直接读，完全不碰网络
        model = WhisperModel(source, device="cpu", compute_type="int8")
    else:
        # 先只认本地缓存（不联网就不会卡）；没缓存过才真的去下
        try:
            model = WhisperModel(source, device="cpu", compute_type="int8", local_files_only=True)
        except Exception:  # noqa: BLE001 - not downloaded yet, fall back to a real fetch
            model = WhisperModel(source, device="cpu", compute_type="int8")

    _MODEL_CACHE[source] = model
    return model


async def transcribe_audio(
    audio_path: str,
    progress_callback: Optional[ProgressCallback] = None,
    model_size: str = "base",
) -> Optional[str]:
    """
    Transcribe an audio/video file using faster-whisper.

    Returns timestamped text, or ``None`` when nothing was recognised.

    The audio-stream check at the top is deliberate: handing a video-only file
    to PyAV used to raise ``IndexError: tuple index out of range`` from deep
    inside ``av.container.streams`` — an error that told the user nothing.
    """
    if not audio_path or not os.path.exists(audio_path):
        raise TranscriptionError(f"媒体文件不存在：{audio_path}")

    if not await media.has_audio_stream_async(audio_path):
        raise TranscriptionError(
            "下载到的文件里没有音轨，无法做语音转文字。"
            "这通常是 ffmpeg 缺失导致视频/音频没有合并，请安装 ffmpeg 后重试。"
        )

    loop = asyncio.get_running_loop()

    def report(value: Union[float, str]):
        """Report progress from either the loop thread or the worker thread."""
        if not progress_callback:
            return
        try:
            loop.call_soon_threadsafe(progress_callback, value)
        except RuntimeError:  # loop already closed
            pass

    def _work() -> str:
        # Model loading happens here, inside the worker thread — never on the loop.
        report("加载语音识别模型…")
        model = _load_model(model_size)

        report("正在转录音频…")
        segments, info = model.transcribe(
            audio_path,
            language="zh",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )

        parts: list[str] = []
        duration = info.duration if info and getattr(info, "duration", 0) else 0
        last_reported = -10.0

        for seg in segments:
            text = (seg.text or "").strip()
            if text:
                parts.append(f"[{format_timestamp(seg.start)}] {text}")
            if duration > 0:
                pct = min(seg.end / duration * 100.0, 99.0)
                if pct - last_reported >= 1.0:
                    last_reported = pct
                    report(pct)  # numeric, so the UI progress bar can move
        return "\n".join(parts)

    try:
        text = await loop.run_in_executor(None, _work)
    except TranscriptionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TranscriptionError(f"语音识别失败：{exc}") from exc

    report(100.0)
    return text or None


def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS (or MM:SS) format."""
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
