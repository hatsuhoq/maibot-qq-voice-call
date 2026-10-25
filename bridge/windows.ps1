param(
    [ValidateSet('check', 'install', 'start', 'stop')][string]$Action = 'check',
    # 默认取 scripts\paths.local.bat 设置的环境变量；也可直接传参。
    [string]$NapCatDir = $env:NAPCAT_SHELL_DIR,
    [string]$QQDir = $env:NAPCAT_QQ_DIR,
    [int]$BridgePort = 6110,
    [int]$HostPort = 6111
)
$ErrorActionPreference = 'Stop'
if (-not $NapCatDir -or -not $QQDir) {
    throw '请传入 -NapCatDir 和 -QQDir，或在 scripts\paths.local.bat 中设置 NAPCAT_SHELL_DIR / NAPCAT_QQ_DIR'
}

function Stop-AVHost([string]$RuntimeDir, [string]$QQExe) {
    # 只结束 AV Host：进程号来自启动器输出，且路径是 NapCat 的 QQ、命令行不带 -q（小睦主进程带 -q 账号）。
    $pidFile = Join-Path $RuntimeDir 'av-host.pid'
    $ids = @()
    if (Test-Path -LiteralPath $pidFile) { $ids += [int](Get-Content -LiteralPath $pidFile -Raw).Trim() }
    foreach ($logFile in (Get-ChildItem -LiteralPath $RuntimeDir -Filter 'av-host*.stdout.log' -File -ErrorAction SilentlyContinue)) {
        foreach ($match in (Select-String -LiteralPath $logFile.FullName -Pattern 'Main Process ID:(\d+)')) { $ids += [int]$match.Matches[0].Groups[1].Value }
    }
    foreach ($id in ($ids | Select-Object -Unique)) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
        if ($null -eq $proc) { continue }
        $samePath = $proc.ExecutablePath -and ([IO.Path]::GetFullPath($proc.ExecutablePath) -ieq [IO.Path]::GetFullPath($QQExe))
        if (-not $samePath -or $proc.CommandLine -match '(^|\s)-q(\s|$)') {
            Write-Output "跳过进程 $id：不是 AV Host（$($proc.ExecutablePath) $($proc.CommandLine)）"
            continue
        }
        $launcher = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.ParentProcessId)" -ErrorAction SilentlyContinue
        & taskkill.exe /PID $id /T /F | Out-Null
        # 启动器还占着日志文件；只结束这个 AV Host 自己的启动器。
        if ($launcher -and $launcher.Name -ieq 'NapCatWinBootMain.exe') { & taskkill.exe /PID $launcher.ProcessId /F | Out-Null }
        Write-Output "已结束上一次的 AV Host 进程 $id。"
        Start-Sleep -Seconds 2
    }
    # 清理 AV Host 退出后残留的 QQ 子进程（崩溃收集、渲染进程等），它们会占住日志文件。
    # 只匹配命令行里带本插件专用配置目录 av-host-profile 的进程，不会碰小睦的 QQ。
    $leftovers = Get-CimInstance Win32_Process -Filter "Name = 'QQ.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*av-host-profile*' -and $_.CommandLine -notmatch '(^|\s)-q(\s|$)' }
    foreach ($item in $leftovers) {
        & taskkill.exe /PID $item.ProcessId /T /F | Out-Null
        Write-Output "已清理残留的 AV Host 子进程 $($item.ProcessId)。"
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
}

$bridgeDirectory = [IO.Path]::GetFullPath($PSScriptRoot)
$napDirectory = (Resolve-Path -LiteralPath $NapCatDir).Path
$qqDirectory = (Resolve-Path -LiteralPath $QQDir).Path
$runtimeDirectory = Join-Path $bridgeDirectory 'runtime'
$launchExecutable = Join-Path $napDirectory 'NapCatWinBootMain.exe'
$injectLibrary = Join-Path $napDirectory 'NapCatWinBootHook.dll'
$qqExecutable = Join-Path $qqDirectory 'QQ.exe'
foreach ($requiredFile in @($launchExecutable, $injectLibrary, $qqExecutable)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) { throw "文件不存在：$requiredFile" }
}
# 必须唯一定位 QQ 版本，避免按目录顺序误用与可执行文件不匹配的 AVSDK。
$versionRoot = Join-Path $qqDirectory 'versions'
$avCandidates = @(Get-ChildItem -LiteralPath $versionRoot -Filter AVSDKPlugin.dll -Recurse -File)
if ($avCandidates.Count -ne 1) { throw 'QQ 中检测到零个或多个 AVSDK 版本，请在独立且只有一个版本的 QQ 安装中运行。' }
$avLibrary = $avCandidates[0].FullName
$appDirectory = Split-Path (Split-Path $avLibrary -Parent) -Parent
$packageFile = Join-Path $appDirectory 'package.json'
$qqPackage = Get-Content -LiteralPath $packageFile -Raw | ConvertFrom-Json
if ($BridgePort -eq $HostPort -or $BridgePort -lt 1024 -or $BridgePort -gt 65535 -or $HostPort -lt 1024 -or $HostPort -gt 65535) {
    throw '桥接端口必须不同，且处于 1024 到 65535 之间。'
}
Write-Output "QQ 版本：$($qqPackage.version)；Windows AVSDK：已找到。"
if ($Action -eq 'check') { Write-Output '文件检查通过；尚未验证通话或音频路由。'; exit 0 }

