$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到 .venv，请先安装 requirements-dev.txt。"
}

Push-Location $ProjectRoot
try {
    & $Python -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Ruff 检查失败。" }

    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Python 测试失败。" }

    & $Python -m unittest tests.test_face_analysis.FaceAnalysisTests.test_shipped_models_match_the_pinned_checksums -v
    if ($LASTEXITCODE -ne 0) { throw "眨眼检测模型校验失败。" }

    $Npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $Npm) { throw "未找到 Node.js/npm，无法运行 Edge Playwright 测试。" }
    & $Npm.Source run test:dom
    if ($LASTEXITCODE -ne 0) { throw "Edge Playwright 测试失败。" }

    Write-Host "全部发布检查通过。"
}
finally {
    Pop-Location
}
