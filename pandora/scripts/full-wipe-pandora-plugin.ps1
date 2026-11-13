<#
.SYNOPSIS
  Full wipe for Pandora for Hermes: uninstall the plugin and remove every
  trace it left behind on this computer.

.DESCRIPTION
  The Desktop trash button (or `hermes plugins remove pandora`) uninstalls the
  plugin but intentionally keeps:

    - providers.pandora   in each Hermes home's auth.json (the saved session)
    - PANDORA_* values    in each home's .env and in the managed env layer
    - plugin-data\pandora (the downloaded mpv build, ~120 MB, plus diag.log)
    - desktop-plugins\pandora (the status-bar half; app level)

  Those leftovers are what make a reinstall start signed in and skip the mpv
  download. Run this script only when you want them gone too: switching Pandora
  accounts, reclaiming disk space, troubleshooting, or leaving no trace.

  It then uninstalls the plugin from every profile it finds. Every file it
  edits is backed up beside the original as <file>.bak-<timestamp>.

.PARAMETER Yes
  Skip the confirmation prompt.

.NOTES
  Requires Windows PowerShell 5.1+. This script ships inside the plugin (its
  scripts\ folder), so anyone who installed the plugin has it; run the copy
  from the installed plugin folder. That is safe: PowerShell loads the whole
  script into memory before executing, so it keeps running even after the
  plugin tree (and this file) is deleted during the final step. Close Hermes
  Desktop before running (a running gateway keeps plugin files open; the CLI
  parks those trees and the next start finishes the delete). Note that closing
  the Desktop window does not always stop the gateway, whose kill-on-close job
  keeps the plugin's mpv alive — the script now stops that mpv itself (by its
  path under plugin-data\pandora\bin) before deleting. If PANDORA_MPV_PATH
  points at a different mpv, stop that player first. Anything still locked
  (an open diag.log) is reported, and the script is safe to re-run once
  Hermes is closed. A pre-flight check warns loudly while the desktop app is
  still running, because it re-provisions what the wipe deletes (re-copying
  the desktop half, re-downloading mpv) — close the app first for a
  deterministic result.
#>
param([switch]$Yes)

# Any terminating error is shown and the window waits, so a failure can never
# look like "nothing happened". ($home is a read-only automatic variable in
# PowerShell: never name a loop variable $home.)
trap {
  Write-Host ''
  Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
  Write-Host ("At: " + $_.InvocationInfo.PositionMessage) -ForegroundColor DarkYellow
  Write-Host 'Nothing further was changed. Fix the cause and re-run.' -ForegroundColor Red
  if ($Host.Name -eq 'ConsoleHost') { Read-Host 'Press Enter to close' }
  exit 1
}

$ErrorActionPreference = 'Stop'

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$root  = Join-Path $env:LOCALAPPDATA 'hermes'
if (-not (Test-Path -LiteralPath $root)) { throw "Hermes home not found: $root" }

# A Hermes-spawned terminal points HERMES_HOME at one profile; clear it so the
# bare `hermes` calls below mean the global profile, as they should here.
Remove-Item Env:HERMES_HOME -ErrorAction SilentlyContinue

if (-not $Yes) {
  Write-Host "This uninstalls Pandora from Hermes and deletes its saved login," -ForegroundColor Yellow
  Write-Host "credentials, downloaded mpv build and diagnostics under $root." -ForegroundColor Yellow
  $answer = Read-Host 'Type WIPE to continue'
  if ($answer -ne 'WIPE') { Write-Host 'Aborted. Nothing was changed.'; return }
}

# A desktop app still running re-provisions what this wipe deletes: it
# re-copies the desktop half and the running backend re-downloads the mpv
# build (observed 2026-11-12 — both trees were back within two minutes).
# Warn so the user can close the app and re-run instead of wondering why the
# leftovers returned.
$liveHermes = @(Get-Process -Name 'Hermes' -ErrorAction SilentlyContinue)
if ($liveHermes.Count -gt 0) {
  Write-Host "WARNING: Hermes Desktop is still running ($($liveHermes.Count) process(es))." -ForegroundColor Yellow
  Write-Host '         It will re-copy the desktop half and re-download mpv as you delete' -ForegroundColor Yellow
  Write-Host '         them. For a deterministic wipe, close the app first and re-run' -ForegroundColor Yellow
  Write-Host '         (idempotent); a stopped gateway keeps any plugin tree it has loaded locked.' -ForegroundColor Yellow
}

