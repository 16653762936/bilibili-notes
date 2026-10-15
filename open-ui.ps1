# 等本地服务起来，然后打开界面。
# 优先用 Edge/Chrome 的 --app 模式（没有地址栏和标签页，看起来就是个桌面应用），
# 找不到就用系统默认浏览器。
param([int]$Port = 17365)

$ErrorActionPreference = "SilentlyContinue"
$url = "http://127.0.0.1:$Port/"

# 最多等 90 秒（首次启动要加载 Python 和依赖，慢一点）
$ready = $false
for ($i = 0; $i -lt 180; $i++) {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri "${url}api/health" -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 500
}

if (-not $ready) {
    # 服务没起来也照样打开浏览器，让用户看到报错
    Start-Process $url
    exit 1
}

$candidates = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)

$browser = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($browser) {
    Start-Process $browser "--app=$url"
} else {
    Start-Process $url
}
