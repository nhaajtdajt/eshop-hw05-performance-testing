param(
    [Parameter(Mandatory)] [ValidateSet('Smoke', 'Load', 'Stress', 'Spike', 'Soak')] [string] $Scenario,
    [string] $RunName = $Scenario,
    [string] $Date = '20261121',
    [string] $JMeterHome,
    [string] $JavaHome,
    [string[]] $LoadProperties = @(),
    [switch] $ShowProgress
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ($RunName -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Choose a simple run name.' }
if ($Date -notmatch '^\d{8}$') { throw 'Date must use YYYYMMDD.' }
if (-not $JMeterHome) { $JMeterHome = Join-Path $projectRoot '.runtime/apache-jmeter-5.6.3' }
$jmeterJar = Join-Path $JMeterHome 'bin/ApacheJMeter.jar'
if (-not (Test-Path -LiteralPath $jmeterJar)) { throw 'Set -JMeterHome to an Apache JMeter 5.6.3 installation.' }
if (-not $JavaHome) {
    $localJdk = Get-ChildItem -LiteralPath (Join-Path $projectRoot '.runtime/jdk21') -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin/javac.exe') } | Select-Object -First 1
    $JavaHome = if ($localJdk) { $localJdk.FullName } else { $env:JAVA_HOME }
}
if (-not $JavaHome) { throw 'Set -JavaHome or JAVA_HOME to a JDK 17 or 21 installation.' }
$java = (Resolve-Path -LiteralPath (Join-Path $JavaHome 'bin/java.exe')).Path
$javac = (Resolve-Path -LiteralPath (Join-Path $JavaHome 'bin/javac.exe')).Path
$javaVersionOutput = @(& $java --version)
$javaVersion = $javaVersionOutput[0]
if ($LASTEXITCODE -ne 0 -or $javaVersion -notmatch '\b(17|21)(\.|\s)') {
    throw "JMeter 5.6.3 with bundled Groovy requires a compatible JDK. Use 17 or 21. Found: $javaVersion"
}
$planFolder = if ($Scenario -in @('Smoke', 'Soak')) { '.runtime' } else { 'plans' }
$planFile = Join-Path $projectRoot "$planFolder/23120231_${Scenario}_$Date.jmx"
if (-not (Test-Path -LiteralPath $planFile)) { throw 'Generate the test plans first.' }
$resultFolder = Join-Path $projectRoot "results/$RunName"
if (Test-Path -LiteralPath $resultFolder) { throw 'A run with that name already exists. Choose a new -RunName.' }

$backendPid = (& (Join-Path $PSScriptRoot 'Restart-Backend.ps1') | Select-Object -Last 1)
$backend = Get-Process -Id $backendPid
New-Item -ItemType Directory -Path $resultFolder -Force | Out-Null
$rawLog = Join-Path $resultFolder "$Scenario.jtl"
$jmeterLog = Join-Path $resultFolder 'jmeter.log'
$dashboard = Join-Path $resultFolder 'html'
$stdout = Join-Path $resultFolder 'jmeter-stdout.txt'
$stderr = Join-Path $resultFolder 'jmeter-stderr.txt'
$monitorPath = Join-Path $resultFolder 'backend-resources.csv'

$classFolder = Join-Path $projectRoot '.runtime/jmeter-classes-java17'
New-Item -ItemType Directory -Path $classFolder -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $classFolder 'LocalPreferences.class'))) {
    & $javac --release 17 -d $classFolder (Join-Path $PSScriptRoot 'LocalPreferences.java')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to compile JMeter headless preferences adapter.' }
}
$classPath = '"' + $jmeterJar + ';' + $classFolder + '"'
$argsForJava = @('-Xms256m', '-Xmx768m', '-Djava.awt.headless=true',
    '-Djava.util.prefs.PreferencesFactory=LocalPreferences', '-cp', $classPath, 'org.apache.jmeter.NewDriver',
    '-n', '-t', ('"' + $planFile + '"'), '-l', ('"' + $rawLog + '"'), '-j', ('"' + $jmeterLog + '"'),
    '-e', '-o', ('"' + $dashboard + '"'), '-Jjmeter.save.saveservice.output_format=csv',
    '-Jjmeter.save.saveservice.print_field_names=true', '-Jsummariser.interval=5',
    '-Jjmeter.reportgenerator.overall_granularity=1000')
