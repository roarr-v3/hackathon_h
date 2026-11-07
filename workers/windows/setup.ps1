# Raven Windows worker prep. Run in an ELEVATED PowerShell on the Windows PC.
#   Set-ExecutionPolicy -Scope Process Bypass; .\setup.ps1
# Idempotent. Does: power/lock settings, firewall rule for 3211 (tailnet only),
# release-to-console.bat on the Desktop, cua CLI + Cua Driver install.
# Then follow README.md step 3 (bitchat-tui) and step 4 (cua host setup --direct ...).
$ErrorActionPreference = "Continue"

Write-Host "== power: never sleep, no sign-in on wake"
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0
powercfg /SETACTIVE SCHEME_CURRENT

Write-Host "== no lock screen / secure screensaver"
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" -Name NoLockScreen -Value 1 -Type DWord
Set-ItemProperty -Path "HKCU:\Control Panel\Desktop" -Name ScreenSaveActive -Value 0
Set-ItemProperty -Path "HKCU:\Control Panel\Desktop" -Name ScreenSaverIsSecure -Value 0

Write-Host "== firewall: 3211 from tailnet only"
if (-not (Get-NetFirewallRule -DisplayName "cua-spacesd (tailscale)" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "cua-spacesd (tailscale)" -Direction Inbound -Protocol TCP -LocalPort 3211 `
    -RemoteAddress 100.64.0.0/10 -Action Allow | Out-Null
}

Write-Host "== Desktop\release-to-console.bat (run as admin instead of closing RDP)"
$bat = @'
@echo off
for /f "skip=1 tokens=3" %%s in ('query user %USERNAME%') do (
  %windir%\System32\tscon.exe %%s /dest:console
)
'@
Set-Content -Path (Join-Path ([Environment]::GetFolderPath("Desktop")) "release-to-console.bat") -Value $bat -Encoding ASCII

Write-Host "== cua CLI + Cua Driver"
if (-not (Get-Command cua -ErrorAction SilentlyContinue)) { irm https://cua.ai/install.ps1 | iex }
if (-not (Get-Command cua-driver -ErrorAction SilentlyContinue)) { irm https://cua.ai/driver/install.ps1 | iex }

Write-Host "== Tailscale IP:"
try { tailscale ip -4 } catch { Write-Host "tailscale not installed: https://tailscale.com/download/windows" }

Write-Host ""
Write-Host "Next: README.md step 3 (bitchat-tui) and step 4:"
Write-Host "  cua host setup --direct <tailscale-ip>:3211 --runner windows-task --name raven-windows"
