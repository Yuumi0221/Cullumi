$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd("\")
$BuildRoot = Join-Path $TempRoot ("PhotoCuller-build-" + [guid]::NewGuid().ToString("N"))
$BuildWorkPath = Join-Path $BuildRoot "work"
$BuildDistPath = Join-Path $BuildRoot "dist"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到 .venv。请先创建虚拟环境并安装 requirements.txt。"
}

Push-Location $ProjectRoot
try {
    $Version = (& $Python -c "from photoculler import __version__; print(__version__)").Trim()
    if (-not $Version) { throw "无法读取应用版本号。" }
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "测试失败，已停止构建。" }
    & $Python -m PyInstaller --noconfirm --clean --workpath $BuildWorkPath --distpath $BuildDistPath PhotoCuller.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }

    $DistRoot = Join-Path $ProjectRoot "dist"
    $PrimaryOutput = Join-Path $BuildDistPath "Cullumi"
    Copy-Item -LiteralPath "README.md" -Destination (Join-Path $PrimaryOutput "使用说明.md") -Force
    $VersionedOutput = Join-Path $DistRoot "Cullumi-v$Version"
    if (Test-Path -LiteralPath $VersionedOutput) {
        $ResolvedDist = (Resolve-Path -LiteralPath $DistRoot).Path.TrimEnd("\")
        $ResolvedVersioned = (Resolve-Path -LiteralPath $VersionedOutput).Path
        if (-not $ResolvedVersioned.StartsWith("$ResolvedDist\", [StringComparison]::OrdinalIgnoreCase)) {
            throw "版本化输出目录不在 dist 内，已停止清理。"
        }
        Remove-Item -LiteralPath $ResolvedVersioned -Recurse -Force
    }
    Copy-Item -LiteralPath $PrimaryOutput -Destination $VersionedOutput -Recurse
    $ReleaseArchive = Join-Path $DistRoot "Cullumi-v$Version-Windows-Portable.zip"
    $ArchiveFullPath = [IO.Path]::GetFullPath($ReleaseArchive)
    if (-not $ArchiveFullPath.StartsWith("$($DistRoot.TrimEnd('\'))\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "发布压缩包不在 dist 内，已停止生成。"
    }
    for ($ArchiveAttempt = 1; $ArchiveAttempt -le 5; $ArchiveAttempt++) {
        try {
            Compress-Archive -Path (Join-Path $VersionedOutput "*") -DestinationPath $ArchiveFullPath -CompressionLevel Optimal -Force
            break
        }
        catch {
            if ($ArchiveAttempt -eq 5) { throw }
            Start-Sleep -Seconds 2
        }
    }
    Write-Host "构建完成："
    Write-Host "  $VersionedOutput\Cullumi.exe"
    Write-Host "  $ArchiveFullPath"
}
finally {
    Pop-Location
    if (Test-Path -LiteralPath $BuildRoot) {
        $ResolvedBuildRoot = (Resolve-Path -LiteralPath $BuildRoot).Path
        if ($ResolvedBuildRoot.StartsWith("$TempRoot\PhotoCuller-build-", [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $ResolvedBuildRoot -Recurse -Force
        }
    }
}
