<#
.SYNOPSIS
    Start the instrument-side half of the production stack on ONE (Windows) machine.

.DESCRIPTION
    Starts the two instrument nodes:

        pascal   the PLD chamber -- growth log, PLDconfig.ini, MI mode, chamber webcam
        rheed    RHEED camera acquisition (Basler/Pylon)

    The server-side half (monitor, storage, detection, api) runs on the other
    machine -- scripts/start_server_host.sh. Start that one FIRST: its storage node
    declares the exchanges these two publish into.

    The broker is NOT on this machine, so a broker host is required: pass
    -BrokerHost, or set [rabbitmq] host in cfg/.secrets.toml. A loopback value
    (the tracked settings.toml default) counts as "unset" here.

    This is the PowerShell port of scripts/start_instrument_host.sh. On Windows there
    is no SIGTERM, so Ctrl-C (and a node dying) stops the children with a hard kill
    rather than the graceful drain the bash version gets.

.PARAMETER BrokerHost
    Broker host -- it lives on the server machine. Alias: -Host. If omitted, falls
    back to settings.rabbitmq.host (cfg/settings.toml, overridden by
    cfg/.secrets.toml); a loopback value there is rejected.

.PARAMETER User
    Broker user. RabbitMQ refuses 'guest' off loopback, so a real account is
    required here; see scripts/apply_broker_permissions.py.

.PARAMETER Password
    Broker password. Alias: -p.

.PARAMETER Log
    Folder PASCAL writes the chamber growth log into (required for -Src path).

.PARAMETER Mi
    The real MI mode folder (required for -Src path).

.PARAMETER PldConfig
    PLDconfig.ini (default: the path in cfg/settings.toml).

.PARAMETER Src
    Chamber source: path | sim | test. 'path' is the real chamber and the default.

.PARAMETER RheedSrc
    RHEED camera source: pylon | webcam | simcam (default pylon).

.PARAMETER Speed
    Simulated-time multiplier, -Src sim only.

.PARAMETER NoRheed
    Do not start the rheed node.

.PARAMETER NoPascal
    Do not start the pascal node.

.PARAMETER Check
    Run the preflight checks and exit without starting anything.

