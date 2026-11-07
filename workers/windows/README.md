# Raven Windows worker (home PC over Tailscale)

The Windows agent drives a real Windows PC through **cua-spacesd** (Cua's guest daemon,
gRPC/HTTP on port **3211**). raven-core connects with the Cua Python SDK:

```python
Sandbox.connect(url="http://<tailscale-ip>:3211", token="<CUA_ENV_TOKEN>")
```

`raven_core.workers.get_worker(Platform.windows)` builds that from `.env`:

```
WINDOWS_WORKER_HOST=100.x.y.z        # or 100.x.y.z:3211 / http://100.x.y.z:3211 / MagicDNS name
WINDOWS_WORKER_TOKEN=<the env token> # falls back to CUA_ENV_TOKEN
```

Everything below happens **on the Windows PC** unless marked *Mac*. ~20 min.
`setup.ps1` in this folder automates steps 2, 4 and 5 (run it in an elevated PowerShell).

---

## 1. Tailscale

1. Install https://tailscale.com/download/windows, sign in to the **same tailnet** as the Mac.
2. In the admin console, *Disable key expiry* for this machine (so it doesn't drop off mid-demo).
3. Note the IP: `tailscale ip -4` → e.g. `100.101.102.103`.
4. *Mac*: `tailscale ping <ip>` must answer.

## 2. Never sleep, never lock (elevated PowerShell)

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off
# don't require sign-in on wake
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0
powercfg /SETACTIVE SCHEME_CURRENT
# no lock screen / screensaver lock
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization" -Name NoLockScreen -Value 1 -Type DWord
Set-ItemProperty -Path "HKCU:\Control Panel\Desktop" -Name ScreenSaveActive -Value 0
Set-ItemProperty -Path "HKCU:\Control Panel\Desktop" -Name ScreenSaverIsSecure -Value 0
```

Also: Settings → Accounts → Sign-in options → "If you've been away, when should Windows require you
to sign in again?" → **Never**. Optional but recommended: Sysinternals **Autologon**
(https://learn.microsoft.com/sysinternals/downloads/autologon) so a reboot lands on the desktop.
Settings → Windows Update → *Pause updates* for 1 week (no surprise reboots during the demo).

Display: keep scaling at **100%** and a resolution around 1920x1080 (Holo clicks on a normalized grid;
huge DPI scaling makes text tiny in screenshots). If the PC is headless, use an HDMI dummy plug,
otherwise the console falls back to 1024x768 after `tscon`.

## 3. Install bitchat-tui (Nostr geohash channels, no Bluetooth needed)

1. Rust: https://rustup.rs → `rustup-init.exe` (default options). If cargo complains about `link.exe`,
   install **Visual Studio Build Tools** with workload **Desktop development with C++**.
2. Git: `winget install --id Git.Git -e`
3. Build + install (new PowerShell so PATH has cargo):
   ```powershell
   cargo install --locked --git https://github.com/E8EgsqUO/bitchat-tui bitchat-tui
   bitchat-tui.exe --help   # %USERPROFILE%\.cargo\bin must be on PATH (rustup does this)
   ```
4. Manual test: run `bitchat-tui.exe` in Windows Terminal; if the Bluetooth scan errors/stalls, that's fine —
   type `/j #u09t`, send a message, and check it shows up on another client (e.g. the Linux sandbox or a phone).
5. Make the terminal readable for the agent: Windows Terminal → Settings → Defaults → Appearance → font size 14+.
6. Fallback client if bitchat-tui misbehaves: `cargo install --locked --git https://github.com/derekross/bitchatx`
   (`bitchatx.exe --channel u09t`, IRC-style `/join u09t`).

## 4. Install Cua and run cua-spacesd (direct mode, Tailscale only)

```powershell
irm https://cua.ai/install.ps1 | iex           # cua CLI (choose CLI only if asked)
irm https://cua.ai/driver/install.ps1 | iex    # Cua Driver: spacesd delegates mouse/keyboard to it
```

**Option A — managed service (preferred):**

```powershell
cua host setup --direct <tailscale-ip>:3211 --runner windows-task --name raven-windows
cua host status        # must say set up + running; shows where the env token is stored
```

`--direct ip:port` serves on the LAN/tailnet with a local env token instead of the cua.ai relay
(no account needed). Binding to the Tailscale IP keeps it off other networks. Copy the token into the
Mac's `.env` as `WINDOWS_WORKER_TOKEN`. The `windows-task` runner starts spacesd at logon **in the
interactive user session** — required, a session-0 service can't see or drive the desktop.

**Option B — by hand with your own token (if A is confusing / token not printed):**

```powershell
$tok = -join ((48..57)+(97..102) | Get-Random -Count 48 | % {[char]$_})
[Environment]::SetEnvironmentVariable("CUA_ENV_TOKEN", $tok, "User")
[Environment]::SetEnvironmentVariable("CUA_ENV_LISTEN", "0.0.0.0", "User")
$tok    # -> WINDOWS_WORKER_TOKEN on the Mac
# start at logon in the user session:
$exe = (Get-Command cua-spacesd).Source
$act = New-ScheduledTaskAction -Execute $exe -Argument "serve"
$trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName "cua-spacesd" -Action $act -Trigger $trg -RunLevel Highest -Force
Start-ScheduledTask -TaskName "cua-spacesd"
```

(If `cua-spacesd` isn't on PATH, look next to `cua.exe`: `Get-Command cua`.)

Firewall — allow 3211 only from the tailnet:

```powershell
New-NetFirewallRule -DisplayName "cua-spacesd (tailscale)" -Direction Inbound -Protocol TCP -LocalPort 3211 `
  -RemoteAddress 100.64.0.0/10 -Action Allow
```

The env token grants full control of the PC: never put it in URLs, logs, or git.

## 5. RDP without locking the console (`tscon` trick)

If you administer the PC over RDP, **closing the RDP window locks the console session** → spacesd
screenshots go black and input fails. Instead, hand the session back to the physical console:

Save as `Desktop\release-to-console.bat` and run it **as Administrator** from inside the RDP session
(your RDP window closes, the desktop stays unlocked and running):

```bat
@echo off
for /f "skip=1 tokens=3" %%s in ('query user %USERNAME%') do (
  %windir%\System32\tscon.exe %%s /dest:console
)
```

(`setup.ps1` writes this file for you.) Prefer Tailscale + a VNC viewer / the spacesd viewer
(`http://<ip>:3211/viewer/` if available) for watching without RDP.

## 6. Verify from the Mac

```bash
cua spacesd caps http://<tailscale-ip>:3211           # version, OS, features
cd raven-core
uv run python scripts/sandbox_setup.py windows         # runs `ver`, prints screen size
uv run python scripts/sandbox_setup.py shot windows    # -> artifacts/spike-windows.png
```

Spec setup for Windows runs `bitchat-tui.exe` via `cmd /c start cmd /k bitchat-tui.exe` (opens a
new console window on the desktop) — works whether spacesd's shell is cmd or PowerShell.

## Troubleshooting

- Black screenshots → console session locked/disconnected: log in physically or use the `tscon` bat.
- Connection refused → spacesd not running in the user session (`Get-ScheduledTask cua-spacesd`), or firewall.
- Clicks land offset → display scaling ≠ 100%.
- To fake Windows during development: `RAVEN_FAKE_WORKERS=windows` in `.env`.
