if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Error "需要 PowerShell 7+，当前 $($PSVersionTable.PSVersion)。请用：pwsh -NoProfile -File $PSCommandPath"
    exit 1
}
$ErrorActionPreference = 'Stop'
# GUI 用 pythonw 启动，不弹黑色控制台窗口。
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    $projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $projectPython)) {
    Write-Error '请先按 README.md 创建项目内 .venv 并安装依赖。'
    exit 1
}
& $projectPython -X utf8 (Join-Path $PSScriptRoot 'python版\gui.py')
exit $LASTEXITCODE