.EXAMPLE
    scripts\start_instrument_host.ps1 -BrokerHost <broker-ip> -User lumi-node -Password pw `
        -Log 'D:\PASCAL\logs\growth.csv' -Mi 'D:\PASCAL\MI'
#>

[CmdletBinding()]
param(
    [Alias('Host')]
    [string]$BrokerHost,

    [string]$User = 'guest',

    [Alias('p')]
    [string]$Password = 'guest',

    [string]$Log,

    [string]$Mi,

    [string]$PldConfig,

    [ValidateSet('path', 'sim', 'test')]
    [string]$Src = 'path',

    [ValidateSet('pylon', 'webcam', 'simcam')]
    [string]$RheedSrc = 'pylon',

    [string]$Speed = '1',

    [switch]$NoRheed,

    [switch]$NoPascal,

    [switch]$Check
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# PS 7.4+ turns non-zero native exit codes into terminating errors; the preflight
# leans on running `python -c import ...` and reading $LASTEXITCODE, so opt out.
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -Scope Global -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$WithPascal = -not $NoPascal
$WithRheed  = -not $NoRheed

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$RunDir = Join-Path $ProjectRoot 'run\production'
$LogDir = Join-Path $RunDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

$script:Failed = $false
function Fail($msg) { Write-Host "  FAIL  $msg" -ForegroundColor Red; $script:Failed = $true }
function Ok($msg)   { Write-Host "  ok    $msg" }
function Warn($msg) { Write-Host "  warn  $msg" -ForegroundColor Yellow }

# Windows PowerShell 5.1 has no $PSNativeCommandUseErrorActionPreference, so under
# $ErrorActionPreference = 'Stop' any bytes a child writes to stderr are turned into
# a terminating NativeCommandError -- and 2>$null does not reliably stop that. Every
# `python -c ...` probe goes through here: stderr is dropped and the caller gets back
# stdout (trimmed) plus the process exit code, with no chance of a stray traceback
# aborting the preflight.
function Invoke-Py {
    param([Parameter(Mandatory)][string[]]$PyArgs)
    $ErrorActionPreference = 'SilentlyContinue'
    $out = & $Python @PyArgs 2>$null
    return [pscustomobject]@{ Out = (($out | Out-String).Trim()); Code = $LASTEXITCODE }
}

function Test-PyImport($module) {
    return ((Invoke-Py @('-c', "import $module")).Code -eq 0)
}

function Check-Import($module, $what, $fix) {
    if (Test-PyImport $module) { Ok $what } else { Fail "$what missing -- $fix" }
}

Write-Host "preflight (instrument host):"

# ---- interpreter and packages --------------------------------------------------
if (-not (Test-Path $Python)) {
    Write-Host "  FAIL  no virtualenv at .venv" -ForegroundColor Red
    Write-Host "        uv sync --extra pascal --extra camera"
    exit 1
}
Ok ("venv at .venv (" + (Invoke-Py @('-V')).Out + ")")

# ---- broker host -------------------------------------------------------------
# -BrokerHost wins; otherwise fall back to settings.rabbitmq.host, which an
# override in cfg/.secrets.toml ([rabbitmq] host = "...") flows into. The broker
# is not on this machine, so a loopback value (the tracked settings.toml default)
# is treated as "unset" -- publishing to localhost here silently reaches nothing.
$BrokerHostFromSettings = $false
if (-not $BrokerHost) {
    $probe = Invoke-Py @('-c', 'from lumi.config import settings; print(settings.rabbitmq.host)')
    $BrokerHost = if ($probe.Code -eq 0) { $probe.Out } else { '' }
    $BrokerHostFromSettings = $true
}

# An explicit -BrokerHost of loopback still falls through to the softer warning
# below (single-machine test setups do that on purpose); only the settings-derived
# default -- empty, or the tracked localhost -- is a hard stop here.
$brokerUnset = (-not $BrokerHost) -or ($BrokerHostFromSettings -and $BrokerHost -in @('localhost', '127.0.0.1', '::1'))
if ($brokerUnset) {
    Write-Host ""
    Write-Host "error: no usable broker host -- it runs on the server machine, not here." -ForegroundColor Red
    if ($BrokerHostFromSettings) {
        $shown = if ($BrokerHost) { "'$BrokerHost'" } else { "unset" }
        Write-Host "  settings.rabbitmq.host is $shown. Set the lab broker address in cfg\.secrets.toml:"
        Write-Host ""
        Write-Host "      [rabbitmq]"
        Write-Host '      host = "<server ip>"'
        Write-Host ""
        Write-Host "  or pass it explicitly:"
    }
    Write-Host "  scripts\start_instrument_host.ps1 -BrokerHost <server ip> -User <node user> -Password <pw> ``"
    Write-Host "      -Log <chamber log folder> -Mi <MI mode folder>"
    exit 1
}

if ($BrokerHostFromSettings) {
    Ok "broker host ${BrokerHost} (from settings.rabbitmq.host -- pass -BrokerHost to override)"
}

if ($WithPascal) {
    Check-Import 'watchdog' 'pascal extra (watchdog)' 'uv sync --extra pascal'
    Check-Import 'pandas'   'pascal extra (pandas)'   'uv sync --extra pascal'
}
if ($WithRheed -or $WithPascal) {
    Check-Import 'cv2' 'camera extra (opencv)' 'uv sync --extra camera'
    Check-Import 'av'  'camera extra (av)'     'uv sync --extra camera'
}

# ---- contract hash -----------------------------------------------------------
# Both halves must be built from the same contract; a drift here shows up as ops
# the server host does not recognise, not as a startup error.
$probe = Invoke-Py @('-c', 'from lumi.contracts import contract_hash; print(contract_hash())')
$ContractHash = if ($probe.Code -eq 0 -and $probe.Out) { $probe.Out } else { '?' }
Ok "contract hash $ContractHash (must match the server host)"

# ---- broker ----------------------------------------------------------------
$reachable = $false
try {
    $client = [System.Net.Sockets.TcpClient]::new()
    $async = $client.BeginConnect($BrokerHost, 5672, $null, $null)
    if ($async.AsyncWaitHandle.WaitOne(3000) -and $client.Connected) { $reachable = $true }
    $client.Close()
} catch { $reachable = $false }

if ($reachable) {
    Ok "broker reachable at ${BrokerHost}:5672"
} else {
    Fail "broker unreachable at ${BrokerHost}:5672"
    Write-Host "        Is scripts/start_server_host.sh running on that machine, and is 5672 open?"
}

# RabbitMQ's default loopback_users blocks guest from anywhere but loopback. This
# host is by definition remote from the broker, so guest can never work here.
if ($BrokerHost -in @('localhost', '127.0.0.1', '::1')) {
    Warn "-BrokerHost is loopback: this script is meant for the machine the broker is NOT on"
} elseif ($User -eq 'guest') {
    Fail "RabbitMQ refuses the 'guest' account off loopback -- pass -User/-Password."
    Write-Host "        On the server host, create a node account:"
    Write-Host "        uv run python scripts/apply_broker_permissions.py --host $BrokerHost ``"
    Write-Host "            --user lumi-node --role node --password <pw>"
} else {
    Ok "broker user '$User'"
}

# ---- chamber ---------------------------------------------------------------
if ($WithPascal) {
    if ($Src -eq 'path') {
        # The node raises FileNotFoundError on a missing log and ValueError on a
        # missing -Mi, both after it has already connected. Catch them here instead.
        if (-not $Log) {
            Fail "-Log is required with -Src path (the folder PASCAL writes the growth log into)"
        } elseif (-not (Test-Path $Log)) {
            Fail "chamber log path does not exist: $Log"
        } else {
            Ok "chamber log $Log"
        }

        if (-not $Mi) {
            Fail "-Mi is required with -Src path (the MI mode folder the controller reads)"
        } elseif (-not (Test-Path $Mi -PathType Container)) {
            Fail "MI mode folder does not exist: $Mi"
        } else {
            $probe = Join-Path $Mi ([System.IO.Path]::GetRandomFileName())
            try {
                [System.IO.File]::WriteAllText($probe, '')
                Remove-Item $probe -Force
                Ok "MI mode folder $Mi (writable)"
            } catch {
                Fail "MI mode folder is not writable: $Mi"
                Write-Host "        The node writes the script and assist files there; read-only means"
                Write-Host "        every command silently fails to reach the controller."
            }
        }

        # PLDconfig.ini gives the carousel geometry, so `Select Target C` resolves to
        # an angle. Its default lives in cfg/settings.toml.
        $resolvedPld = $PldConfig
        if (-not $resolvedPld) {
            $probe = Invoke-Py @('-c', 'from lumi.config import settings; print(settings.pascal.config_reader.config_path)')
            $resolvedPld = if ($probe.Code -eq 0) { $probe.Out } else { '' }
        }
        if (-not $resolvedPld) {
            Fail "could not resolve the PLDconfig.ini path"
        } elseif (-not (Test-Path $resolvedPld -PathType Leaf)) {
            Fail "PLDconfig.ini not readable: $resolvedPld"
            Write-Host "        Pass -PldConfig <file>, or fix [pascal.config_reader] in cfg/settings.toml."
        } else {
            Ok "PLDconfig.ini $resolvedPld"
        }
    } else {
        Warn "-Src ${Src}: this is NOT the real chamber (use -Src path on the instrument)"
    }
}

# ---- RHEED camera --------------------------------------------------------------
if ($WithRheed) {
    switch ($RheedSrc) {
        'pylon' {
            if (-not (Test-PyImport 'pypylon.pylon')) {
                Fail "pypylon is not importable -- uv sync --extra camera"
            } else {
                # The node raises "no Basler camera found" at startup; enumerating
                # here turns that into a preflight line instead of a crashed node.
                $probe = Invoke-Py @('-c', 'from lumi.base.camera.pylon_camera import list_devices; print(len(list_devices()))')
                $devices = if ($probe.Code -eq 0) { $probe.Out } else { 'error' }
                if ($devices -eq 'error') {
                    Fail "could not enumerate Basler devices"
                } elseif ($devices -eq '0') {
                    Fail "no Basler camera found -- check the network cable and the camera's IP"
                } else {
                    Ok "$devices Basler camera(s) found"
                }
            }
        }
        default {
            Warn "-RheedSrc ${RheedSrc}: not the Basler camera (use pylon on the instrument)"
        }
    }
}

# ---- competing consumers -----------------------------------------------------
# Two chamber nodes on one broker are competing consumers on the same queue and
# round-robin the RPCs between them, which reads as random lag in the UI.
try {
    $running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -match 'nodes[\\/](pascal|rheed)\.py' }
    if ($running) {
        Warn "pascal/rheed already running on this host -- two nodes on one broker round-robin"
        Warn "the RPCs between them. Stop them first."
    }
} catch {
    Warn "could not check for running pascal/rheed processes ($($_.Exception.Message))"
}

if ($script:Failed) {
    Write-Host ""
    Write-Host "preflight failed -- nothing started." -ForegroundColor Red
    exit 1
}

Write-Host "preflight passed."
if ($Check) { exit 0 }
Write-Host ""

# config_reader has no CLI flag, so -PldConfig has to reach it through dynaconf.
if ($PldConfig) {
    $env:DYNACONF_PASCAL__CONFIG_READER__CONFIG_PATH = $PldConfig
}

$script:Procs   = [System.Collections.Generic.List[object]]::new()
$script:Names   = [System.Collections.Generic.List[string]]::new()
$script:Stopped = $false

function Stop-Nodes {
    if ($script:Stopped) { return }
    $script:Stopped = $true
    Write-Host ""
    Write-Host "shutting down..."
    for ($i = $script:Procs.Count - 1; $i -ge 0; $i--) {
        $p = $script:Procs[$i]
        try {
            if ($p -and -not $p.HasExited) {
                # No SIGTERM on Windows -- this is a hard kill (the whole tree).
                Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            }
        } catch { }
    }
    Write-Host "all nodes stopped"
}

function Quote-Arg($a) {
    if ($a -match '[\s"]') { '"' + ($a -replace '"', '\"') + '"' } else { "$a" }
}

function Start-Node {
    param([string]$Name, [string[]]$NodeArgs)
    $out = Join-Path $LogDir "$Name.log"
    $err = Join-Path $LogDir "$Name.err.log"
    Write-Host "starting $Name -> $out"
    # Windows PowerShell 5.1 does not quote -ArgumentList array elements, so build the
    # line by hand -- otherwise a -Log/-Mi path with spaces splits into extra args.
    $argLine = ((@("nodes\$Name.py") + $NodeArgs) | ForEach-Object { Quote-Arg $_ }) -join ' '
    # Start-Process cannot merge stdout+stderr into one file the way `>> log 2>&1`
    # does, so stderr lands in $Name.err.log alongside it.
    $p = Start-Process -FilePath $Python `
        -ArgumentList $argLine `
        -WorkingDirectory $ProjectRoot `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err
    $script:Procs.Add($p)
    $script:Names.Add($Name)
}

