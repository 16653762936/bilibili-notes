@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PY=%ROOT%python\python.exe"
set "MYBOX_HOME=%ROOT%"
set "MYBOX_FFMPEG=%ROOT%ffmpeg\bin\ffmpeg.exe"
set "MYBOX_FFPROBE=%ROOT%ffmpeg\bin\ffprobe.exe"
set "HF_HOME=%ROOT%models"
set "HF_HUB_OFFLINE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%ROOT%app"

echo ============================================================
echo   环境自检
echo ============================================================
echo.

echo [1] 便携 Python
if exist "%PY%" (
  "%PY%" -c "import sys; print('     OK  ' + sys.version.split()[0] + '  (' + sys.executable + ')')"
) else (
  echo     X  找不到 python\python.exe
)
echo.

echo [2] 依赖包（缺哪个会直接列出来）
"%PY%" -c "import importlib.util;mods=['fastapi','uvicorn','httpx','openai','sse_starlette','bilibili_api','faster_whisper','ctranslate2','onnxruntime','av','numpy','yt_dlp','Cryptodome','lxml','bs4','qrcode_terminal','tzdata','apscheduler'];bad=[m for m in mods if not importlib.util.find_spec(m)];print('     OK  全部', len(mods), '个依赖都在') if not bad else print('     X   缺少:', ', '.join(bad))"
echo.

echo [2b] C 扩展加载测试（缺 MSVC 运行库会在这里暴露）
"%PY%" -c "import ctranslate2, numpy, onnxruntime, av; print('     OK  ctranslate2 / numpy / onnxruntime / av 均可加载')"
if errorlevel 1 (
  echo     X   有 C 扩展加载失败
  echo         最常见原因：缺少 Microsoft Visual C++ 2015-2022 运行库
  echo         下载安装：https://aka.ms/vs/17/release/vc_redist.x64.exe
  echo         装完重开这个窗口再试
)
echo.

echo [3] ffmpeg
if exist "%MYBOX_FFMPEG%" (
  powershell -NoProfile -Command "$v = (& '%MYBOX_FFMPEG%' -version 2>&1 | Select-String -Pattern '^ffmpeg version' | Select-Object -First 1); if ($v) { '     OK  ffmpeg ' + ($v.ToString().Split(' ')[2]) } else { '     ?   ffmpeg 存在但读不到版本号' }"
) else (
  echo     X   找不到 ffmpeg\bin\ffmpeg.exe
)
echo.

echo [4] 语音识别模型（不打进包里的话首次使用会联网下载）
if exist "%ROOT%models\faster-whisper-base\model.bin" (
  echo     OK  faster-whisper base 已内置
) else (
  echo     !   没有内置模型，首次转写时会从 HuggingFace 下载（约 140MB）
)
echo.

echo [5] 配置文件
if exist "%USERPROFILE%\.bilibili-notes\config.json" (
  echo     OK  %USERPROFILE%\.bilibili-notes\config.json
) else (
  echo     !   还没有配置，启动后会自动生成一份
)
echo.

echo [6] 磁盘占用
powershell -NoProfile -Command "$s=(Get-ChildItem -Recurse -File '%ROOT%' | Measure-Object -Property Length -Sum).Sum; '{0:N0} MB' -f ($s/1MB)"
echo.

echo ============================================================
echo   自检结束。有 X 的话先解决再启动。
echo ============================================================
pause
