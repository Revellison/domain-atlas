param(
    [string]$InputFile = "build/input/domains.csv",
    [string]$OutputFile = "domains_report.csv",
    [switch]$SkipExcel,
    [switch]$RetryUnresolved
)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $reportArgs = @('-X', 'utf8', "$PSScriptRoot/domain_report.py", $InputFile, '--output', $OutputFile)
    if ($RetryUnresolved) { $reportArgs += '--retry-unresolved' }
    python @reportArgs
    if ($LASTEXITCODE -ne 0) { throw "Обработка доменов завершилась с кодом $LASTEXITCODE" }
    if (-not $SkipExcel) {
        $runtime = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
        $bundledNode = Join-Path $runtime 'bin/node.exe'
        $modules = Join-Path $runtime 'node_modules'
        if ((Test-Path -LiteralPath $bundledNode) -and (Test-Path -LiteralPath (Join-Path $modules '@oai/artifact-tool'))) {
            $junction = Join-Path $PSScriptRoot 'node_modules'
            if (-not (Test-Path -LiteralPath $junction)) {
                New-Item -ItemType Junction -Path $junction -Value $modules | Out-Null
            }
            $details = [System.IO.Path]::ChangeExtension($OutputFile, '.details.json')
            $excel = [System.IO.Path]::ChangeExtension($OutputFile, '.xlsx')
            & $bundledNode "$PSScriptRoot/export_xlsx.mjs" $details $excel
            if ($LASTEXITCODE -ne 0) { throw "CSV создан, но экспорт в Excel завершился с ошибкой" }
        } else {
            Write-Host 'CSV готов. Для экспорта XLSX нужен установленный runtime Codex с @oai/artifact-tool.'
        }
    }
} finally {
    Pop-Location
}