$homes = @($root)
$profilesDir = Join-Path $root 'profiles'
if (Test-Path -LiteralPath $profilesDir) {
  # Only real profile names: Hermes keeps tombstone folders such as ".deleted"
  # here, and the CLI rejects them as profile names.
  Get-ChildItem -LiteralPath $profilesDir -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^[a-z0-9][a-z0-9_-]{0,63}$' } |
    ForEach-Object { $homes += $_.FullName }
}
$legacy = Join-Path $HOME '.hermes'
if ((Test-Path -LiteralPath $legacy) -and ($legacy -ne $root)) { $homes += $legacy }

# Every folder that could hold a plugin tree, tombstones included: the final
# fallback deletes leftovers there directly, without asking the CLI (which
# rejects names like ".deleted").
$everyDir = @($root)
if (Test-Path -LiteralPath $profilesDir) {
  $everyDir += @(Get-ChildItem -LiteralPath $profilesDir -Directory -ErrorAction SilentlyContinue |
                    ForEach-Object { $_.FullName })
}
if ((Test-Path -LiteralPath $legacy) -and ($legacy -ne $root)) { $everyDir += $legacy }

function Write-TextNoBom([string]$path, [string]$text) {
  [System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))
}

function Strip-EnvKeys([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return }
  $lines = @(Get-Content -LiteralPath $path)
  $kept  = @($lines | Where-Object { $_ -notmatch '^\s*(export\s+)?PANDORA_' })
  if ($kept.Count -eq $lines.Count) { return }
  Copy-Item -LiteralPath $path "$path.bak-$stamp"
  Write-TextNoBom $path (($kept -join "`r`n") + "`r`n")
  Write-Host ("  stripped {0} PANDORA_* line(s) from {1}" -f ($lines.Count - $kept.Count), $path)
}

function Strip-ProviderState([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return }
  try { $json = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } catch { return }
  if (-not $json.PSObject.Properties['providers']) { return }
  if (-not $json.providers.PSObject.Properties['pandora']) { return }
  Copy-Item -LiteralPath $path "$path.bak-$stamp"
  $json.providers.PSObject.Properties.Remove('pandora')
  Write-TextNoBom $path ($json | ConvertTo-Json -Depth 100)
  Write-Host "  removed providers.pandora from $path"
}

function Strip-InstallRecord([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return }
  try { $json = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } catch { return }
  if (-not $json.PSObject.Properties['pandora']) { return }
  Copy-Item -LiteralPath $path "$path.bak-$stamp"
  $json.PSObject.Properties.Remove('pandora')
  Write-TextNoBom $path ($json | ConvertTo-Json -Depth 100)
  Write-Host "  removed the pandora install record from $path"
}

foreach ($h in $homes) {
  $label = if ($h -eq $root) { 'global' } else { Split-Path $h -Leaf }
  Write-Host "[$label]"
  Strip-ProviderState (Join-Path $h 'auth.json')
  Strip-EnvKeys (Join-Path $h '.env')
  Strip-InstallRecord (Join-Path $h 'plugins\.install-metadata.json')
}

# The managed env layer (all-profiles credentials); keep the file/var if it
# still carries values that are not ours.
$managed = Join-Path $root 'managed\.env'
Strip-EnvKeys $managed
if (Test-Path -LiteralPath $managed) {
  $rest = @(Get-Content -LiteralPath $managed | Where-Object { $_ -match '\S' -and $_ -notmatch '^\s*#' })
  if ($rest.Count -eq 0) {
    Remove-Item -LiteralPath $managed -Force
    Write-Host '  removed the now-empty managed env file'
    Write-Host '  note: the HERMES_MANAGED_DIR user variable can stay; it is harmless when the folder is empty'
  }
}

