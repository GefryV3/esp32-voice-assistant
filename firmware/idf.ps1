# Обёртка над idf.py: подставляет окружение ESP-IDF 5.5.5 и запускает команду.
#   .\idf.ps1 build
#   .\idf.ps1 -p COM5 flash monitor
# ESP-IDF поставлен ESPHome'ом в C:\Users\kurli\.local\idf; C:\idf — короткий junction на него
# (длинные пути упираются в MAX_PATH). Запускать из PowerShell/cmd: из Git Bash тулчейн ломается.
# Аргументы берём из $args как есть: блок param() с [Parameter()] делает скрипт «продвинутым»,
# и PowerShell сам съедает ключи вроде -p (порт так и не доходил до idf.py).
$IdfArgs = $args
# Не 'Stop': PowerShell 5.1 считает любой вывод программы в stderr ошибкой, а ESP-IDF
# пишет туда безобидные предупреждения.
$ErrorActionPreference = 'Continue'

$root = if (Test-Path 'C:\idf\frameworks') { 'C:\idf' } else { "$env:USERPROFILE\.local\idf" }
$env:IDF_PATH = "$root\frameworks\5.5.5"
$env:IDF_TOOLS_PATH = $root
$env:IDF_PYTHON_ENV_PATH = "$root\penvs\5.5.5"
$py = "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe"

foreach ($line in & $py "$env:IDF_PATH\tools\idf_tools.py" export --format key-value 2>$null) {
    $key, $value = $line -split '=', 2
    if (-not $value) { continue }
    if ($key -eq 'PATH') { $value = $value.Replace('%PATH%', $env:PATH) }
    Set-Item "env:$key" $value
}
# ccache вместе с песочницей AppData уже ломал сборку — без него надёжнее.
$env:IDF_CCACHE_ENABLE = '0'

Push-Location $PSScriptRoot
try {
    & $py "$env:IDF_PATH\tools\idf.py" @IdfArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
