<#
.SYNOPSIS
  生成 Windows 安装包（Inno Setup 6）。
.DESCRIPTION
  前置：
    1. 先运行  python packaging\build.py   生成 dist\BackupStudio\
    2. 安装 Inno Setup 6：winget install --id JRSoftware.InnoSetup -e
  产物：dist\BackupStudio-Setup-<version>.exe
#>
param(
  [string]$ISCC = "",
  [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $ISCC) {
  $candidates = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:LocalAppData 'Programs\Inno Setup 6\ISCC.exe')
  )
  foreach ($c in $candidates) {
    if ($c -and (Test-Path $c)) { $ISCC = $c; break }
  }
}

if (-not $SkipBuild) {
  python packaging\build.py
}

if (-not $ISCC -or -not (Test-Path $ISCC)) {
  throw 'ISCC.exe not found. Install Inno Setup 6 first: winget install --id JRSoftware.InnoSetup -e'
}

$py = $env:PYTHON
if (-not $py) { $py = 'python' }
$version = (& $py -c "import sys; sys.path.insert(0,'.'); import backupstudio; print(backupstudio.__version__)").Trim()
$escRoot = $root.Replace('\', '\\')
$iss = Join-Path $env:TEMP ("backupstudio-$version.iss")

$templatePath = Join-Path $PSScriptRoot 'installer.iss.in'
$template = [System.IO.File]::ReadAllText($templatePath, [System.Text.Encoding]::UTF8)
$content = $template.Replace('__VERSION__', $version).Replace('__ROOT__', $escRoot)
[System.IO.File]::WriteAllText($iss, $content, (New-Object System.Text.UTF8Encoding $false))

& $ISCC $iss
if ($LASTEXITCODE -ne 0) {
  throw ('Inno Setup failed with exit code ' + $LASTEXITCODE)
}
Write-Host ('==> Installer created: ' + $root + '\dist\BackupStudio-Setup-' + $version + '.exe')
