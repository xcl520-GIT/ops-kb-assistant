# ============================================================
#  在桌面创建「运维助手」快捷方式
#  用法：右键 → 使用 PowerShell 运行；或在 PowerShell 里执行本文件
#  说明：只创建快捷方式，**不会**添加任何开机自启项
# ============================================================

$ErrorActionPreference = 'Stop'

$Root     = Split-Path $PSScriptRoot -Parent
$Bat      = Join-Path $Root 'start.bat'
$Desktop  = [Environment]::GetFolderPath('Desktop')
$LinkPath = Join-Path $Desktop '运维助手.lnk'
$IconExe  = Join-Path $env:SystemRoot 'System32\SHELL32.dll'

if (-not (Test-Path $Bat)) {
    Write-Host "找不到启动脚本：$Bat" -ForegroundColor Red
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($LinkPath)
$lnk.TargetPath       = $Bat
$lnk.WorkingDirectory = $Root
$lnk.Description      = '本地运维知识库助手（双击启动，再访问 http://127.0.0.1:8765）'
$lnk.IconLocation     = "$IconExe,220"     # 一个工具图标
$lnk.WindowStyle      = 7                  # 最小化启动，避免黑框

$lnk.Save()

Write-Host ''
Write-Host '✅ 桌面快捷方式已创建' -ForegroundColor Green
Write-Host "   快捷方式：$LinkPath"
Write-Host "   目标脚本：$Bat"
Write-Host ''
Write-Host '使用方式：双击桌面「运维助手」→ 自动启动服务并打开浏览器'
Write-Host '停机方式：双击项目里的 stop.bat，或在页面右上角点 ⏻'
Write-Host ''
