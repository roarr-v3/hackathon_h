# Testing Raven

## 1. Setup

- **Tools:** macOS on Apple silicon, Xcode 27, Node 26, pnpm 12, `uv`, and the Devin CLI (`devin auth`).
- **Keys:** `cp .env.example .env`, then set `HAI_API_KEY`. The other keys are optional:
  - `GLADIA_API_KEY`: voice.
  - `LINKUP_API_KEY`: failure research.
  - `DEVIN_API_KEY` + `DEVIN_ORG_ID`: Devin VMs and cloud planning.
- **Linux target:** needs the Cua CLI and the `raven-linux-apple` Space.
  Prepare it once with `cd raven-core && uv run python scripts/sandbox_setup.py linux --no-fallback`.
- **macOS and iOS targets:** grant Screen Recording and Accessibility to Raven Agent.
- **iOS target:** connect and unlock your iPhone, trust this Mac and enable Developer Mode.

## 2. Run Raven

```bash
bin/raven            # build if needed, then open Raven Agent (it starts raven-core)
bin/raven rebuild    # rebuild after pulling changes
bin/raven status     # is Raven Agent / raven-core running?
bin/raven stop       # stop raven-core if Raven Agent left it running
```

Without the desktop app: `cd raven-core && uv run raven-core serve` (API on `http://localhost:8787`).

- **Logs:** `artifacts/logs/raven-core.log`.
- **History:** `artifacts/events.jsonl`, reloaded on restart.
- **Screenshots:** `artifacts/<agent-id>/`.

## 3. Smoke tests

Run each from Raven Agent, the iPhone app, or the terminal (`cd raven-core` first).

| Target | Command or prompt | Pass when |
|---|---|---|
| Linux | `uv run raven-core run holo-linux-smoke --platforms linux` | `RAVEN INPUT OK <code>` is visible |
| macOS | `uv run raven-core agent macos "Open TextEdit and type bonjour. Pass if bonjour is visible."` | verdict `PASSED` |
| iOS | prompt: `open device hub and show sinan's iphone screen` | the phone screen is live in Device Hub |
| Phone app | send a task from the home screen, then reply in its thread | a thread appears and continues |

`raven-core agent` prints every action, then `PASSED` or `FAILED`; its exit code is 0 on a pass.

## 4. iPhone app (RavenMobile)

Build and install on your iPhone. The scheme launches without the debugger, so it opens quickly.

```bash
DEVICE_ID="<physical device ID from: curl -s localhost:8787/execution/devices>"
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile \
  -destination "platform=iOS,id=$DEVICE_ID" -derivedDataPath /tmp/raven-ios -allowProvisioningUpdates build
xcrun devicectl device install app --device "$DEVICE_ID" /tmp/raven-ios/Build/Products/Debug-iphoneos/RavenMobile.app
```

Then pair it: scan the QR code in Raven Agent → Settings → Raven. Away from the Mac's network, use
Tailscale on both devices.

## 5. Automated tests

```bash
(cd raven-core && RAVEN_ENV_FILE=/dev/null uv run pytest -q)   # ignore your .env for a clean run
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile \
  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build
(cd t3code && PATH="$PWD/node_modules/.bin:$PATH" npx vp run --filter @t3tools/desktop --filter t3 build)
```

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| "No physical iPhone is available" | Connect and unlock the iPhone, trust this Mac, and turn on Developer Mode. Run `xcrun devicectl list devices` to check it. |
| Planning is slow | Set `DEVIN_PLANNING_MODEL=gpt-6-sol-none-priority` in `.env`, then restart Raven Agent. |
| New threads use Devin, not Holo | Raven Agent → Settings → Threads → Default model → Raven · Holo4 (and clear any per-project default). |
| iPhone app shows no chats | Make sure Raven Agent is running, then pull to refresh. Chats are reloaded from `artifacts/events.jsonl`. |
| `devin acp` sign-in error from a terminal | Unset `ACP_BACKEND` before starting raven-core from an IDE terminal. |
