# Bilibili Notes · B 站视频笔记工具（Windows 版）

把 B 站视频变成可检索的 Obsidian 笔记：自动取字幕（或语音转写）→ AI 整理成带时间戳和关键截图的 Markdown。

> ### ⚠️ 从 GitHub 下载 ZIP（`bilibili-notes-main`）不能直接双击 `启动.bat`
>
> 本仓库是**源码 + 已构建前端**，不含便携运行时（Python / ffmpeg / 语音模型）——
> 它们体积超过 GitHub 单文件上限（`model.bin` 138MB、`avcodec-63.dll` 114MB），已被 `.gitignore` 排除。
> 所以直接双击 `启动.bat` 会提示「这个目录里没有便携运行时」，**这不是打包坏了**。
>
> | 你手上有什么 | 怎么做 |
> |---|---|
> | 只有源码（ZIP / clone） | 先装 **Python 3.10+** 和 **ffmpeg**，然后双击 **`从源码启动.bat`**（自动建虚拟环境、装依赖，首次几分钟） |
> | 便携版压缩包 | 解压后双击 **`启动.bat`**，无需安装任何东西 |

---

## 它能做什么

- 粘贴 B 站链接（支持 `BV` 号、`av` 号、`b23.tv` 短链、番剧 `ep`）
- 有字幕的视频直接读官方字幕；没有就用 **faster-whisper** 本地语音转写
- 调用 **DeepSeek** 生成结构化笔记，四种详略可选
- 按知识点时间戳用 **ffmpeg** 抽帧，把截图写进笔记的 `assets/`
- 生成即落盘，直接进你的 Obsidian 仓库；界面里可以回看历史笔记

---

## 界面

| 区域 | 说明 |
|---|---|
| 顶栏 | 设置（配色 / API Key / B 站 Cookie / 模型 / 目录）；窄窗口下出现「笔记列表」「信息」开关 |
| 左栏 | 搜索 + 最近笔记列表，「刷新列表」「打开笔记目录」 |
| 中栏 | 链接输入 → 解析信息 / 选笔记类型 / 生成笔记；下方是最近笔记卡片 |
| 右栏 | 运行环境（后端、DeepSeek、Cookie、ffmpeg、yt-dlp、模型）、任务状态、保存位置 |

快捷键：`Ctrl+K` 聚焦链接 · `Ctrl+S` 保存笔记 · `Ctrl+,` 设置 · `Esc` 关闭弹层

四种笔记类型：**简略笔记** / **完整笔记** / **简略知识笔记** / **详细知识笔记**。

### 讲解风格：写给「还不会」的人

笔记默认按「讲明白」的标准生成，而不是把术语换个说法：

- 每条知识点按「先结论 → 再讲为什么需要它 → 然后才是定义/公式 → 必须有一个具体例子 → 常见误解」组织
- 禁止用术语解释术语：任何术语第一次出现，当场用大白话说明它大概是什么
- 禁止只有形容词没有机制（「性能大幅提升」「非常灵活」这类必须给出具体原因或数字）
- 视频本身没讲透的地方会标「（视频里没讲透）」，不拿空话填满

设置页有 **「我的知识背景」**，填自己的水平（例如「Python 很熟；C++ 只会基础语法；
没接触过图形学」），模型据此决定哪里要补前置知识、哪里可以直接讲。
没提到的领域按完全新手处理。

---

## 纯下载（不生成笔记）

中栏工具栏除了「解析信息 / 生成笔记」，还有一个 **「下载视频」**：
把链接里的视频文件原样存到本地，**不解析、不转写、不调 AI、不写笔记**。

- **点一下就开下，不弹任何窗口**——目录在设置页里配好一次就够了
- **粘贴什么都行**：干净链接、B 站分享文案（`【标题】https://...`）、`b23.tv` 短链、
  甚至一个裸 BV 号 `/ av 号`，后端会自己把链接揪出来
- 进度显示在窗口底部的一条状态条上（百分比 + 目标目录 + 取消按钮），不打断当前操作
- 完成后状态条显示文件名和大小，可「打开文件夹」；15 秒后自动消失
- 失败直接在状态条上说明原因，不会静默失败
- 下载目录在**设置页 → 视频下载目录**：留空就用系统的「下载」文件夹
- 目录可以直接**「浏览…」**选：调的是 Windows 原生的文件夹选择框（Vista+ 的 `IFileDialog`，
  有地址栏和面包屑），不是 Python tkinter 那种自绘窗口
