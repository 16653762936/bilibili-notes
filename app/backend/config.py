"""
Config file management for bilibili-notes.
Settings are persisted to ~/.bilibili-notes/config.json
"""
import json
import os
import re

CONFIG_DIR = os.path.expanduser("~/.bilibili-notes")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    "deepseek_api_key": "",
    "save_path": "",
    "download_path": "",        # 「下载视频」存到哪；留空 = 系统的「下载」文件夹
    "knowledge_background": "",  # 读者自述的知识背景，用来决定讲解要铺垫多少
    "max_chars": 80000,
    "prompt_template": "",
    # Added in the redesign:
    "sessdata": "",           # optional B站 cookie: unlocks official subtitles
    "bili_jct": "",           # optional B站 cookie, paired with sessdata
    "whisper_model": "base",  # tiny | base | small | medium
    "keep_media": False,      # keep the downloaded mp4 after processing
    "screenshot_enabled": True,
    "polish_transcript": True,  # 分块精校整篇文稿（简略类笔记本来就不生成）
    "note_style": "full",       # brief | full | brief_knowledge | detailed_knowledge
    "theme": "ink",             # 界面配色
    "auto_save": True,          # 生成后立刻写入磁盘，不用再点保存
}

WHISPER_MODELS = ("tiny", "base", "small", "medium")


def load_config() -> dict:
    """Load config from file, returns defaults for anything missing."""
    merged = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
        except (json.JSONDecodeError, OSError):
            pass
    if merged.get("whisper_model") not in WHISPER_MODELS:
        merged["whisper_model"] = DEFAULT_CONFIG["whisper_model"]
    return merged


def save_config(config: dict) -> dict:
    """Save config to file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    merged = DEFAULT_CONFIG.copy()
    merged.update({k: v for k, v in (config or {}).items() if k in DEFAULT_CONFIG})
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def public_config() -> dict:
    """Config that is safe to hand to the frontend (secrets masked)."""
    cfg = load_config()
    key = cfg["deepseek_api_key"]
    return {
        "save_path": cfg["save_path"],
        "download_path": cfg["download_path"],
        "knowledge_background": cfg["knowledge_background"],
        "max_chars": cfg["max_chars"],
        "prompt_template": cfg["prompt_template"],
        "whisper_model": cfg["whisper_model"],
        "keep_media": cfg["keep_media"],
        "screenshot_enabled": cfg["screenshot_enabled"],
        "polish_transcript": cfg["polish_transcript"],
        "note_style": cfg["note_style"],
        "theme": cfg["theme"],
        "auto_save": cfg["auto_save"],
        "has_api_key": bool(key),
        "api_key_hint": (key[:6] + "…" + key[-4:]) if len(key) > 12 else "",
        "has_credential": bool(cfg["sessdata"]),
    }


def ytdlp_cookie_args() -> list[str]:
    """把设置里的 B 站 Cookie 转成 yt-dlp 参数。

    之前 yt-dlp 这一路完全没带 Cookie：需要登录才有的清晰度、番剧、
    部分影视会直接失败或只能拿到最低清，而「生成笔记」那边用的是
    bilibili_api + Credential，两边行为不一致。
    """
    cfg = load_config()
    sess = (cfg.get("sessdata") or "").strip()
    if not sess:
        return []
    jct = (cfg.get("bili_jct") or "").strip()
    cookie = f"SESSDATA={sess}"
    if jct:
        cookie += f"; bili_jct={jct}"
    return ["--add-header", f"Cookie: {cookie}"]


def ytdlp_page_args(url: str) -> list[str]:
    """分 P 视频（?p=3）要指定具体那一 P。

    yt-dlp 对多 P 视频返回的是播放列表，原来固定带 --no-playlist，
    结果不管点第几 P，下到的永远是第 1 P。
    """
    m = re.search(r"[?&]p=(\d+)", url or "")
    if not m:
        return ["--no-playlist"]
    page = m.group(1)
    return ["--playlist-items", page]
