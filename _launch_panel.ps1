$ErrorActionPreference = "Continue"
# 三脚架 · 启动脚本（PowerShell 版）
# **不写死任何路径**：解释器优先读项目根目录 panel_config.json 的 comfy_python，
# 读不到就自动找同级/父级的 .venv、venv。面板已在跑就只开界面。
$base  = Split-Path -Parent $MyInvocation.MyCommand.Path
$panel = Join-Path $base "panel.py"
$url   = "http://127.0.0.1:8899"

function Get-PanelPython {
    $cfgPath = Join-Path $base "panel_config.json"
    if (Test-Path $cfgPath) {
        try {
            $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($cfg.comfy_python -and (Test-Path $cfg.comfy_python)) { return $cfg.comfy_python }
        } catch {}
    }
    foreach ($root in @($base, (Split-Path -Parent $base))) {
        foreach ($v in @(".venv", "venv", "env")) {
            foreach ($n in @("pythonw.exe", "python.exe")) {
                $p = Join-Path $root (Join-Path $v (Join-Path "Scripts" $n))
                if (Test-Path $p) { return $p }
            }
        }
    }
    return "pythonw.exe"
}

Write-Host "  正在启动 AI 生图面板 ..." -ForegroundColor Cyan

function Test-Panel {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", 8899)
        $c.Close()
        return $true
    } catch { return $false }
}

if (-not (Test-Panel)) {
    $py = Get-PanelPython
    Start-Process -FilePath $py -ArgumentList @("-u", $panel) -WorkingDirectory $base -WindowStyle Hidden
    for ($i = 1; $i -le 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Panel) { break }
    }
}

if (Test-Panel) {
    Write-Host "  面板已就绪，正在打开浏览器 ..." -ForegroundColor Green
    Start-Process $url
    Write-Host ""
    Write-Host "  地址：$url" -ForegroundColor White
    Write-Host "  这个窗口可以关掉，面板会继续在后台运行。" -ForegroundColor Gray
    Start-Sleep -Seconds 3
} else {
    Write-Host "  面板没能在 30 秒内起来。请检查 panel_config.json 里的 comfy_dir / comfy_python。" -ForegroundColor Red
    Read-Host "  按回车关闭"
}