- 进度按阶段映射（先视频后音频 → 0–45% / 45–90%），保证只前进不回退

底层交给 **yt-dlp**（便携包里自带），所以它能下的站点都能下。
遇到下不了的站点会给一句中文原因，而不是甩一段 yt-dlp 的英文报错。

---

## 快速开始

### 方式一：便携压缩包（推荐，解压即可用）

双击 `启动.bat` 即可，包内自带便携 Python、全部依赖、ffmpeg 与 faster-whisper base 模型，无需另外安装任何东西。

首次运行点右上角**齿轮**填 DeepSeek API Key（[platform.deepseek.com](https://platform.deepseek.com) 申请）。

出问题先双击 `检查环境.bat`，它会逐项检查 Python / 依赖 / C 扩展 / ffmpeg / 模型 / 配置 / 磁盘占用。

### 方式二：从源码跑（clone 下来的情况）

需要 **Python 3.10+**、**ffmpeg**、以及一个 DeepSeek API Key。
下面的流程已实测：干净 clone → 建虚拟环境 → 装依赖 → 起服务 → `/api/health` 正常。

```powershell
# 1. 克隆
git clone https://github.com/16653762936/bilibili-notes.git
cd bilibili-notes

# 2. 双击 从源码启动.bat 即可（自动完成 3~5 步）
#    想手动来就继续往下：

python -m venv .venv
.venv\Scripts\python.exe -m pip install -r app\requirements.txt `
    -i https://pypi.tuna.tsinghua.edu.cn/simple

$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "$(Get-Location)\app"
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 17365 --loop asyncio
```

然后浏览器打开 <http://127.0.0.1:17365/>，在右上角**设置**里填 DeepSeek API Key。

补充说明：

- **ffmpeg**：如果 `ffmpeg` 已在 PATH 里（`winget install Gyan.FFmpeg` 或官网压缩包），
  程序会自动找到；也可以放到 `ffmpeg\bin\`，或用 `MYBOX_FFMPEG` / `MYBOX_FFPROBE` 指定。
- **语音模型**：首次生成笔记会从 HuggingFace 下载 faster-whisper 模型（约 140MB）。
  国内下载卡住时先执行 `set HF_ENDPOINT=https://hf-mirror.com` 再启动。
- **B 站 Cookie（可选）**：不填也能用；填了能直接读官方字幕、下更高清晰度。
- 笔记默认写到 `notes\bilibilinote`，可在设置里改成你的 Obsidian 仓库。

需要 **Python 3.12**、**ffmpeg**（`ffmpeg` 与 `ffprobe` 都要）。

```powershell
# 1. 依赖
pip install -r app/requirements.txt

# 2. 语音识别模型（可选；不装则首次转写时联网下载）
#    faster-whisper 会把 base 模型缓存到 HF_HOME 下

# 3. 告诉后端 ffmpeg / 模型在哪（便携包里靠 .bat 设，源码跑需自己设）
$env:MYBOX_HOME      = (Get-Location)
$env:MYBOX_FFMPEG    = "C:\path\to\ffmpeg.exe"
$env:MYBOX_FFPROBE   = "C:\path\to\ffprobe.exe"
$env:MYBOX_WHISPER_MODEL_DIR = "C:\path\to\faster-whisper-base"
$env:PYTHONPATH      = "$(Get-Location)\app"

# 4. 起服务
cd app
python -m uvicorn backend.main:app --host 127.0.0.1 --port 17365 --loop asyncio
```

然后浏览器打开 <http://127.0.0.1:17365/>。

---

## 目录结构

```
BilibiliNotes/
├── 启动.bat               启动服务并在 Edge/Chrome 的 --app 窗口里打开界面
├── 检查环境.bat           环境自检（Python / 依赖 / C 扩展 / ffmpeg / 模型 / 配置 / 磁盘）
├── open-ui.ps1            等健康检查通过后用无地址栏窗口打开界面
├── 使用说明-Windows.md    面向最终用户的图文说明
└── app/
    ├── backend/           后端（FastAPI）
    │   ├── main.py            路由、任务编排、SSE 进度
    │   ├── bilibili_handler.py 解析视频、取字幕、下载音视频
    │   ├── stt.py             faster-whisper 语音转写
    │   ├── summarizer.py      DeepSeek 调用与笔记类型定义
    │   ├── screenshotter.py   ffmpeg 抽帧
    │   ├── note_generator.py  Markdown 落盘（Obsidian 友好）
    │   ├── media.py           工具链定位（ffmpeg / yt-dlp / 平台差异）
    │   └── config.py          配置读写与脱敏
    ├── dist/              已构建的前端（含 logo / favicon）
    ├── scripts/init_config.py  首次运行生成默认配置
    └── requirements.txt
```

配置文件在 `%USERPROFILE%\.bilibili-notes\config.json`，**不随仓库分发**。
删掉它会恢复默认（笔记不会丢）。

---

## 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查 + 工具链状态 |
| GET | `/api/tooling` | 工具链报告 |
| GET | `/api/note-styles` | 笔记类型下拉数据 |
| GET / PUT | `/api/config` | 读写配置（GET 返回脱敏结果） |
| GET | `/api/history` | 最近笔记（`limit` 1–500） |
| GET | `/api/note` | 读取笔记正文 |
| GET | `/api/note-asset` | 笔记内图片 |
| GET | `/api/cover` | B 站封面代理（带缓存） |
| POST | `/api/inspect` | 只解析视频信息 |
| POST | `/api/download` | **纯下载**：只把视频存到下载目录，不解析、不做笔记 |
| POST | `/api/process` | 建任务，开始生成 |
| GET | `/api/progress/{id}` | SSE 进度流 |
| GET | `/api/task/{id}` | 取任务结果 |
| POST | `/api/save/{id}` | 保存笔记 |
| POST | `/api/cancel/{id}` | 取消任务 |
| POST | `/api/reveal` | 在资源管理器中定位 |
| POST | `/api/open-url` | 用默认浏览器打开链接 |

---

## Windows 适配修复记录

上游是从 macOS 版本移植过来的，本仓库在 Windows 上修掉了这些问题：

**启动脚本（致命）**
- `启动.bat` / `检查环境.bat` / `open-ui.ps1` 原为 **Unix LF 换行 + UTF-8 无 BOM**。
  cmd.exe 按字节偏移重定位，遇到 LF + 多字节中文 + `chcp 65001` 会错位，把命令从中间截断执行
  （表现为 `'OENCODING' is not recognized` 之类，uvicorn 那行根本没跑到，服务起不来）。
  已改为 **CRLF + UTF-8 BOM**。
- `检查环境.bat` 里依赖检查漏写 `import importlib.util`，以及 ffmpeg 版本检测的 `for /f` 嵌套引号被当成窗口标题，均已修正。

**编码 / MIME**
- Windows 注册表没有 `.webp` 映射，`mimetypes.guess_type` 返回 `None`，
  Starlette 的 `FileResponse` 会把笔记截图兜底成 `text/plain; charset=utf-8`。
  已在 `main.py` 显式注册 `.webp/.avif/.m4a/.opus/.webm`。
- `/api/cover` 的缓存文件名为 `<sha1>.img`，缓存命中时不传 `media_type`，
  同样被兜底成 `text/plain`（冷缓存反而正确）。已改为按文件头嗅探真实类型。

**平台文案**
- `⌘K / ⌘S / ⌘,` → `Ctrl+K / Ctrl+S / Ctrl+,`（代码里本来就是 `metaKey || ctrlKey`，只有文案错）
- `Finder` → `资源管理器`
- ffmpeg 缺失提示里的 `brew install ffmpeg`（macOS Homebrew）→ 改为提示恢复便携包内的 ffmpeg
- `⇧` → `Shift`

**界面去重**
- 解析视频后，视频卡片里的「开始生成笔记」和顶部工具栏的「生成笔记」调的是同一个函数，
  同屏出现两个入口。已移除卡片里那颗，操作统一收在顶部动作条（「在 B 站打开」保留）。

**原生文件夹选择框**
- 便携 Python **没有 tkinter**，系统也常常没有 PowerShell 7（PS 5.1 自带的
  `FolderBrowserDialog` 是老式树状窗口）。所以 `app/scripts/pick_folder.ps1` 用 C# 互操作
  直接调 Vista+ 的 `IFileDialog` + `FOS_PICKFOLDERS`，拿到带地址栏的现代选择器，
  只有互操作失败时才退回老对话框。
- 那个脚本刻意写成**纯 ASCII**：PowerShell 5.1 读取无 BOM 脚本时按 ANSI 代码页解码，
  中文字面量会变乱码（曾经把整个脚本搞崩）。中文标题/按钮文案由后端以 **base64** 传参，
  避开命令行被控制台代码页打乱的问题。
- 对话框以当前前台窗口为 owner 弹出，否则它会跑到应用窗口后面，看着像按钮没反应。

**图标**
- 自带 `favicon.ico` 与多尺寸 PNG logo（前端 `dist/` 内）。
- 后端只挂载了 `/assets`，落在 `dist/` 根目录的图标原本没有路由；
  已扩展为带**文件名白名单**的图标路由，避免变成任意文件读取。

**下载失败排查（2026-11）**
- **用的是哪个 yt-dlp**：便携包里虽然带着 yt-dlp，但 `find_tool()` 会先在 PATH 里
  命中系统 Python 装的那个，结果「包内升级了也不生效」。现在便携包内一律用
  `python -m yt_dlp`，`/api/tooling` 也会报告实际版本与调用命令。
- 便携包内的 yt-dlp 已从 2026.08.19 升到 **2026.11.23**（直接解包 wheel 替换，无需 pip）。
- **B 站 Cookie 一路没传下去**：两条下载路径（「下载视频」和「生成笔记」）调用 yt-dlp 时
  都没有带 Cookie，需要登录才有的清晰度、番剧、部分影视因此失败；现在设置里填了
  `SESSDATA` 就会自动加到 yt-dlp 请求头里。
- **分 P 视频只下第一 P**：URL 带 `?p=3` 时原来固定加 `--no-playlist`，永远拿到第 1 P；
  现在会按 `p` 参数取对应分 P。
- **卡死没有超时**：yt-dlp 偶尔没有任何输出地挂住（网络半死、需要登录在等验证），
  界面就一直转圈。现在 180 秒没有任何进展会自动中止并说明原因；
  失败时完整日志写到 `~/.bilibili-notes/logs/download-*.log`。
- 重试参数加厚：`--retries 10 --fragment-retries 10 --extractor-retries 3 --socket-timeout 20`。
- **预览页缩略图全是坏图**：前端请求 `/api/assets/<文件名>`，后端根本没有这个路由，
  所以生成完的截图缩略图 404。已补上带路径穿越防护的路由。

---

## 已知边界

- 仓库不含便携运行时；`python/`、`ffmpeg/`、`models/` 与 `notes/` 都在 `.gitignore` 中。
  `models/faster-whisper-base/model.bin`（138 MB）与 `ffmpeg/bin/avcodec-63.dll`（114 MB）
  都超过 GitHub 单文件 100 MB 上限。
- 前端只有构建产物 `app/dist/`，没有源码。所以界面层的改动（macOS 文案、「下载视频」按钮、
  设置页的下载目录字段）都是**直接改在压缩后的 bundle 上的**，改动清单见
  [Windows 适配修复记录](#windows-适配修复记录) 与「纯下载」一节；日后若重新构建前端需要同步。
  「下载视频」面板本身是一个独立的原生 JS 片段，内联在 `app/dist/index.html` 里。
- **抖音暂不支持**。yt-dlp 的 `DouyinIE` 只调用 `aweme/v1/web/aweme/detail/`，
  而该接口现在需要 `a_bogus` 签名 + Argus 风控 Cookie（yt-dlp 源码里那句
  `# TODO: Run verification challenge code to generate signature cookies` 就是这件事）。
  实测纯 HTTP 各种入口都拿不到数据：详情接口返回 `403 Blocked by ArgusSecurityPlugin`，
  老的 `iesdouyin` iteminfo 接口返回空，分享页只剩页面壳。所以下载抖音会给出明确提示，
  不会假装成功。要支持抖音需要单独实现签名（或用浏览器自动化），不在当前实现范围内。
- `/api/note` 对「笔记目录本身」返回 403 而非 404（安全边界，界面不会走到这条路径）。
- 生成笔记需要自备 DeepSeek API Key；配置 B 站 Cookie（`SESSDATA`）可大幅提速。
