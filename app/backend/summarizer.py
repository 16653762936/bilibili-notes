"""DeepSeek 调用：笔记类型（note_style）与两段式生成。

## 为什么是现在这个结构

踩过两个真实的坑：

1. **JSON 被截断**。旧实现把 `polished_transcript`（整篇精校文稿）和结构化笔记塞进
   *同一个* JSON。长视频转录三四万字，模型单次输出上限只有几千 token，JSON 必然在
   字符串中间被切断 → `json.loads` 失败 → 「DeepSeek 返回格式异常，无法解析 JSON」。
   现在精校文稿单独走 `polish_transcript`，按 2600 字分块，块块都不会超限。

2. **「不落下所有内容」做不到**。要求模型一次输出覆盖 71 分钟视频的全部知识点，
   同样会顶到输出上限而被截断。所以 `完整笔记` / `详细知识笔记` 会走
   **map-reduce**：把转录按时间切成若干段，每段单独抽取知识点，最后再把
   「已经抽好的知识点」汇总成概括和大纲。每次请求的输出都是有界的。

## 四种笔记类型

两个维度：**覆盖面**（简略 / 完整）× **侧重**（内容 / 知识）。
"""

import asyncio
import json
import re
from typing import Callable, Optional

NOTE_MAX_TOKENS = 8000
POLISH_MAX_TOKENS = 4000
POLISH_CHUNK_CHARS = 2600
# 超过这个长度就走分段抽取，避免单次输出被截断
DETAILS_CHUNK_CHARS = 11000


# --------------------------------------------------------------------- 工具


def _strip_code_fence(content: str) -> str:
    """模型偶尔仍会用 ```json 包裹，剥掉它。"""
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def _salvage_json(raw: str) -> Optional[dict]:
    """尽最大努力从（可能被截断的）模型输出里抢救出一个 dict。

    做法：从头扫描，记住括号栈与字符串状态；发现完整顶层对象就直接解析，否则
    补上未闭合的引号与括号，再去掉结尾那个残缺的键。
    """
    text = _strip_code_fence(raw)
    start = text.find("{")
    if start == -1:
        return None
    text = text[start:]

    stack: list[str] = []
    in_string = False
    escaped = False
    complete_end: Optional[int] = None

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if stack:
                stack.pop()
            if not stack:
                complete_end = index + 1
                break

    if complete_end is not None:
        try:
            return json.loads(text[:complete_end])
        except json.JSONDecodeError:
            pass

    buffer = text + ('"' if in_string else "")
    buffer = re.sub(r',\s*"[^"]*"\s*:?\s*$', "", buffer)
    buffer = re.sub(r",\s*$", "", buffer)
    buffer += "".join(reversed(stack))
    try:
        return json.loads(buffer)
    except json.JSONDecodeError:
        return None


def _normalise_time(value) -> str:
    """把模型给的各种时间写法统一成 HH:MM:SS。"""
    if value is None:
        return ""
    text = str(value).strip().replace("：", ":")
    if not text:
        return ""
    parts = [p for p in text.split(":") if p != ""]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return ""
    seconds = 0.0
    for num in nums:
        seconds = seconds * 60 + num
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _client(api_key: str):
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com")


# --------------------------------------------------------------- 笔记类型表

