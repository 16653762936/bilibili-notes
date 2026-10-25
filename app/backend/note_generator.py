import os
import re
import shutil
from datetime import datetime


class NoteGenerator:
    """Generate Obsidian-compatible Markdown notes."""

    def __init__(self, base_path: str):
        self.base_path = base_path
        os.makedirs(self.base_path, exist_ok=True)

    def _safe_path(self, safe_name: str, source_url: str) -> str:
        """同名文件不要静默覆盖别人的笔记。

        规则：已有的同名文件如果来自**同一个视频**（frontmatter 里 source 相同），
        那是重新生成，直接覆盖；如果 source 不同（只是标题撞车），就退让成
        「标题 (2).md」，绝不悄悄抹掉另一篇笔记。
        """
        filepath = os.path.join(self.base_path, f"{safe_name}.md")
        if not os.path.exists(filepath):
            return filepath

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                head = f.read(2000)
        except OSError:
            head = ""
        existing_source = ""
        for line in head.splitlines()[:20]:
            if line.startswith("source:"):
                existing_source = line.split(":", 1)[1].strip()
                break

        if existing_source and source_url and existing_source == source_url:
            return filepath  # 同一个视频，重新生成，覆盖

        for index in range(2, 100):
            candidate = os.path.join(self.base_path, f"{safe_name} ({index}).md")
            if not os.path.exists(candidate):
                return candidate
        return filepath

    def save_note(self, result: dict, bvid: str) -> str:
        title = result.get("title", "未命名视频")
        url = result.get("url", "")
        note_title = result.get("note_title") or title
        summary = result.get("summary", "")
        outline = result.get("outline", []) or []
        details = result.get("details", []) or []
        tags = result.get("tags", []) or []
        terms = result.get("terms", []) or []
        video = result.get("video") or {}
        today = datetime.now()

        assets_dir = os.path.join(self.base_path, "assets")
        os.makedirs(assets_dir, exist_ok=True)

        # Copy screenshots next to the note so the vault stays self-contained.
        cache_assets = os.path.join(os.path.expanduser("~/.bilibili-notes/cache"), "assets")
        for d in details:
            src = d.get("screenshot_path")
            if not src:
                continue
            src_full = os.path.join(cache_assets, src)
            if os.path.exists(src_full):
                try:
                    shutil.copy2(src_full, os.path.join(assets_dir, src))
                except (OSError, shutil.Error):
                    pass

        md: list[str] = []
        base_url = url.split("?")[0]

        # --- Frontmatter ---
        md.append("---")
        md.append(f'title: "{escape_yaml(note_title)}"')
        md.append(f"source: {base_url}")
        md.append(f"date: {today.strftime('%Y-%m-%d')}")
        platform = video.get("platform") or "Bilibili"
        md.append(f"platform: {platform}")
        if result.get("note_style_label"):
            md.append(f'note_type: "{result["note_style_label"]}"')
        if video.get("bvid"):
            id_key = "douyin_id" if platform == "Douyin" else "bvid"
            md.append(f"{id_key}: {video['bvid']}")
        if video.get("owner", {}).get("name"):
            md.append(f'uploader: "{escape_yaml(video["owner"]["name"])}"')
        if video.get("duration"):
            md.append(f"duration: {format_duration(video['duration'])}")
        if video.get("pubdate"):
            md.append(f"published: {datetime.fromtimestamp(video['pubdate']).strftime('%Y-%m-%d')}")
        if video.get("stat", {}).get("view"):
            md.append(f"views: {video['stat']['view']}")
        if tags:
            md.append("tags: [" + ", ".join(str(t) for t in tags) + "]")
        md.append("---")
        md.append("")

        # --- Header ---
        md.append(f"# {note_title}")
        md.append("")
        md.append(f"**原视频**：[{title}]({base_url})")
        meta_bits = []
        if video.get("owner", {}).get("name"):
            meta_bits.append(f"UP主 {video['owner']['name']}")
        if video.get("duration"):
            meta_bits.append(f"时长 {format_duration(video['duration'])}")
        if video.get("pubdate"):
            meta_bits.append(datetime.fromtimestamp(video["pubdate"]).strftime("%Y-%m-%d"))
        if video.get("stat", {}).get("view"):
            meta_bits.append(f"播放 {format_count(video['stat']['view'])}")
        if meta_bits:
            md.append("")
            md.append(" · ".join(meta_bits))
        md.append("")

        # --- Summary ---
        md.append("## 📌 笔记概括")
        md.append("")
        md.append(summary or "_（无）_")
        md.append("")

        # --- Terms ---
        if terms:
            md.append("## 📖 名词解释")
            md.append("")
            for t in terms:
                md.append(f"- **{t.get('term', '')}**：{t.get('explain', '')}")
            md.append("")

        # --- Outline ---
        if outline:
            md.append("## 🗂 知识大纲")
            md.append("")
            for cat in outline:
                md.append(f"**{cat.get('category', '')}**")
                md.append("")
                for item in cat.get("items", []) or []:
                    md.append(f"- {item}")
                md.append("")

        # --- Details with screenshots + timestamp links ---
        if details:
            md.append("## 🔍 细节知识点")
            md.append("")
            for i, d in enumerate(details, 1):
                time_str = d.get("time") or ""
                heading = d.get("title") or f"知识点 {i}"
                if time_str:
                    link = time_link(base_url, time_str)
                    md.append(f"### {i}. {heading}  <sub>[{time_str}]({link})</sub>")
                else:
                    md.append(f"### {i}. {heading}")
                md.append("")
                if d.get("screenshot_path"):
                    md.append(f"![[assets/{d['screenshot_path']}]]")
                    md.append("")
                md.append(d.get("content", ""))
                md.append("")

        # --- Polished transcript ---
        polished = result.get("polished_transcript", "")
        if polished:
            md.append("## ✍️ 语音文字精校版")
            md.append("")
            md.append("> AI 精校：去除语气词、修正识别错误、整理段落，内容完整保留")
            md.append("")
            for para in polished.split("\n"):
                para = para.strip()
                if para:
                    md.append(para)
                    md.append("")

        if tags:
            md.append("---")
            md.append("")
            md.append(" ".join(f"#{t}" for t in tags))
            md.append("")

        safe = sanitize_filename(note_title)
        filepath = self._safe_path(safe, base_url)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(md))

        print(f"Note saved: {filepath}")
        return filepath


def escape_yaml(text: str) -> str:
    if any(c in text for c in [":", '"', "'", "#", "[", "]"]):
        return text.replace('"', '\\"')
    return text


def time_link(base_url: str, time_str: str) -> str:
    """``https://.../video/BVxxx?t=83`` opens the player at that second.

    这个 ``?t=`` 只有 B 站认。抖音之类的站点没有等价的起播参数，
    就退回原链接，避免生成一个点了没反应的假链接。
    """
    if not base_url or not time_str:
        return base_url
    if "bilibili.com" not in base_url:
        return base_url
    seconds = 0
    for part in str(time_str).split(":"):
        try:
            seconds = seconds * 60 + int(float(part))
        except ValueError:
            return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}t={seconds}"


def format_duration(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_count(value) -> str:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return ""
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


# Windows 不允许用作文件名的保留字
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    # Windows 上结尾的点和空格会被静默丢掉，干脆自己去掉
    name = name.strip(". ")
    if name.upper() in _WINDOWS_RESERVED:
        name = f"{name}_"
    return name[:120] if name else "untitled"
