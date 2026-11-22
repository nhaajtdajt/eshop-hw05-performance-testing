$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot '.runtime'
$backendDir = Join-Path $runtimeDir 'backend'
$upstreamDir = Join-Path (Split-Path -Parent $projectRoot) 'eshop-sut/backend'
$serverPath = Join-Path $backendDir 'server.js'
$stateFile = Join-Path $runtimeDir 'backend-process.json'
if (-not (Test-Path -LiteralPath (Join-Path $backendDir 'node_modules/sqlite3'))) {
    New-Item -ItemType Directory -Force -Path $backendDir | Out-Null
    foreach ($filename in @('package.json', 'package-lock.json', 'database.js', 'database.sqlite')) {
        Copy-Item -LiteralPath (Join-Path $upstreamDir $filename) -Destination $backendDir
    }
    npm.cmd --prefix $backendDir ci --cache (Join-Path $runtimeDir 'npm-cache') --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
}

if (Test-Path -LiteralPath $stateFile) {
    $previous = Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
    $oldProcess = Get-Process -Id $previous.pid -ErrorAction SilentlyContinue
    if ($oldProcess -and $oldProcess.StartTime.ToUniversalTime().Ticks -eq $previous.start_utc_ticks) {
        Stop-Process -InputObject $oldProcess
        $oldProcess.WaitForExit()
    }
}

$source = Get-Content -Raw -Encoding utf8 -LiteralPath (Join-Path $upstreamDir 'server.js')
$bindCall = 'app.listen(PORT, () => {'
if (-not $source.Contains($bindCall)) { throw 'Unexpected upstream server.listen call.' }
$localSource = $source.Replace($bindCall, 'app.listen(PORT, "127.0.0.1", () => {')
[IO.File]::WriteAllText($serverPath, $localSource, [Text.UTF8Encoding]::new($false))
$stdout = Join-Path $runtimeDir 'backend-stdout.txt'
$stderr = Join-Path $runtimeDir 'backend-stderr.txt'
$backend = Start-Process -FilePath (Get-Command node.exe).Source -ArgumentList 'server.js' `
    -WorkingDirectory $backendDir -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
@{pid = $backend.Id; start_utc_ticks = $backend.StartTime.ToUniversalTime().Ticks} |
    ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8

$ready = $false
$lastFailure = $null
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($backend.HasExited) { throw ('Backend exited. ' + (Get-Content -Raw -LiteralPath $stderr)) }
    try {
        $products = Invoke-RestMethod -Uri 'http://127.0.0.1:3000/api/products' -TimeoutSec 2
        if (@($products).Count -ge 5) { $ready = $true; break }
    } catch {
        $lastFailure = $_.Exception.Message
        if ($_.Exception.InnerException -is [Net.Sockets.SocketException] -and
            $_.Exception.InnerException.SocketErrorCode -eq 'AccessDenied') { break }
        Start-Sleep -Milliseconds 250
    }
}
if (-not $ready) { throw "Local EShop did not become ready at 127.0.0.1:3000. $lastFailure" }
python (Join-Path $PSScriptRoot 'seed.py')
if ($LASTEXITCODE -ne 0) { throw 'Fixture preparation failed.' }
$backend.Id

