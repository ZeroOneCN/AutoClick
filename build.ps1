# AutoClick 打包脚本（Windows / PowerShell）
#
# 用法：
#   .\build.ps1            # 完整版：内置 OpenCV，支持「找图」，产物 dist\AutoClick.exe
#   .\build.ps1 -Light     # 轻量版：不含 OpenCV，仅「找控件」，产物 dist\AutoClick_Lite.exe
#
# 前置条件：已安装 Python 3.10+ 与 PyInstaller（pip install pyinstaller）；
#           完整版还需 pip install opencv-python numpy。
# 打包结果：单文件 EXE（约 100~150MB / 轻量版约 20MB），双击即可运行，
#           目标机器无需安装 Python 与任何依赖；flows/ 与 templates/ 在 EXE 同级自动创建。

param(
    [switch]$Light
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "== AutoClick 打包 ==" -ForegroundColor Cyan

# 1. 校验内置 adb 三件套是否就位（缺少 adb 时工具完全不可用）
$adbDir = Join-Path $PSScriptRoot "resources\adb"
$required = @("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")
$missing = @()
foreach ($name in $required) {
    if (-not (Test-Path (Join-Path $adbDir $name))) { $missing += $name }
}
if ($missing.Count -gt 0) {
    Write-Host "缺少内置 adb 文件：$($missing -join '、')" -ForegroundColor Red
    Write-Host "请把 Android SDK platform-tools 下的 adb.exe / AdbWinApi.dll / AdbWinUsbApi.dll 复制到 $adbDir" -ForegroundColor Yellow
    exit 1
}
Write-Host "内置 adb 校验通过：$adbDir" -ForegroundColor Green

# 2. 按模式设置环境变量（build.spec 读取它决定是否内置 OpenCV）
if ($Light) {
    $env:AUTOCLICK_LIGHT = "1"
    Write-Host "构建模式：轻量版（不含 OpenCV，仅支持找控件）" -ForegroundColor Yellow
} else {
    Remove-Item Env:AUTOCLICK_LIGHT -ErrorAction SilentlyContinue
    $env:AUTOCLICK_LIGHT = $null
    Write-Host "构建模式：完整版（内置 OpenCV，支持找图）" -ForegroundColor Yellow
}

# 3. 执行打包（失败时重试一次：杀毒软件实时扫描可能短暂锁定输出文件）
$attempts = 0
$code = 1
while ($attempts -lt 2) {
    $attempts++
    python -m PyInstaller --noconfirm --clean build.spec
    $code = $LASTEXITCODE
    if ($code -eq 0) { break }
    if ($attempts -lt 2) {
        Write-Host "打包失败（退出码 $code），可能是杀毒软件正在扫描输出文件而短暂锁定；稍后重试…" -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
}
if ($code -ne 0) {
    Write-Host "打包失败（PyInstaller 退出码 $code）；请关闭杀毒软件实时防护或把项目目录加入白名单后重试" -ForegroundColor Red
    exit $code
}

# 4. 汇报产物
$name = if ($Light) { "AutoClick_Lite.exe" } else { "AutoClick.exe" }
$exePath = Join-Path $PSScriptRoot "dist\$name"
if (Test-Path $exePath) {
    $size = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
    Write-Host "打包完成：$exePath（$size MB）" -ForegroundColor Green
    Write-Host "可直接把该文件复制到任意 Windows 机器双击运行（无需 Python 环境）。" -ForegroundColor Green
} else {
    Write-Host "未找到产物：$exePath" -ForegroundColor Red
    exit 1
}
