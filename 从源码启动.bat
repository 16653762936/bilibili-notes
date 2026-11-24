@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Bilibili Notes（源码模式）

rem ============================================================
rem  源码模式启动：用系统 Python 建 .venv、装依赖、起服务
rem  适用于「从 GitHub 下载 ZIP」的情况——这种包里没有便携运行时。
rem ============================================================

echo ============================================================
echo   Bilibili Notes  ^|  源码模式启动
echo ============================================================
echo.

set "ROOT=%~dp0"
cd /d "%ROOT%"

rem ---------- 1. 找系统 Python ----------
set "SYS_PY="
for /f "delims=" %%P in ('where python 2^>nul') do (
  if not defined SYS_PY set "SYS_PY=%%P"
)
if not defined SYS_PY (
  echo [错误] 没找到 python
  echo.
  echo        请先安装 Python 3.10 或更高版本：
  echo          https://www.python.org/downloads/
  echo        安装时务必勾选 "Add Python to PATH"，装完重开这个窗口。
  goto :fail
)

"%SYS_PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [错误] Python 版本太低，需要 3.10 以上
  "%SYS_PY%" --version
  goto :fail
)
echo [0/3] 使用系统 Python:
"%SYS_PY%" --version
echo       路径: %SYS_PY%
echo.

rem ---------- 2. 虚拟环境 ----------
set "VENV=%ROOT%.venv"
set "PY=%VENV%\Scripts\python.exe"
if not exist "%PY%" (
  echo [1/3] 首次运行，正在创建虚拟环境 .venv ...
  "%SYS_PY%" -m venv "%VENV%"
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败
    goto :fail
  )
) else (
  echo [1/3] 虚拟环境已存在
)

rem ---------- 3. 依赖 ----------
echo [2/3] 检查依赖 ...
"%PY%" -c "import importlib.util,sys;mods=['fastapi','uvicorn','httpx','openai','sse_starlette','bilibili_api','faster_whisper','ctranslate2','onnxruntime','av','numpy','yt_dlp','Cryptodome','lxml','bs4','qrcode_terminal','tzdata','apscheduler'];sys.exit(0 if all(importlib.util.find_spec(m) for m in mods) else 3)" >nul 2>&1
if errorlevel 3 (
  echo       依赖不全，开始安装（首次需要几分钟）...
  if not defined PIP_INDEX set "PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"
  echo       使用镜像: !PIP_INDEX!
  "%PY%" -m pip install --upgrade pip -i "!PIP_INDEX!" --quiet
  "%PY%" -m pip install -r "%ROOT%app\requirements.txt" -i "!PIP_INDEX!"
  if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。可以换镜像再试，例如：
    echo        set PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
    goto :fail
  )
)
echo       依赖就绪
echo.

rem ---------- ffmpeg ----------
if not exist "%ROOT%ffmpeg\bin\ffmpeg.exe" (
  echo [提示] 没找到 ffmpeg\bin\ffmpeg.exe
  echo        截图和音视频合并需要 ffmpeg。把 ffmpeg.exe 和 ffprobe.exe 放到
  echo        ffmpeg\bin\ 下即可；也可以用环境变量指定：
  echo          set MYBOX_FFMPEG=D:\ffmpeg\bin\ffmpeg.exe
  echo          set MYBOX_FFPROBE=D:\ffmpeg\bin\ffprobe.exe
  echo        没有 ffmpeg 也能启动界面，但「生成笔记」会在下载/截图时失败。
  echo.
)

rem ---------- 语音模型 ----------
if not exist "%ROOT%models\faster-whisper-base\model.bin" (
  echo [提示] 没找到内置语音模型。首次「生成笔记」时会自动从 HuggingFace 下载
  echo        faster-whisper 模型（约 140MB），只下一次。
  echo        如果下载卡住或超时，先设置镜像再重跑本脚本：
  echo          set HF_ENDPOINT=https://hf-mirror.com
  echo.
)

rem ---------- 运行环境 ----------
set "MYBOX_HOME=%ROOT%"
if exist "%ROOT%ffmpeg\bin\ffmpeg.exe" (
  set "MYBOX_FFMPEG=%ROOT%ffmpeg\bin\ffmpeg.exe"
  set "MYBOX_FFPROBE=%ROOT%ffmpeg\bin\ffprobe.exe"
)
set "HF_HOME=%ROOT%models"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%ROOT%app"
set "PORT=17365"

rem 首次运行生成默认配置
set "CFG=%USERPROFILE%\.bilibili-notes\config.json"
if not exist "%CFG%" (
  echo [初始化] 正在生成默认配置...
  "%PY%" "%ROOT%app\scripts\init_config.py" "%ROOT%notes\bilibilinote" >nul 2>&1
  echo          笔记默认存到: %ROOT%notes\bilibilinote
  echo          稍后可在界面右上角「设置」里改成你的 Obsidian 仓库路径。
  echo.
)

rem ---------- 端口占用 ----------
curl -s -o nul -m 2 http://127.0.0.1:%PORT%/api/health >nul 2>&1
if !errorlevel! equ 0 (
  echo [提示] 端口 %PORT% 已被占用，可能已经开着一个实例。
  echo        直接打开界面。
  start "" "http://127.0.0.1:%PORT%/"
  goto :eof
)

echo [3/3] 启动本地服务（这个窗口不要关，关了服务就停）
echo       界面地址: http://127.0.0.1:%PORT%/
echo.
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ROOT%open-ui.ps1" -Port %PORT%

cd /d "%ROOT%app"
"%PY%" -m uvicorn backend.main:app --host 127.0.0.1 --port %PORT% --loop asyncio

echo.
echo ------------------------------------------------------------
echo 服务已退出。
echo 出错先看上面最后几行；依赖问题可重跑本脚本，它会自动补装。
pause
goto :eof

:fail
echo.
pause
exit /b 1
