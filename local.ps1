param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.local'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pgCtlPath = Join-Path $runtimeRoot 'pgsql\bin\pg_ctl.exe'
$pgDataPath = Join-Path $runtimeRoot 'pgdata'
$memuraiPath = Join-Path $runtimeRoot 'memurai\Memurai\memurai.exe'
$env:DJANGO_SETTINGS_MODULE = 'config.settings_local'
$env:PYTHONUTF8 = '1'
$env:TIKTOKEN_CACHE_DIR = Join-Path $runtimeRoot 'tiktoken'

function Get-LocalProcess([string]$Name) {
    $pidPath = Join-Path $runtimeRoot "$Name.pid"
    if (Test-Path -LiteralPath $pidPath) {
        $processId = [int](Get-Content -LiteralPath $pidPath)
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $processId"
        if ($candidate -and $candidate.ExecutablePath -and
            $candidate.ExecutablePath.StartsWith($projectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            return $candidate
        }
    }
}

function Start-LocalProcess([string]$Name, [string]$Executable, [string[]]$Arguments) {
    if (Get-LocalProcess $Name) {
        Write-Host "$Name is already running."
        return
    }
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtimeRoot "$Name.out.log") `
        -RedirectStandardError (Join-Path $runtimeRoot "$Name.err.log")
    Set-Content -LiteralPath (Join-Path $runtimeRoot "$Name.pid") -Value $process.Id
}

function Stop-LocalProcessTree([int]$ProcessId) {
    # Python's Windows venv launcher starts a child interpreter.
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId")
    foreach ($child in $children) { Stop-LocalProcessTree $child.ProcessId }
    Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue
}

Push-Location $projectRoot
try {
    if ($Action -eq 'stop') {
        foreach ($name in @('web', 'beat', 'worker')) {
            $running = Get-LocalProcess $name
            if ($running) { Stop-LocalProcessTree $running.ProcessId }
        }
        if (Get-LocalProcess 'redis') {
            & (Join-Path $runtimeRoot 'memurai\Memurai\memurai-cli.exe') -h 127.0.0.1 -p 56379 shutdown
            if ($LASTEXITCODE -ne 0) { throw 'Redis could not be stopped.' }
        }
        & $pgCtlPath -D $pgDataPath status *> $null
        if ($LASTEXITCODE -eq 0) {
            & $pgCtlPath -D $pgDataPath stop -m fast
            if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL could not be stopped.' }
        }
        Write-Host 'Local ERP stopped. Database files are preserved.'
        return
    }

    if ($Action -eq 'status') {
        & $pgCtlPath -D $pgDataPath status
        foreach ($name in @('redis', 'worker', 'beat', 'web')) {
            $running = Get-LocalProcess $name
            Write-Host "$name running: $([bool]$running)"
        }
        return
    }

    & $pgCtlPath -D $pgDataPath status *> $null
    if ($LASTEXITCODE -ne 0) {
        & $pgCtlPath -D $pgDataPath -l (Join-Path $runtimeRoot 'postgres.log') start -w
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL could not be started.' }
    }
    Start-LocalProcess 'redis' $memuraiPath @('".local\memurai.conf"')
    $redisReady = $false
    $redisCliPath = Join-Path $runtimeRoot 'memurai\Memurai\memurai-cli.exe'
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $pong = & $redisCliPath -h 127.0.0.1 -p 56379 ping 2>$null
        if ($LASTEXITCODE -eq 0 -and $pong -eq 'PONG') { $redisReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $redisReady) { throw 'Redis could not be started. Check .local logs.' }
    & $pythonPath manage.py migrate --noinput
    if ($LASTEXITCODE -ne 0) { throw 'Database migrations failed.' }
    Start-LocalProcess 'worker' $pythonPath @('-m', 'celery', '-A', 'config', 'worker', '--pool=solo', '--loglevel=INFO', '--hostname=erp-local@%h')
    Start-LocalProcess 'beat' $pythonPath @('-m', 'celery', '-A', 'config', 'beat', '--loglevel=INFO', '--pidfile=')
    Start-LocalProcess 'web' $pythonPath @('manage.py', 'runserver', '0.0.0.0:8000', '--noreload')
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/healthz/' -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'Web server did not become ready. Check .local/web.err.log.' }
    foreach ($name in @('redis', 'worker', 'beat', 'web')) {
        if (-not (Get-LocalProcess $name)) { throw "$name exited. Check .local/$name.err.log." }
    }
    Write-Host 'ERP is ready: http://127.0.0.1:8000/'
} finally {
    Pop-Location
}
