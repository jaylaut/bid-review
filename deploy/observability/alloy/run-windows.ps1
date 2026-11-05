# 本机（Windows）运行 Alloy 采集 —— 与容器形态共用同一份 alloy/config.alloy
#
# 为什么本机跑二进制：Python 进程跑在宿主机（不在容器里），采集器同机跑最可靠——
# Windows 下把日志目录 bind mount 进容器，inotify 事件会丢，表现为"写了日志但没采到"。
#
# 用法（PowerShell）：
#   cd deploy/observability/alloy
#   ./run-windows.ps1
#   # 自定义： ./run-windows.ps1 -LokiUrl http://10.0.0.5:3100/loki/api/v1/push
#
# 前置：./alloy.exe 已就位（官方 release: grafana/alloy 的 alloy-windows-amd64.exe.zip）
# 停止：Ctrl+C

param(
    [string]$AlloyExe = "E:/CodingTools/alloy/alloy-windows-amd64.exe",
    [string]$Config   = "$PSScriptRoot/config.alloy",
    [string]$LogPath  = "",
    [string]$LokiUrl  = "http://localhost:3100/loki/api/v1/push",
    [string]$Storage  = "$PSScriptRoot/../../../data/alloy"
)

if (-not (Test-Path $AlloyExe)) {
    Write-Error "找不到 alloy 可执行文件：$AlloyExe（改 -AlloyExe 指向实际路径）"
    exit 1
}

# 日志文件默认取 review-engine/logs/review-engine.log（即 .env 里 LOG_DIR 的落点）
if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $LogPath = (Resolve-Path "$PSScriptRoot/../../../review-engine/logs/review-engine.log").Path
}

# config.alloy 里用 sys.env 读这两个变量 —— 同一份配置服务容器与本机两种形态
$env:LOG_PATH = $LogPath
$env:LOKI_URL = $LokiUrl

Write-Host "采集源: $LogPath"
Write-Host "写入到: $LokiUrl"
Write-Host "控制台: http://127.0.0.1:12345"

& $AlloyExe run `
    --server.http.listen-addr=127.0.0.1:12345 `
    --storage.path=$Storage `
    $Config
