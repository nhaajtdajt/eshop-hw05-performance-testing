param(
    [string] $JMeterHome,
    [string] $JavaHome
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runScenario = Join-Path $PSScriptRoot 'Run-Scenario.ps1'
$analysis = Join-Path $PSScriptRoot 'analyze.py'
$settings = @{ ShowProgress = $true }
if ($JMeterHome) { $settings.JMeterHome = $JMeterHome }
if ($JavaHome) { $settings.JavaHome = $JavaHome }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.runtime/23120231_Soak_20261121.jmx'))) {
    python (Join-Path $PSScriptRoot 'make_plans.py') --date 20261121
    if ($LASTEXITCODE -ne 0) { throw 'Plan generation failed.' }
}

function Run-IfNeeded($Scenario, $Name, [string[]] $Properties = @()) {
    $folder = Join-Path $projectRoot "results/$Name"
    $jtl = Join-Path $folder "$Scenario.jtl"
    if (Test-Path -LiteralPath (Join-Path $folder 'run.json')) {
        $saved = Get-Content -Raw -LiteralPath (Join-Path $folder 'run.json') | ConvertFrom-Json
        $planDir = if ($Scenario -in @('Smoke', 'Soak')) { '.runtime' } else { 'plans' }
        $plan = Join-Path $projectRoot "$planDir/23120231_${Scenario}_20261121.jmx"
        $hash = (Get-FileHash -LiteralPath $plan).Hash
        if ($saved.plan_sha256 -eq $hash -and
            (@($saved.load_properties) -join ',') -eq ($Properties -join ',') -and
            (Test-Path -LiteralPath (Join-Path $folder 'html/index.html')) -and
            (Test-Path -LiteralPath (Join-Path $folder 'backend-resources.csv')) -and
            (Test-Path -LiteralPath $jtl) -and @(Get-Content -LiteralPath $jtl -TotalCount 2).Count -gt 1) {
            Write-Host "Using complete existing run $Name."
            return
        }
    }
    if (Test-Path -LiteralPath $folder) {
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
        Rename-Item -LiteralPath $folder -NewName "$Name-previous-$stamp"
    }
    & $runScenario -Scenario $Scenario -RunName $Name -LoadProperties $Properties @settings
}

Run-IfNeeded Smoke Smoke-Verified
$smokePath = Join-Path $projectRoot 'results/Smoke-Verified/Smoke.jtl'
$smoke = @(Import-Csv -LiteralPath $smokePath)
if ($smoke.Count -ne 10 -or @($smoke | Where-Object success -NE 'true').Count -ne 0 -or
    @($smoke | Where-Object label -EQ 'flow_03').Count -ne 1) {
    throw "Expected nine successful HTTP samples plus one workflow sample. Inspect $smokePath."
}

foreach ($scenario in @('Load', 'Stress', 'Spike')) {
    Run-IfNeeded $scenario $scenario
}
$candidate = python $analysis --candidate
if ($LASTEXITCODE -ne 0 -or "$candidate" -notmatch '^\d+$') { throw 'Stress analysis failed.' }
$users = if ([int] $candidate -gt 0) { [int] $candidate } else { 8 }
Write-Host "Soak will run at $users virtual users for 630 seconds."
Run-IfNeeded Soak Soak @("endurance_users=$users", 'endurance_seconds=630')
python $analysis
if ($LASTEXITCODE -ne 0) { throw 'Result analysis failed.' }