NOTE_STYLES: dict[str, dict] = {
    "brief": {
        "label": "简略笔记",
        "hint": "快速概括视频讲了什么，用来判断值不值得看",
        "polish": False,
        "chunked": False,
        "screenshot_hint": "一般不需要配图，只有关键图表/演示才把 need_screenshot 设为 true",
        "instructions": """## 本次任务：简略笔记

目标：让读者 **30 秒**判断这个视频讲了什么、值不值得看。**不要**逐段复述。

- summary：3-5 句话，说清主题、最关键的几个点、结论或价值
- terms：只解释「不解释就看不懂 summary」的核心名词，0-5 条即可
- outline：3-5 个分类，每个分类 2-4 条要点，只写结论不写推导
- details：3-6 条，每条 40-120 字，只保留最重要的结论性内容
- 大胆省略铺垫、举例、推导过程、重复内容
- 不要为了凑数把同一件事拆成多条""",
    },
    "full": {
        "label": "完整笔记",
        "hint": "按时间顺序记下视频的全部内容，不落下细节",
        "polish": True,
        "chunked": True,
        "screenshot_hint": "涉及图表、界面、代码、演示画面时把 need_screenshot 设为 true",
        "instructions": """## 本次任务：完整笔记

目标：**不落下视频里的任何内容**，按讲述顺序完整记录。

- summary：5-8 句话，覆盖视频的主要脉络
- terms：视频里出现的专业名词、缩写、产品名都解释一遍
- outline：按视频结构做完整分类大纲
- details：**覆盖从头到尾的全部内容**，按时间顺序排列，每条 100-300 字
- 例子、数据、步骤、结论、甚至作者的观点和吐槽都要写进去
- details 按视频讲述顺序排列，宁多勿少；内容多就增加条数，不要压缩每条的篇幅
- 原话里有信息量的地方尽量保留原意，不要概括成「讲了某某方法」""",
    },
    "brief_knowledge": {
        "label": "简略知识笔记",
        "hint": "篇幅短，但公式/技巧/方法论一个字都不省",
        "polish": False,
        "chunked": False,
        "screenshot_hint": "公式推导、图表、代码界面把 need_screenshot 设为 true",
        "instructions": """## 本次任务：简略知识笔记

目标：**篇幅可以短，但知识性内容一个字都不能省。**

可以省略的：寒暄、铺垫、闲聊、重复、单纯为了活跃气氛的举例。
**绝对不能省略的**：
- 公式、定理、定义、参数取值、单位
- 具体的技巧、诀窍、经验法则、方法论
- 操作步骤、命令、代码片段、配置项
- 数据、指标、对比结论、适用条件与边界

要求：
- summary：3-5 句话
- terms：所有专业名词都要解释
- outline：按**知识主题**分类（而不是按视频时间线）
- details：条数可以少，但每条必须是**知识本身**，公式/步骤原文完整保留，
  绝不写成「讲解了 XX 方法」这种空壳概括。条数 3-10 条，短小但信息密度高
- 如果一句话里既有闲聊又有知识，只保留知识部分""",
    },
    "detailed_knowledge": {
        "label": "详细知识笔记",
        "hint": "完整记录全部内容，并逐个讲透里面的知识",
        "polish": True,
        "chunked": True,
        "screenshot_hint": "公式、图表、界面、代码都要配图，need_screenshot 设为 true",
        "instructions": """## 本次任务：详细知识笔记

目标：**完整记录视频内容，同时把里面的知识讲透。**

- summary：5-8 句话，完整交代视频脉络
- terms：所有专业名词、背景概念都解释清楚
- outline：构成一个完整的知识体系大纲
- details：既覆盖全部内容，又对每个知识点做**记录 + 解释**：
  - 是什么：定义、公式、参数
  - 为什么：原理、动机、推导思路
  - 怎么用：操作步骤、代码、配置
  - 什么时候不适用：边界条件、常见误区、踩坑点
- 每条 150-400 字，按视频顺序排列
- 视频里没展开但理解所必需的内容，可以补充解释，但**必须明确标注为「补充说明」**，
  严禁把推测写成视频的原话
- 数据、公式、版本号、命令必须原文保留""",
    },
}

DEFAULT_NOTE_STYLE = "full"

# 单次请求（简略类）：一次性产出完整 JSON
NOTE_SCHEMA = """{
  "note_title": "简洁文件名，如：UE5虚拟纹理优化方案",
  "summary": "笔记内容概括",
  "terms": [{"term": "专业名词", "explain": "用一两句大白话解释"}],
  "outline": [{"category": "分类名称", "items": ["要点1", "要点2"]}],
  "details": [
    {"index": 1, "time": "00:01:23", "title": "知识点标题",
     "content": "内容，完整保留技术细节、数据、步骤、原理", "need_screenshot": true}
  ],
  "tags": ["标签1", "标签2"]
}"""

