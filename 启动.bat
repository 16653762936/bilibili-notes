@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   Bilibili Notes  ^|  B 站视频笔记工具
echo ============================================================
echo.

rem ---------- 目录与运行环境 ----------
set "ROOT=%~dp0"
set "PY=%ROOT%python\python.exe"
set "MYBOX_HOME=%ROOT%"
set "MYBOX_FFMPEG=%ROOT%ffmpeg\bin\ffmpeg.exe"
set "MYBOX_FFPROBE=%ROOT%ffmpeg\bin\ffprobe.exe"
set "HF_HOME=%ROOT%models"
set "HF_HUB_OFFLINE=1"
rem 模型是平铺的普通目录，直接指过去，连 HuggingFace 缓存都不碰
set "MYBOX_WHISPER_MODEL_DIR=%ROOT%models\faster-whisper-base"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%ROOT%app"

if not exist "%PY%" (
  echo [错误] 找不到 python\python.exe
  echo        请确认整个文件夹是一起解压的，没有单独挪动里面的文件。
  goto :fail
)
if not exist "%ROOT%ffmpeg\bin\ffmpeg.exe" (
  echo [错误] 找不到 ffmpeg\bin\ffmpeg.exe
  echo        请确认整个文件夹是一起解压的。
  goto :fail
)

rem ---------- 首次运行：生成一份默认配置（交给 python 写，避免 bat 转义地狱）----------
set "CFG=%USERPROFILE%\.bilibili-notes\config.json"
if not exist "%CFG%" (
  echo [首次运行] 正在生成默认配置...
  "%PY%" "%ROOT%app\scripts\init_config.py" "%ROOT%notes\bilibilinote"
  echo             笔记会存到: %ROOT%notes\bilibilinote
  echo             稍后可在界面右上角「设置」里改成你的 Obsidian 仓库路径。
  echo.
)

rem ---------- 端口占用检查 ----------
set "PORT=17365"
rem 用 curl 探活最可靠：netstat 的输出在中文系统上是「侦听」而不是 LISTENING
curl -s -o nul -m 2 http://127.0.0.1:%PORT%/api/health >nul 2>&1
if !errorlevel! equ 0 (
  echo [提示] 端口 %PORT% 已被占用，可能是已经开了一个实例。
  echo        直接打开界面；如果界面打不开，请先关掉旧的窗口再重试。
  call :openui
  goto :eof
)

echo [1/2] 正在启动本地服务（这个窗口不要关，关了服务就停了）...
echo       界面地址: http://127.0.0.1:%PORT%/
echo.

rem 另开一个隐藏窗口去等健康检查、再打开界面
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ROOT%open-ui.ps1" -Port %PORT%

echo [2/2] 日志如下（生成笔记时的进度也会打在这里）:
echo ------------------------------------------------------------
cd /d "%ROOT%app"
"%PY%" -m uvicorn backend.main:app --host 127.0.0.1 --port %PORT% --loop asyncio
set "RC=%errorlevel%"
echo ------------------------------------------------------------
echo 服务已退出（代码 %RC%）。
if not "%RC%"=="0" (
  echo.
  echo 常见原因：
  echo   1. 依赖没装全    -^> 先运行「检查环境.bat」
  echo   2. 端口被占用    -^> 关掉其它 Bilibili Notes 窗口
  echo   3. Python 报错   -^> 把上面的红字发给 AI
)
pause
goto :eof

:openui
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ROOT%open-ui.ps1" -Port %PORT%
goto :eof

:fail
pause