# 恢复 runtime 目录的默认权限（继承上级目录）。早期版本收紧权限时把当前用户也挡在了外面。
if (Test-Path -LiteralPath $runtimeDirectory) {
    & icacls.exe $runtimeDirectory /reset /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Output "恢复权限失败（icacls 退出码 $LASTEXITCODE），尝试取回所有权……"
        & takeown.exe /F $runtimeDirectory /R /D Y | Out-Null
        & icacls.exe $runtimeDirectory /reset /T /C /Q | Out-Null
    }
    try { [void][IO.File]::ReadAllText((Join-Path $runtimeDirectory 'control.token')) } catch {
        throw "仍无法读取 control.token：请右键 bridge\runtime → 属性 → 安全 → 高级，启用继承后重试。"
    }
}
if ($Action -eq 'stop') { Stop-AVHost $runtimeDirectory $qqExecutable; exit 0 }
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$tokenFile = Join-Path $runtimeDirectory 'control.token'
if (-not (Test-Path -LiteralPath $tokenFile)) {
    $randomBytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($randomBytes) } finally { $rng.Dispose() }
    [IO.File]::WriteAllText($tokenFile, [Convert]::ToBase64String($randomBytes))
}

if ($Action -eq 'install') {
    $pluginDirectory = Join-Path $napDirectory 'plugins\napcat-plugin-maibot-qq-voice-call'
    if (Test-Path -LiteralPath $pluginDirectory) { throw "目标插件已存在，请先人工核对，未覆盖：$pluginDirectory" }
    New-Item -ItemType Directory -Path $pluginDirectory | Out-Null
    Copy-Item -LiteralPath (Join-Path $bridgeDirectory 'napcat-plugin\index.mjs'),(Join-Path $bridgeDirectory 'napcat-plugin\package.json') -Destination $pluginDirectory
    $bridgeConfig = @{controlHost='127.0.0.1'; controlPort=$BridgePort; avHost='127.0.0.1'; avPort=$HostPort; tokenFile=$tokenFile}
    [IO.File]::WriteAllText((Join-Path $pluginDirectory 'bridge-config.json'), ($bridgeConfig | ConvertTo-Json))
    Write-Output "已安装独立 NapCat 插件。请重载该插件或重启 NapCat；没有改写 QQ/NapCat 核心文件。"
    Write-Output "MaiBot 的 bridge.token_file 填写：$tokenFile"
    exit 0
}

$qqPackage.main = './loadNapCat.js'
$isolatedPackage = Join-Path $runtimeDirectory 'qq-package.json'
[IO.File]::WriteAllText($isolatedPackage, ($qqPackage | ConvertTo-Json -Depth 10))
$env:NAPCAT_PATCH_PACKAGE = $isolatedPackage
$env:NAPCAT_LOAD_PATH = Join-Path $bridgeDirectory 'av-host\windows-loader.cjs'
$env:MAIBOT_QQ_CALL_BRIDGE_DIR = $bridgeDirectory
$env:MAIBOT_QQ_CALL_QQ_DIR = $qqDirectory
$env:MAIBOT_QQ_CALL_AVSDK_PATH = $avLibrary
$env:MAIBOT_QQ_CALL_BRIDGE_TOKEN_FILE = $tokenFile
$env:MAIBOT_QQ_CALL_BRIDGE_PORT = [string]$BridgePort
$env:MAIBOT_QQ_CALL_AV_HOST_PORT = [string]$HostPort
Remove-Item Env:MAIBOT_QQ_CALL_BRIDGE_TOKEN -ErrorAction SilentlyContinue
$arguments = @('"' + $qqExecutable + '"', '"' + $injectLibrary + '"')
Stop-AVHost $runtimeDirectory $qqExecutable
Remove-Item -LiteralPath (Join-Path $runtimeDirectory 'av-host.log') -ErrorAction SilentlyContinue
# 每次启动用新的日志文件，避免上一次的启动器仍占用旧文件导致启动失败。
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdoutLog = Join-Path $runtimeDirectory "av-host-$stamp.stdout.log"
$stderrLog = Join-Path $runtimeDirectory "av-host-$stamp.stderr.log"
Write-Output "启动 AV Host：$launchExecutable $($arguments -join ' ')"
$process = Start-Process -FilePath $launchExecutable -ArgumentList $arguments -WorkingDirectory $napDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
$qqPid = $null
for ($i = 0; $i -lt 20 -and -not $qqPid; $i++) {
    Start-Sleep -Milliseconds 500
    $found = Select-String -LiteralPath $stdoutLog -Pattern 'Main Process ID:(\d+)' -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($found) { $qqPid = $found.Matches[0].Groups[1].Value }
}
if ($qqPid) { [IO.File]::WriteAllText((Join-Path $runtimeDirectory 'av-host.pid'), $qqPid) }
Write-Output "已启动独立 AV Host，QQ 进程号：$qqPid。是否成功以 bridge-status 输出为准。"