try {
    if ($WithPascal) {
        $pascalArgs = @('--host', $BrokerHost, '--user', $User, '--password', $Password, '--src', $Src)
        if ($Log) { $pascalArgs += @('--log', $Log) }
        if ($Mi)  { $pascalArgs += @('--mi', $Mi) }
        if ($Src -eq 'sim') { $pascalArgs += @('--speed', $Speed) }
        Start-Node -Name 'pascal' -NodeArgs $pascalArgs
    }

    if ($WithRheed) {
        Start-Node -Name 'rheed' -NodeArgs @('--host', $BrokerHost, '--user', $User, '--password', $Password, '--src', $RheedSrc)
    }

    Write-Host ""
    Write-Host "nodes running (Ctrl-C to stop):"
    for ($i = 0; $i -lt $script:Names.Count; $i++) {
        "  {0,-10} pid {1}" -f $script:Names[$i], $script:Procs[$i].Id | Write-Host
    }
    if (-not $WithPascal) { Write-Host "  pascal     skipped (-NoPascal)" }
    if (-not $WithRheed)  { Write-Host "  rheed      skipped (-NoRheed)" }
    Write-Host ""
    Write-Host "publishing to ${BrokerHost}:5672 as '$User' -- contract $ContractHash"
    Write-Host "logs in $LogDir"

    # Exit as soon as either node dies, rather than sitting on a half-dead stack.
    while ($true) {
        for ($i = 0; $i -lt $script:Procs.Count; $i++) {
            if ($script:Procs[$i].HasExited) {
                Write-Host ("node '{0}' exited -- see {1}\{0}.log" -f $script:Names[$i], $LogDir) -ForegroundColor Red
                Stop-Nodes
                exit 1
            }
        }
        Start-Sleep -Seconds 2
    }
} finally {
    # Runs on Ctrl-C too (Start-Sleep is interruptible).
    Stop-Nodes
}