# A gateway outliving the Desktop window keeps its kill-on-close job open, so
# the plugin's mpv survives "close the app". Stop it first — matched by its
# executable path in this plugin's data dir, so an unrelated mpv (e.g. a
# PANDORA_MPV_PATH install) is spared — otherwise its locked exe/DLLs survive
# the delete below.
$pluginMpv = Join-Path $root 'plugin-data\pandora\bin\mpv.exe'
$strayMpv = @(Get-CimInstance Win32_Process -Filter "Name='mpv.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $pluginMpv) })
foreach ($proc in $strayMpv) {
  Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
  Write-Host "  stopped plugin mpv (pid $($proc.ProcessId))"
}
if ($strayMpv.Count -gt 0) { Start-Sleep -Milliseconds 500 }   # let the OS release the file locks

# Best effort: a running Hermes (a playing mpv.exe, an open diag.log) locks
# individual files, and Windows refuses to delete or even open them. Delete
# item by item deepest-first so one locked file cannot strand the rest of the
# tree; anything that survives is reported for a re-run. The script is
# idempotent, so re-running it after closing Hermes finishes the job.
$leftovers = @()
foreach ($dir in @((Join-Path $root 'plugin-data\pandora'),
                   (Join-Path $root 'desktop-plugins\pandora'))) {
  if (-not (Test-Path -LiteralPath $dir)) { continue }
  $items = @(Get-ChildItem -LiteralPath $dir -Recurse -ErrorAction SilentlyContinue |
             Sort-Object { $_.FullName.Length } -Descending)
  foreach ($item in $items) {
    try { Remove-Item -LiteralPath $item.FullName -Recurse -Force }
    catch { $leftovers += $item.FullName }
  }
  # -Recurse here (and never interactively): the children were already
  # attempted individually above, so only locked stragglers can remain, and a
  # prompt-less attempt on them simply fails into the catch below.
  try { Remove-Item -LiteralPath $dir -Recurse -Force } catch { }
  if (-not (Test-Path -LiteralPath $dir)) { Write-Host "  deleted $dir" }
}
if ($leftovers.Count -gt 0) {
  Write-Warning ("{0} item(s) survived - a running Hermes (mpv playback, open log) holds them." -f $leftovers.Count)
  Write-Warning 'Close Hermes Desktop and re-run this script (it is idempotent), or delete them by hand:'
  foreach ($left in $leftovers) { Write-Warning "  $left" }
}

# Uninstall the code from every home. The CLI is what removes the tree and the
# install metadata together; a direct delete is the fallback if it is missing.
$hermesExe = Get-Command hermes -ErrorAction SilentlyContinue
Push-Location $root   # never hold a working directory inside a tree being removed
$savedEap = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'   # a home without the plugin must not stop the run
try {
  if ($hermesExe) {
    $cliFailed = 0
    & hermes plugins remove pandora 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { $cliFailed++ }
    foreach ($h in @($homes | Where-Object { $_ -ne $root -and $_ -ne $legacy })) {
      & hermes -p (Split-Path $h -Leaf) plugins remove pandora 2>&1 | Out-Null
      if ($LASTEXITCODE -ne 0) { $cliFailed++ }
    }
    if ($cliFailed -eq 0) {
      Write-Host '  plugin removed from every home it was installed in'
    } else {
      Write-Warning ("  the CLI uninstalled what it could ($cliFailed home(s) reported an error, often 'not installed'); leftover trees are deleted below")
    }
  } else {
    Write-Warning 'hermes is not on PATH - uninstall from the Desktop trash button instead'
  }
} catch {
  Write-Warning "the CLI reported an error for one home; the fallback delete below still runs: $($_.Exception.Message)"
} finally {
  $ErrorActionPreference = $savedEap
  Pop-Location
}

foreach ($h in $everyDir) {
  $tree = Join-Path $h 'plugins\pandora'
  if (Test-Path -LiteralPath $tree) {
    try { Remove-Item -LiteralPath $tree -Recurse -Force; Write-Host "  removed leftover $tree" }
    catch { Write-Warning "could not remove $tree (close Hermes and re-run)" }
  }
}

Write-Host ''
Write-Host 'Done. Close and reopen Hermes Desktop; a reinstall now starts signed out.' -ForegroundColor Green
if (-not $Yes -and $Host.Name -eq 'ConsoleHost') { Read-Host 'Press Enter to close' }
exit 0
