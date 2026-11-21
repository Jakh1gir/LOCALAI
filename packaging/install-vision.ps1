#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$WinSW,
    [Parameter(Mandatory)][string]$Wheelhouse,
    [Parameter(Mandatory)][string]$Tessdata,
    [Parameter(Mandatory)][uri]$ServerUrl,
    [Parameter(Mandatory)][pscredential]$Worker,
    [string]$CaFile
)
$ErrorActionPreference = 'Stop'
$service = 'LocalAIVision'
$app = Join-Path $env:ProgramFiles 'LOCALAI\Vision'
$data = Join-Path $env:ProgramData 'LOCALAI\Vision'
$source = Join-Path $PSScriptRoot '..\vision'

function Invoke-Checked([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Ошибка выполнения $Command (код $LASTEXITCODE)" }
}

if (Get-Service $service -ErrorAction SilentlyContinue) { throw "Служба $service уже установлена" }
if ((Test-Path $app) -or (Test-Path $data)) { throw "Каталог установки или данных уже существует" }
foreach ($item in @($Python, $WinSW, $Wheelhouse, "$Tessdata\rus.traineddata", "$Tessdata\eng.traineddata")) {
    if (!(Test-Path -LiteralPath $item)) { throw "Не найдено: $item" }
}
$created = @()
$installed = $false
$wrapper = "$app\$service.exe"
try {
    foreach ($dir in @($app, $data)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
        $created += $dir
    }
    # Закрываем каталог до записи пароля. Права службы добавляются по её собственному SID.
    Invoke-Checked icacls @($data, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
    New-Item -ItemType Directory -Path "$data\cache", "$data\logs", "$app\vision", "$app\tessdata" | Out-Null
    Copy-Item "$source\*.py" "$app\vision"
    Copy-Item -LiteralPath $WinSW -Destination $wrapper
    foreach ($model in 'rus','eng') { Copy-Item "$Tessdata\$model.traineddata" "$app\tessdata" }
    Invoke-Checked $Python @('-m','venv',"$app\runtime")
    $exe = "$app\runtime\Scripts\python.exe"
    Invoke-Checked $exe @('-m','pip','install','--no-index','--find-links',$Wheelhouse,'-r',"$source\requirements.txt")
    Push-Location $app
    try {
        Invoke-Checked $exe @('-c','import sys; from pathlib import Path; from vision.reader import Reader; Reader(Path(sys.argv[1]))',"$app\tessdata")
    } finally { Pop-Location }
    $config = @{
        url = $ServerUrl.AbsoluteUri.TrimEnd('/')
        login = $Worker.UserName
        password = $Worker.GetNetworkCredential().Password
        tessdata = "$app\tessdata"
        cache = "$data\cache"
    }
    if ($CaFile) {
        Copy-Item -LiteralPath $CaFile -Destination "$data\ca.pem"
        $config.ca = "$data\ca.pem"
    }
    $config | ConvertTo-Json | Set-Content "$data\worker.json" -Encoding UTF8
    [xml]$xml = Get-Content "$PSScriptRoot\vision-service.xml" -Encoding UTF8 -Raw
    $xml.service.executable = $exe
    $xml.service.workingdirectory = $app
    $xml.service.arguments = "-m vision --config `"$data\worker.json`""
    $xml.service.logpath = "$data\logs"
    $xml.Save("$app\$service.xml")
    Invoke-Checked $wrapper @('install')
    $installed = $true
    Invoke-Checked sc.exe @('sidtype', $service, 'unrestricted')
    $account = "NT SERVICE\$service"
    Invoke-Checked icacls @($data, '/grant', "${account}:(OI)(CI)RX")
    foreach ($dir in 'cache','logs') {
        Invoke-Checked icacls @("$data\$dir", '/grant', "${account}:(OI)(CI)M")
    }
    Start-Service $service
    Get-Service $service
} catch {
    if ($installed) {
        Stop-Service $service -ErrorAction SilentlyContinue
        Invoke-Checked $wrapper @('uninstall')
    }
    foreach ($dir in $created) {
        if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
    }
    throw
}
