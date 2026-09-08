param(
    [switch]$Browser
)

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

    if ($Browser) {
        $Npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        $Node = Get-Command node -ErrorAction SilentlyContinue
        $Playwright = Join-Path $ProjectRoot "node_modules\playwright\cli.js"
        if ($Npm) {
            & $Npm.Source run test:dom
        } elseif ($Node -and (Test-Path -LiteralPath $Playwright)) {
            & $Node.Source $Playwright test --config tests/dom/playwright.config.mjs
        } else {
            throw "未找到 Node.js 或已安装的 Playwright，请先安装 Node.js 并运行 npm ci。"
        }
        if ($LASTEXITCODE -ne 0) { throw "Edge Playwright 测试失败。" }
    }

    Write-Host $(if ($Browser) {
        "核心检查和可选浏览器检查全部通过。"
    } else {
        "核心发布检查通过；浏览器检查未启用。"
    })
}
finally {
    Pop-Location
}