# 分段抽取：每段只抽知识点，输出天然有界
CHUNK_SCHEMA = """{
  "details": [
    {"time": "00:01:23", "title": "知识点标题",
     "content": "内容，完整保留技术细节、数据、步骤、原理", "need_screenshot": true}
  ],
  "terms": [{"term": "专业名词", "explain": "用一两句大白话解释"}]
}"""

# 汇总：拿已经抽好的知识点生成概括与大纲
META_SCHEMA = """{
  "note_title": "简洁文件名",
  "summary": "笔记内容概括",
  "terms": [{"term": "专业名词", "explain": "解释"}],
  "outline": [{"category": "分类名称", "items": ["要点1", "要点2"]}],
  "tags": ["标签1", "标签2"]
}"""


def style_config(note_style: Optional[str]) -> dict:
    return NOTE_STYLES.get(note_style or "", NOTE_STYLES[DEFAULT_NOTE_STYLE])


def style_label(note_style: Optional[str]) -> str:
    return style_config(note_style)["label"]


def style_generates_transcript(note_style: Optional[str]) -> bool:
    return bool(style_config(note_style)["polish"])


def list_styles() -> list[dict]:
    return [
        {"id": key, "label": value["label"], "hint": value["hint"]}
        for key, value in NOTE_STYLES.items()
    ]


# --------------------------------------------------------------- 通用提示词

_BASE_RULES = """
## 通用规则
- 技术名词、版本号、公式、命令、代码片段必须原文保留，不要翻译、不要改写
- 时间戳格式统一为 HH:MM:SS，必须是该知识点在视频中真实出现的位置
- 不要编造视频里没有的信息
- 只输出 JSON，不要任何解释文字"""


def _build_system_prompt(note_style: Optional[str], prompt_template: str, schema: str, extra: str = "") -> str:
    style = style_config(note_style)
    parts = [
        "你是专业的视频笔记整理助手，把视频转录整理成结构化知识笔记。",
        "",
        style["instructions"],
        "",
        "## 输出格式",
        "严格按下面的 JSON 结构输出：",
        schema,
        "",
        f"## 配图要求\n{style['screenshot_hint']}",
        _BASE_RULES,
    ]
    if extra:
        parts += ["", extra]
    if prompt_template and prompt_template.strip():
        parts += [
            "",
            "## 用户的额外要求（优先级高于上面的通用风格描述，但不要改变 JSON 结构）",
            prompt_template.strip(),
        ]
    return "\n".join(parts)


def _split_by_lines(text: str, chunk_chars: int) -> list[str]:
    """按行切块，尽量不切断一句话。"""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in (text or "").split("\n"):
        line_len = len(line) + 1
        if size + line_len > chunk_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _normalise_details(items, start_index: int = 1) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        title = str(item.get("title") or "").strip()
        if not content and not title:
            continue
        out.append(
            {
                "time": _normalise_time(item.get("time")),
                "title": title or "未命名知识点",
                "content": content,
                "need_screenshot": bool(item.get("need_screenshot")),
            }
        )
    for offset, item in enumerate(out):
        item["index"] = start_index + offset
    return out


def _normalise_terms(items) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        explain = str(item.get("explain") or "").strip()
        if not term or term in seen:
            continue
        seen.add(term)
        out.append({"term": term, "explain": explain})
    return out


# ------------------------------------------------------------------ 主入口


