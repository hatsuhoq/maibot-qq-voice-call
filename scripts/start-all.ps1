# 一键启动 QQ 语音通话所需的全部组件；已经在运行的会跳过。
# 顺序：Studio One 路由工程 → 语音识别/合成服务 → NapCat（含通话桥）→ 通话组件 AV Host → 小睦（MaiBot）。
param(
    # 默认取 scripts\paths.local.bat 设置的环境变量；也可直接传参。
    [string]$NapCatBat = $env:NAPCAT_BAT,
    [string]$MaiBotDir = $env:MAIBOT_DIR,
    [string]$StudioOneSong = $env:STUDIO_ONE_SONG   # 工程文件或所在文件夹；留空则自动在“文档\Studio One\Songs”里找名字含 通话/路由 的工程
)
$ErrorActionPreference = 'Continue'
if (-not $NapCatBat -or -not $MaiBotDir) {
    Write-Host '★请在 scripts\paths.local.bat 中设置 NAPCAT_BAT 和 MAIBOT_DIR（参考 paths.example.bat）'
    exit 1
}
$napCatRoot = Split-Path $NapCatBat -Parent
$scriptsDir = $PSScriptRoot
$pluginDir = Split-Path $scriptsDir -Parent
$bridgeDir = Join-Path $pluginDir 'bridge'
$results = [ordered]@{}

function Test-Port([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}
function Test-Http([string]$Url) {
    try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri $Url).StatusCode -eq 200 } catch { return $false }
}
function Wait-Until([scriptblock]$Check, [int]$Seconds, [string]$What) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Check) { return $true }
        Start-Sleep -Seconds 2
    }
    Write-Host "  ★等待 $What 超时（$Seconds 秒）"
    return $false
}

Write-Host '== 1/5 Studio One 路由工程 =='
if (Get-Process -Name 'Studio One' -ErrorAction SilentlyContinue) {
    Write-Host '  Studio One 已在运行（请确认打开的是通话路由工程）。'
    $results['Studio One'] = '已在运行'
} else {
    $song = $StudioOneSong
    if (-not $song) {
        $songsRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Studio One\Songs'
        $song = Get-ChildItem -LiteralPath $songsRoot -Filter '*.song' -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '通话|路由|call|route' } |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if ($song -and (Test-Path -LiteralPath $song)) {
        Start-Process -FilePath $song
        Write-Host "  已打开：$song"
        $results['Studio One'] = '已启动'
    } else {
        Write-Host '  ★没找到路由工程。请手动打开，或在 scripts\paths.local.bat 里填写 STUDIO_ONE_SONG。'
        $results['Studio One'] = '★需手动打开'
    }
}

Write-Host '== 2/5 语音识别与合成服务 =='
& cmd.exe /c (Join-Path $scriptsDir 'ensure-services.bat')
$voiceOk = Wait-Until { (Test-Http 'http://127.0.0.1:8091/health') -and (Test-Http 'http://127.0.0.1:9880/docs') } 180 '语音服务就绪'
$results['语音服务'] = $(if ($voiceOk) { '就绪' } else { '★未就绪，看服务窗口报错' })

Write-Host '== 3/5 NapCat 与通话桥 =='
if (Test-Port 6110) {
    Write-Host '  NapCat 通话桥已在运行。'
    $results['NapCat'] = '已在运行'
} else {
    $napcatQQ = Get-CimInstance Win32_Process -Filter "Name = 'QQ.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -like "$napCatRoot\*" -and $_.CommandLine -match '(^|\s)-q(\s|$)' }
    if ($napcatQQ) {
        Write-Host '  ★NapCat 在运行但通话桥（6110）没起来：请关闭 NapCat 窗口后重新运行本脚本。'
        $results['NapCat'] = '★通话桥未加载'
    } else {
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$NapCatBat`"" -WorkingDirectory (Split-Path $NapCatBat -Parent)
        Write-Host '  已启动 NapCat，等待登录……'
        $napOk = Wait-Until { Test-Port 6110 } 120 'NapCat 通话桥'
        $results['NapCat'] = $(if ($napOk) { '已启动' } else { '★启动超时，看 NapCat 窗口' })
    }
}

Write-Host '== 4/5 通话组件 AV Host =='
if (Test-Port 6111) {
    Write-Host '  通话组件已在运行。'
    $results['通话组件'] = '已在运行'
} elseif (Test-Port 6110) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $bridgeDir 'windows.ps1') -Action start
    $avOk = Wait-Until { Test-Port 6111 } 40 '通话组件'
    $results['通话组件'] = $(if ($avOk) { '已启动' } else { '★启动失败，看 bridge\runtime\av-host.log' })
} else {
    Write-Host '  跳过：NapCat 通话桥还没起来。'
    $results['通话组件'] = '★跳过（NapCat 未就绪）'
}

Write-Host '== 5/5 小睦（MaiBot） =='
$maibot = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'bot\.py' -and $_.ExecutablePath -like "$MaiBotDir\*" }
if ($maibot) {
    Write-Host '  小睦已在运行（插件每 15 秒会自动重试等待来电，不用重启）。'
    $results['小睦'] = '已在运行'
} else {
    $maiBat = Join-Path $MaiBotDir '启动MaiBot.bat'
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$maiBat`"" -WorkingDirectory $MaiBotDir
    Write-Host '  已启动小睦。'
    $results['小睦'] = '已启动'
}

Write-Host ''
Write-Host '== 通话桥登录状态 =='
Start-Sleep -Seconds 5
try {
    $token = (Get-Content -LiteralPath (Join-Path $bridgeDir 'runtime\control.token') -Raw).Trim()
    $status = Invoke-RestMethod -TimeoutSec 3 -Uri 'http://127.0.0.1:6110/v1/status' -Headers @{ Authorization = "Bearer $token" }
    $login = $status.data.avHost.loginPosted
    $results['通话组件登录'] = $(if ($login) { '已登录' } else { "★未登录：$($status.data.avHost.lastError)" })
} catch {
    $results['通话组件登录'] = '★查询失败（NapCat 通话桥未运行）'
}

Write-Host ''
Write-Host '================ 汇总 ================'
foreach ($key in $results.Keys) { Write-Host ("  {0,-8} {1}" -f $key, $results[$key]) }
if ($results.Values -match '★') {
    Write-Host '有项目需要处理，见上面带 ★ 的行。'
} else {
    Write-Host '全部就绪。小睦启动完成后（约半分钟）就可以打电话了。'
}
