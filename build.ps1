$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到 .venv。请先创建虚拟环境并安装 requirements.txt。"
}

Push-Location $ProjectRoot
try {
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "测试失败，已停止构建。" }
    & $Python -m PyInstaller --noconfirm --clean PhotoCuller.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }
    Copy-Item -LiteralPath "README.md" -Destination "dist\照片筛选器\使用说明.md" -Force
    Write-Host "构建完成：$ProjectRoot\dist\照片筛选器\照片筛选器.exe"
}
finally {
    Pop-Location
}