async def summarize_text(
    text: str,
    title: str,
    url: str,
    api_key: str,
    max_chars: int = 80000,
    prompt_template: str = "",
    meta: Optional[dict] = None,
    note_style: str = DEFAULT_NOTE_STYLE,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> dict:
    """按笔记类型生成结构化笔记（不含精校文稿）。"""
    if not api_key:
        raise ValueError("DeepSeek API Key 未配置")

    client = _client(api_key)
    style = style_config(note_style)

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n\n... [内容已截断，笔记可能不完整]"

    meta = meta or {}
    meta_lines = []
    if meta.get("owner"):
        meta_lines.append(f"UP主：{meta['owner']}")
    if meta.get("duration"):
        meta_lines.append(f"时长：{meta['duration']}")
    if meta.get("tname"):
        meta_lines.append(f"分区：{meta['tname']}")
    if meta.get("desc"):
        meta_lines.append(f"简介：{str(meta['desc'])[:500]}")
    header = f"视频标题：{title}\n视频链接：{url}\n"
    if meta_lines:
        header += "\n".join(meta_lines) + "\n"

    if style["chunked"] and len(text) > DETAILS_CHUNK_CHARS:
        result = await _summarize_chunked(
            client, text, header, note_style, prompt_template, on_progress
        )
    else:
        result = await _summarize_once(client, text, header, note_style, prompt_template)

    if not result.get("note_title"):
        result["note_title"] = title or "视频笔记"
    result.setdefault("summary", "")
    result["truncated"] = truncated
    result["note_style"] = note_style if note_style in NOTE_STYLES else DEFAULT_NOTE_STYLE
    result["note_style_label"] = style["label"]
    return result


async def _call_json(
    client,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = NOTE_MAX_TOKENS,
    temperature: float = 0.7,
) -> dict:
    try:
        response = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        raise Exception(f"DeepSeek API 调用失败: {exc}") from exc

    choice = response.choices[0] if response.choices else None
    content = choice.message.content if choice else None
    finish_reason = getattr(choice, "finish_reason", None) if choice else None

    if not content:
        raise Exception("DeepSeek 返回为空，请稍后重试")

    parsed = _salvage_json(content)
    if parsed is None:
        hint = "（返回内容被长度限制截断）" if finish_reason == "length" else ""
        raise Exception(f"DeepSeek 返回的 JSON 无法解析{hint}，请重试；反复失败可换一个视频试试")
    return parsed


async def _summarize_once(client, text, header, note_style, prompt_template) -> dict:
    system_prompt = _build_system_prompt(note_style, prompt_template, NOTE_SCHEMA)
    user_prompt = f"{header}\n转录内容：\n{text}"
    parsed = await _call_json(client, system_prompt, user_prompt)

    parsed["terms"] = _normalise_terms(parsed.get("terms"))
    parsed["details"] = _normalise_details(parsed.get("details"))
    parsed["outline"] = [c for c in (parsed.get("outline") or []) if isinstance(c, dict)]
    parsed["tags"] = [str(t) for t in (parsed.get("tags") or []) if str(t).strip()]
    return parsed


async def _summarize_chunked(
    client, text, header, note_style, prompt_template, on_progress
) -> dict:
    """map-reduce：逐段抽取知识点，再汇总成概括与大纲。

    这样「完整笔记 / 详细知识笔记」不会因为一次输出太长而被截断。
    """
    chunks = _split_by_lines(text, DETAILS_CHUNK_CHARS)
    total = len(chunks)

    system_prompt = _build_system_prompt(
        note_style,
        prompt_template,
        CHUNK_SCHEMA,
        extra=(
            "## 本次输入是视频的**一段**（不是全部）\n"
            f"完整视频被切成 {total} 段，你只负责这一段。\n"
            "- 只输出这一段里出现的知识点，不要写概括、不要写大纲\n"
            "- 不要凭空补充其他段落可能有的内容\n"
            "- 顺序必须与原文一致"
        ),
    )

    semaphore = asyncio.Semaphore(2)
    done = {"n": 0}

    async def run_chunk(index: int) -> dict:
        async with semaphore:
            try:
                part = await _call_json(
                    client,
                    system_prompt,
                    f"{header}\n这是第 {index + 1}/{total} 段转录：\n{chunks[index]}",
                    temperature=0.5,
                )
            except Exception as exc:  # noqa: BLE001 - 单段失败不影响其它段
                print(f"[summarize] chunk {index} failed: {exc}")
                part = {}
            finally:
                done["n"] += 1
                if on_progress:
                    try:
                        on_progress("chunk", done["n"], total)
                    except Exception:  # noqa: BLE001
                        pass
            return part

    parts = await asyncio.gather(*(run_chunk(i) for i in range(total)))

    details: list[dict] = []
    terms: list[dict] = []
    for part in parts:
        details.extend(_normalise_details(part.get("details"), start_index=len(details) + 1))
        terms.extend(_normalise_terms(part.get("terms")))

    if not details:
        raise Exception("分段分析没有产出任何知识点，请重试")

    # 用「已经抽好的知识点」做汇总：输入远短于原始转录，输出也不会超限
    digest = "\n\n".join(
        f"[{d['time']}] {d['title']}\n{d['content']}" for d in details
    )
    if on_progress:
        try:
            on_progress("merge", total, total)
        except Exception:  # noqa: BLE001
            pass

    merge_prompt = _build_system_prompt(
        note_style,
        prompt_template,
        META_SCHEMA,
        extra=(
            "## 本次任务：汇总\n"
            "下面是已经按时间顺序抽取好的知识点清单。请据此产出 note_title、summary、"
            "outline、tags，并把名词解释补充完整。\n"
            "**不要再输出 details**，details 已经由前一步生成好了。"
        ),
    )
    merged = await _call_json(
        client,
        merge_prompt,
        f"{header}\n知识点清单：\n{digest}",
        temperature=0.6,
    )

    return {
        "note_title": merged.get("note_title") or "",
        "summary": merged.get("summary") or "",
        "outline": [c for c in (merged.get("outline") or []) if isinstance(c, dict)],
        "tags": [str(t) for t in (merged.get("tags") or []) if str(t).strip()],
        "terms": _normalise_terms(merged.get("terms")) or _normalise_terms(terms),
        "details": details,
    }


# ------------------------------------------------------------ 文稿精校（分块）

POLISH_SYSTEM_PROMPT = (
    "你是视频文稿校对员。用户给你一段语音识别产生的转录（每行以 [时间戳] 开头）。\n"
    "请把它整理成干净、可读的文字：\n"
    "1. 去掉「嗯、啊、呃、那个、就是」等口语填充词与重复口吃\n"
    "2. 根据上下文修正同音错别字、专有名词\n"
    "3. 补标点、按语义分段，统一中英文标点\n"
    "4. **不要删减任何信息**，技术名词、数据、步骤、例子必须全部保留，长度与原文相当\n"
    "5. **保留每行开头的 [时间戳]**，不要合并或改动时间戳\n"
    "6. 只输出整理后的正文，不要任何解释、前言或 Markdown 代码块"
)


async def polish_transcript(
    text: str,
    api_key: str,
    on_progress: Optional[Callable[[int, int], None]] = None,
    concurrency: int = 3,
) -> str:
    """把转录精校成干净文稿；分块请求，块块都不会超限。"""
    if not api_key:
        raise ValueError("DeepSeek API Key 未配置")

    chunks = _split_by_lines(text, POLISH_CHUNK_CHARS)
    if not chunks:
        return ""

    client = _client(api_key)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    counter = {"done": 0}

    async def polish_one(index: int, chunk: str) -> tuple[int, str]:
        async with semaphore:
            polished = chunk
            try:
                response = await client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {"role": "system", "content": POLISH_SYSTEM_PROMPT},
                        {"role": "user", "content": chunk},
                    ],
                    temperature=0.2,
                    max_tokens=POLISH_MAX_TOKENS,
                )
                choice = response.choices[0] if response.choices else None
                candidate = _strip_code_fence((choice.message.content if choice else "") or "").strip()
                # 明显异常（被截断或几乎没返回）就退回原文，宁可不清校也不丢内容
                if candidate and len(candidate) >= len(chunk) * 0.4:
                    polished = candidate
            except Exception as exc:  # noqa: BLE001
                print(f"[polish] chunk {index} failed: {exc}")
            finally:
                counter["done"] += 1
                if on_progress:
                    try:
                        on_progress(counter["done"], len(chunks))
                    except Exception:  # noqa: BLE001
                        pass
            return index, polished

    results = await asyncio.gather(*(polish_one(i, c) for i, c in enumerate(chunks)))
    return "\n\n".join(part for _, part in sorted(results, key=lambda r: r[0]))