foreach ($property in $LoadProperties) {
    if ($property -notmatch '^[a-zA-Z_]+=[0-9]+$') { throw "Invalid numeric JMeter property: $property" }
    $argsForJava += ('-J' + $property)
}

$clock = [Diagnostics.Stopwatch]::StartNew()
$lastCpu = $backend.TotalProcessorTime.TotalSeconds
$lastTime = $clock.Elapsed.TotalSeconds
$runner = Start-Process -FilePath $java -ArgumentList $argsForJava `
    -WorkingDirectory (Split-Path -Parent $jmeterJar) -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$null = $runner.Handle
@{scenario=$Scenario; run=$RunName; jmeter_pid=$runner.Id; backend_pid=$backend.Id;
    start_utc=[DateTime]::UtcNow.ToString('o'); java=$javaVersion; load_properties=$LoadProperties;
    plan_sha256=(Get-FileHash -LiteralPath $planFile).Hash} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $resultFolder 'run.json') -Encoding utf8
Write-Output "JMeter $Scenario started. Java PID $($runner.Id), backend PID $backendPid."

$nextDisplay = 0
try {
    $writer = [IO.StreamWriter]::new($monitorPath, $false, [Text.UTF8Encoding]::new($false))
    $writer.AutoFlush = $true
    $writer.WriteLine('utc,elapsed_s,backend_pid,cpu_percent,working_set_mib,private_mib')
    while (-not $runner.HasExited) {
        $backend.Refresh()
        if ($backend.HasExited) { throw 'The backend exited during the test.' }
        $now = $clock.Elapsed.TotalSeconds
        $cpu = $backend.TotalProcessorTime.TotalSeconds
        $cpuPercent = 100 * ($cpu - $lastCpu) / ([Math]::Max(0.01, $now - $lastTime) * [Environment]::ProcessorCount)
        $row = [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:o},{1:F3},{2},{3:F2},{4:F2},{5:F2}',
            [DateTime]::UtcNow, $now, $backend.Id, $cpuPercent, $backend.WorkingSet64 / 1MB, $backend.PrivateMemorySize64 / 1MB)
        $writer.WriteLine($row)
        if ($ShowProgress -and $now -ge $nextDisplay) {
            $summary = Get-Content -LiteralPath $stdout -Tail 16 | Where-Object { $_ -match '^summary [+=]' } |
                Select-Object -Last 1
            if ($summary) { Write-Host $summary }
            Write-Host ('{0,-6} +{1,5:F1}s  node.exe PID {2}  CPU {3,5:F1}%  WS {4,7:F1} MiB  Private {5,7:F1} MiB' -f
                $Scenario, $now, $backend.Id, $cpuPercent, ($backend.WorkingSet64 / 1MB), ($backend.PrivateMemorySize64 / 1MB))
            $nextDisplay = $now + 5
        }
        $lastCpu = $cpu
        $lastTime = $now
        Start-Sleep -Milliseconds 1000
    }
} finally {
    if ($writer) { $writer.Dispose() }
}

$runner.WaitForExit()
$exitCode = $runner.ExitCode
if (($null -ne $exitCode -and $exitCode -ne 0) -or -not (Test-Path -LiteralPath $rawLog) -or
    -not (Test-Path -LiteralPath (Join-Path $dashboard 'index.html')) -or
    @(Get-Content -LiteralPath $rawLog -TotalCount 2).Count -lt 2) {
    throw "JMeter did not produce the required results (exit code '$exitCode'). Inspect $resultFolder."
}
Get-Content -LiteralPath $stdout | Select-Object -Last 12
Write-Output "Run recorded in $resultFolder"
