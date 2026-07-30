$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到 .venv。请先创建虚拟环境并安装 requirements.txt。"
}

Push-Location $ProjectRoot
try {
    $Version = (& $Python -c "from photoculler import __version__; print(__version__)").Trim()
    if (-not $Version) { throw "无法读取应用版本号。" }
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "测试失败，已停止构建。" }
    & $Python -m PyInstaller --noconfirm --clean PhotoCuller.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }
    Copy-Item -LiteralPath "README.md" -Destination "dist\照片筛选器\使用说明.md" -Force

    $DistRoot = Join-Path $ProjectRoot "dist"
    $PrimaryOutput = Join-Path $DistRoot "照片筛选器"
    $VersionedOutput = Join-Path $DistRoot "照片筛选器-v$Version"
    if (Test-Path -LiteralPath $VersionedOutput) {
        $ResolvedDist = (Resolve-Path -LiteralPath $DistRoot).Path.TrimEnd("\")
        $ResolvedVersioned = (Resolve-Path -LiteralPath $VersionedOutput).Path
        if (-not $ResolvedVersioned.StartsWith("$ResolvedDist\", [StringComparison]::OrdinalIgnoreCase)) {
            throw "版本化输出目录不在 dist 内，已停止清理。"
        }
        Remove-Item -LiteralPath $ResolvedVersioned -Recurse -Force
    }
    Copy-Item -LiteralPath $PrimaryOutput -Destination $VersionedOutput -Recurse
    Write-Host "构建完成："
    Write-Host "  $PrimaryOutput\照片筛选器.exe"
    Write-Host "  $VersionedOutput\照片筛选器.exe"
}
finally {
    Pop-Location
}
