# 需要 PowerShell 7+
if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Error "需要 PowerShell 7+，当前 $($PSVersionTable.PSVersion)"
    exit 1
}
# 带 WebView2 调试端口启动客户管家，供 DOM 通道连接（只读网页界面，不改客户端文件）。
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = '--remote-debugging-port=9223'
$app = 'D:\圆通科技\客户管家\客户管家\YtoChatApp.exe'
if (-not (Test-Path -LiteralPath $app)) {
    Write-Error "找不到客户管家：$app"
    exit 1
}
Start-Process -FilePath $app
Write-Host "已启动客户管家（CDP 端口 9223）"
