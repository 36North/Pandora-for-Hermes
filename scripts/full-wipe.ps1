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
  Requires Windows PowerShell 5.1+. Close Hermes Desktop before running (a
  running gateway keeps plugin files open; the CLI parks those trees and the
  next start finishes the delete).
#>
param([switch]$Yes)

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

$homes = @($root)
$profilesDir = Join-Path $root 'profiles'
if (Test-Path -LiteralPath $profilesDir) {
  Get-ChildItem -LiteralPath $profilesDir -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { $homes += $_.FullName }
}
$legacy = Join-Path $HOME '.hermes'
if ((Test-Path -LiteralPath $legacy) -and ($legacy -ne $root)) { $homes += $legacy }

function Write-TextNoBom([string]$path, [string]$text) {
  [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
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

foreach ($home in $homes) {
  $label = if ($home -eq $root) { 'global' } else { Split-Path $home -Leaf }
  Write-Host "[$label]"
  Strip-ProviderState (Join-Path $home 'auth.json')
  Strip-EnvKeys (Join-Path $home '.env')
  Strip-InstallRecord (Join-Path $home 'plugins\.install-metadata.json')
}

# The managed env layer (all-profiles credentials); keep the file/var if it
# still carries values that are not ours.
$managed = Join-Path $root 'managed\.env'
Strip-EnvKeys $managed
if (Test-Path -LiteralPath $managed) {
  $rest = @(Get-Content -LiteralPath $managed | Where-Object { $_ -match '\S' })
  if ($rest.Count -eq 0) {
    Remove-Item -LiteralPath $managed -Force
    Write-Host '  removed the now-empty managed env file'
    Write-Host '  note: the HERMES_MANAGED_DIR user variable can stay; it is harmless when the folder is empty'
  }
}

foreach ($dir in @((Join-Path $root 'plugin-data\pandora'),
                   (Join-Path $root 'desktop-plugins\pandora'))) {
  if (Test-Path -LiteralPath $dir) {
    try {
      Remove-Item -LiteralPath $dir -Recurse -Force
      Write-Host "  deleted $dir"
    } catch {
      Write-Warning "could not delete $dir (close Hermes and re-run): $_"
    }
  }
}

# Uninstall the code from every home. The CLI is what removes the tree and the
# install metadata together; a direct delete is the fallback if it is missing.
$hermes = Get-Command hermes -ErrorAction SilentlyContinue
Push-Location $root   # never hold a working directory inside a tree being removed
try {
  if ($hermes) {
    & hermes plugins remove pandora 2>$null
    foreach ($home in @($homes | Where-Object { $_ -ne $root -and $_ -ne $legacy })) {
      & hermes -p (Split-Path $home -Leaf) plugins remove pandora 2>$null
    }
    Write-Host '  plugin removed from every home it was installed in'
  } else {
    Write-Warning 'hermes is not on PATH - uninstall from the Desktop trash button instead'
  }
} finally { Pop-Location }

foreach ($home in $homes) {
  $tree = Join-Path $home 'plugins\pandora'
  if (Test-Path -LiteralPath $tree) {
    try { Remove-Item -LiteralPath $tree -Recurse -Force; Write-Host "  removed leftover $tree" }
    catch { Write-Warning "could not remove $tree (close Hermes and re-run)" }
  }
}

Write-Host ''
Write-Host 'Done. Close and reopen Hermes Desktop; a reinstall now starts signed out.' -ForegroundColor Green
