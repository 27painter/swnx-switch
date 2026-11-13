# swnx-switch 交互菜单 —— 双击 启动器 用，不需要记命令。
# 也可以带参数批处理调用：
#   powershell -File menu.ps1 -Action probe
#   powershell -File menu.ps1 -Action convert -Source "a.sldprt" -Target "a.prt"
param(
    [ValidateSet('menu', 'probe', 'read', 'convert', 'compare', 'brief')]
    [string]$Action = 'menu',
    [string]$Source = '',
    [string]$Target = ''
)

$ErrorActionPreference = 'Continue'
$utf8 = New-Object System.Text.UTF8Encoding($false)
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

$repo = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = (Join-Path $repo 'src') + ';' + $env:PYTHONPATH
$outDir = Join-Path $repo 'out'
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }

$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue) }
if (-not $python) {
    Write-Host '找不到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH。' -ForegroundColor Red
    exit 2
}
$pythonExe = $python.Source

$CAD_FILTER = 'CAD 零件 (*.sldprt;*.prt;*.step;*.stp;*.x_t)|*.sldprt;*.prt;*.step;*.stp;*.x_t|IR JSON (*.json)|*.json|所有文件 (*.*)|*.*'
$PART_FILTER = 'CAD 零件 (*.sldprt;*.prt)|*.sldprt;*.prt|所有文件 (*.*)|*.*'

function Invoke-Cli {
    param([string[]]$CliArgs)
    Write-Host ''
    Write-Host ('> ' + ($CliArgs -join ' ')) -ForegroundColor DarkGray
    & $pythonExe -m swnx_switch.cli @CliArgs | Out-Host
    $code = $LASTEXITCODE
    Write-Host ''
    if ($code -eq 0) { Write-Host "完成 (exit $code)" -ForegroundColor Green }
    else { Write-Host "失败 (exit $code)，日志和中间文件在 $repo\work" -ForegroundColor Yellow }
    return $code
}

function Pick-OpenFile {
    param([string]$Title, [string]$Filter)
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = $Title
    $dialog.Filter = $Filter
    $dialog.InitialDirectory = $repo
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $dialog.FileName }
    return ''
}

function Pick-SaveFile {
    param([string]$Title, [string]$Filter, [string]$FileName)
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    $dialog = New-Object System.Windows.Forms.SaveFileDialog
    $dialog.Title = $Title
    $dialog.Filter = $Filter
    $dialog.FileName = $FileName
    $dialog.InitialDirectory = $repo
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $dialog.FileName }
    return ''
}

function Step-Probe {
    $null = Invoke-Cli @('probe')
}

function Step-Read {
    $file = $Source
    if (-not $file) { $file = Pick-OpenFile '选择要读取的零件' $CAD_FILTER }
    if (-not $file) { Write-Host '已取消。'; return }
    $name = [System.IO.Path]::GetFileNameWithoutExtension($file)
    $ir = Join-Path $outDir "$name.ir.json"
    $code = Invoke-Cli @('read', $file, '--out', $ir)
    if ($code -eq 0) {
        $null = Invoke-Cli @('brief', $ir)
    }
}

function Step-Convert {
    $src = $Source
    if (-not $src) { $src = Pick-OpenFile '选择源零件' $CAD_FILTER }
    if (-not $src) { Write-Host '已取消。'; return }
    $ext = [System.IO.Path]::GetExtension($src).ToLower()
    $defaultExt = '.prt'
    if ($ext -eq '.sldprt') { $defaultExt = '.prt' }
    elseif ($ext -eq '.prt') { $defaultExt = '.SLDPRT' }
    $defaultName = [System.IO.Path]::GetFileNameWithoutExtension($src) + $defaultExt
    $dst = $Target
    if (-not $dst) {
        $dst = Pick-SaveFile '选择转换结果保存位置' '目标格式 (*.prt;*.SLDPRT;*.step;*.x_t)|*.prt;*.SLDPRT;*.step;*.x_t|所有文件 (*.*)|*.*' $defaultName
    }
    if (-not $dst) { Write-Host '已取消。'; return }
    $args = @('convert', $src, $dst)
    if (Test-Path $dst) {
        $answer = Read-Host "目标已存在，覆盖吗？[y/N]"
        if ($answer -match '^(y|Y|yes|YES)$') { $args += '--force' }
        else { Write-Host '已取消。'; return }
    }
    $null = Invoke-Cli $args
}

function Step-Compare {
    $a = Pick-OpenFile '选择参照件（源）' $CAD_FILTER
    if (-not $a) { Write-Host '已取消。'; return }
    $b = Pick-OpenFile '选择待比较件（转换结果）' $CAD_FILTER
    if (-not $b) { Write-Host '已取消。'; return }
    $null = Invoke-Cli @('compare', $a, $b)
}

function Step-Brief {
    $ir = Pick-OpenFile '选择 IR JSON（read 的产物）' 'IR JSON (*.json)|*.json|所有文件 (*.*)|*.*'
    if (-not $ir) { Write-Host '已取消。'; return }
    $null = Invoke-Cli @('brief', $ir)
}

function Clear-Screen {
    try { Clear-Host } catch { }
}

function Show-Menu {
    Write-Host ''
    Write-Host '======== swnx-switch · SolidWorks ⇄ NX 零件互通 ========' -ForegroundColor Cyan
    Write-Host '  1  环境探测（先确认 NX / SolidWorks 可用）'
    Write-Host '  2  读取零件 → 生成 IR + 零件简报'
    Write-Host '  3  转换零件（SolidWorks ⇄ NX / STEP / Parasolid）'
    Write-Host '  4  对比两个零件（判断是不是同一个零件 / 转换是否一致）'
    Write-Host '  5  查看已有零件的简报（IR JSON）'
    Write-Host '  6  打开输出目录 out'
    Write-Host '  0  退出'
    Write-Host '=======================================================' -ForegroundColor Cyan
}

function Run-Action {
    param([string]$Name)
    switch ($Name) {
        'probe' { Step-Probe }
        'read' { Step-Read }
        'convert' { Step-Convert }
        'compare' { Step-Compare }
        'brief' { Step-Brief }
    }
}

if ($Action -ne 'menu') {
    Run-Action $Action
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    exit 0
}

while ($true) {
    Clear-Screen
    Write-Host ('项目: ' + $repo)
    Write-Host ('Python: ' + $pythonExe) -ForegroundColor DarkGray
    Show-Menu
    $choice = Read-Host '请选择'
    switch ($choice) {
        '1' { Step-Probe; Read-Host '按回车返回菜单' | Out-Null }
        '2' { Step-Read; Read-Host '按回车返回菜单' | Out-Null }
        '3' { Step-Convert; Read-Host '按回车返回菜单' | Out-Null }
        '4' { Step-Compare; Read-Host '按回车返回菜单' | Out-Null }
        '5' { Step-Brief; Read-Host '按回车返回菜单' | Out-Null }
        '6' { Start-Process explorer.exe $outDir; Start-Sleep -Milliseconds 300 }
        '0' { return }
        default { Write-Host '输入 0-6。' -ForegroundColor Yellow; Start-Sleep -Milliseconds 700 }
    }
}
