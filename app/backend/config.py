"""
Config file management for bilibili-notes.
Settings are persisted to ~/.bilibili-notes/config.json
"""
import json
import os

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
