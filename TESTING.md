# Raven — build, run and test

Everything below runs from a terminal on the Mac (Apple silicon, macOS 26). Paths are relative to the
repo root `hackathon_h/` unless stated otherwise.

```
raven-core (Python, :8787)  ← the brain: API, agents, voice, reports
  ├─ Raven Agent      (t3code desktop app)   talks to it over ACP
  ├─ RavenMobile      (iOS app)              talks to it over HTTP/WebSocket
  └─ test machines    (Cua Linux container, Cua macOS VM, Windows/Devin later)
        └─ H Company computer use (Holo4) clicks and types inside them
```

## 0. One-time setup

### Tools
```bash
brew install uv node pnpm colima docker docker-buildx gh
curl -fsSL https://cua.ai/install.sh | sh -s -- --cli-only      # Cua CLI → ~/.local/bin/cua
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh
```
Xcode 27 is needed for the iOS app.

### Keys
```bash
cp .env.example .env      # gitignored — never commit it
open -e .env              # fill in the values
```
| Key | Needed for | Where to get it |
|---|---|---|
| `HAI_API_KEY` | all computer use (required) | platform.hcompany.ai → API keys |
| `GLADIA_API_KEY` | voice input | app.gladia.io |
| `LINKUP_API_KEY` | failure research in reports | app.linkup.so |
| `DEVIN_API_KEY` | Devin cloud sessions via API | app.devin.ai → Settings |

Check which keys are set (names only):
```bash
grep -oE '^[A-Z_]+=.' .env | sed 's/=.$/ is set/'
```

### Dependencies
```bash
(cd raven-core && uv sync)
(cd t3code && pnpm install)
```

## 1. Start the test machines

### Linux (Cua container) — ready in seconds
```bash
colima start --cpu 4 --memory 6 --disk 60 --vm-type vz   # Docker runtime, once per boot
cua sb create linux --name raven-linux --wait desktop    # skip if `cua sb ls` already shows it
cua sb ls
```
raven-core installs H's desktop driver and its deps into the container on first use.
bitchat-tui is pre-built in the container at `/home/cua/bitchat-tui/target/release/bitchat-tui`
(rebuild: `docker exec -u cua -e HOME=/home/cua raven-linux bash -lc 'cd ~/bitchat-tui && cargo build --release'`).

Launch bitchat on the sandbox desktop:
```bash
docker exec -u cua -e HOME=/home/cua -e DISPLAY=:1 -d raven-linux \
  xfce4-terminal --maximize -T bitchat -e /home/cua/bitchat-tui/target/release/bitchat-tui
```

### macOS (Cua Lume VM) — first pull is ~23 GB
```bash
cua sb create macos --name raven-mac      # later creates clone in ~25 s
```

### Watch a machine live
```bash
cua sb view raven-linux                   # opens the HTML5 viewer in your browser
cua sb screenshot raven-linux -o /tmp/linux.png
```

## 2. Start raven-core

```bash
cd raven-core
RAVEN_H_MODE=models-loop uv run raven-core serve --host 0.0.0.0 --port 8787
```
Leave it running in its own terminal. Health check: `curl localhost:8787/health` → `{"ok":true}`.

`RAVEN_H_MODE` picks how H drives the machine:

| Value | What runs | Notes |
|---|---|---|
| `models-loop` | Holo4 via H's Models API + Raven's loop | **use this now** — no session quota |
| `agents-api` | H's managed `h/holo-desktop` agent | counts against the plan's 3 concurrent sessions |
| `local-runtime` | H's agent loop inside the machine | not available for linux-arm64 containers |

Model: `HAI_MODEL=holo4-27b` (most capable) or `holo4-35b-a3b` (fastest/cheapest) in `.env`.

UI testing without spending credits: `RAVEN_FAKE_WORKERS=1 uv run raven-core serve` (fake screens; agents
still need `HAI_API_KEY` to think).

## 3. Test from the terminal

### One agent, followed live
```bash
cd raven-core
uv run raven-core agent linux "Open a terminal window and run: uname -a. Pass if the Linux kernel line is visible."
```
Prints every action and screenshot path, then `PASSED`/`FAILED`; exit code 0 = passed.
Ctrl+C stops the agent. Screenshots: `artifacts/<agent-id>/NNN.png` at the repo root.

bitchat example (start bitchat first, see §1):
```bash
uv run raven-core agent linux 'In the bitchat window, click the "Type a message" box, type /j #u09t and press Enter. Then type: hello from raven and press Enter. Pass if the message appears in the channel.'
```

### A whole test suite (several platforms in parallel)
```bash
uv run raven-core run bitchat-geohash-interop --platforms linux,macos
```
Suites live in `specs/*.yaml`. `bitchat-geohash-interop` needs the macOS sender, so it only passes once
`raven-mac` exists.

### Raw API (curl)
```bash
curl -s localhost:8787/specs | jq
curl -s -X POST localhost:8787/agents -H 'content-type: application/json' \
  -d '{"platform":"linux","task":"Open Firefox and go to example.com. Pass if the title is Example Domain."}' | jq
curl -s localhost:8787/agents | jq '.[] | {id, platform, status, verdict_reason}'
curl -s localhost:8787/agents/<agent-id> | jq '.events[] | {type, data}'
curl -s -X POST localhost:8787/agents/<agent-id>/messages -H 'content-type: application/json' -d '{"text":"close the popup first"}'
curl -s -X POST localhost:8787/agents/<agent-id>/stop
curl -s -X POST localhost:8787/stop                      # stop everything
curl -s -X POST localhost:8787/voice/command -H 'content-type: application/json' -d '{"text":"run the bitchat test on linux"}'
```

### Voice (Gladia) from the terminal
```bash
say -o /tmp/v.aiff "start linux agent open a terminal and run uname" && \
afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/v.aiff /tmp/v.wav
cd raven-core && uv run python - <<'EOF'
import asyncio, json, httpx, websockets, wave
async def main():
    s = httpx.post("http://localhost:8787/voice/session").json()
    w = wave.open("/tmp/v.wav"); pcm = w.readframes(w.getnframes())
    async with websockets.connect(s["url"]) as ws:
        for i in range(0, len(pcm), 3200):
            await ws.send(pcm[i:i+3200]); await asyncio.sleep(0.1)
        await ws.send(json.dumps({"type": "stop_recording"}))
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") == "transcript" and m["data"].get("is_final"):
                text = m["data"]["utterance"]["text"]; print("heard:", text)
                print(httpx.post("http://localhost:8787/voice/command", json={"text": text}).json())
asyncio.run(main())
EOF
```

### Through Raven Agent's real thread pipeline (headless)
Uses a throwaway t3code server so it never touches the desktop app's state.
```bash
cd t3code && export PATH="$PWD/node_modules/.bin:$PATH"
node scripts/dev-runner.ts dev:server --home-dir /tmp/raven-t3-test --no-seed &   # note the PID
cd apps/server
node src/bin.ts project add /path/to/hackathon_h --base-dir /tmp/raven-t3-test      # prints <project-id>
node src/bin.ts thread start --base-dir /tmp/raven-t3-test --provider raven --model holo4-27b --wait <project-id> "help"
node src/bin.ts thread start --base-dir /tmp/raven-t3-test --provider raven --model holo4-27b --wait <project-id> "on linux: open a terminal and run uname -a"
```

## 4. Build and run Raven Agent (desktop app)

```bash
cd t3code && export PATH="$PWD/node_modules/.bin:$PATH"
npx vp run --filter @t3tools/desktop --filter t3 build                 # ~20 s, rerun after code changes
T3CODE_HOME="$PWD/.t3" node apps/desktop/scripts/start-electron.mjs   # one window, no watcher
```
- `T3CODE_HOME=$PWD/.t3` keeps its data inside the repo. **Never** point it at `~/.t3` (real T3 Code data).
- Do **not** use `pnpm dev:desktop` for testing: its watcher relaunches Electron on every rebuild and
  orphaned watchers respawn windows in a loop.
- Start raven-core (§2) first; the Raven provider connects to `http://localhost:8787`.

In the app:
1. New thread → provider **Raven** → type `help`, `specs`, `status`, `stop`, or `on linux: <task>`.
2. Mic button (or `Cmd+Shift+D`) → speak → text lands in the composer → Enter.
3. Providers **Devin** / **Devin Cloud**: Settings → Providers → *Log in with browser* (required once;
   Devin ignores CLI credentials in ACP mode).

## 5. Build and run RavenMobile (iOS)

### Simulator
```bash
SIM="iPhone 18 Pro"
xcrun simctl boot "$SIM" 2>/dev/null; open -a Simulator
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile \
  -destination "platform=iOS Simulator,name=$SIM" -derivedDataPath /tmp/raven-ios-build build
xcrun simctl install booted /tmp/raven-ios-build/Build/Products/Debug-iphonesimulator/RavenMobile.app
xcrun simctl launch booted ai.raven.mobile -demo NO -server http://localhost:8787
```
`-demo YES` runs a fully simulated backend (no keys needed). Screenshot: `xcrun simctl io booted screenshot /tmp/ios.png`.

### Your iPhone
1. Plug it in (or Xcode → Window → Devices → *Connect via network*), enable Developer Mode on the phone.
2. Find your team id: Xcode → Settings → Accounts.
3. Build and install:
```bash
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile \
  -destination 'generic/platform=iOS' -derivedDataPath /tmp/raven-ios-device \
  -allowProvisioningUpdates DEVELOPMENT_TEAM=<TEAM_ID> PRODUCT_BUNDLE_IDENTIFIER=ai.raven.mobile.<yourname> build
xcrun devicectl device install app --device <device-id> \
  /tmp/raven-ios-device/Build/Products/Debug-iphoneos/RavenMobile.app
```
4. In the app: Settings → server `http://<mac-ip>:8787` (`ipconfig getifaddr en0`). Phone and Mac must be on
   the same network; venue Wi-Fi often isolates clients — use the phone's hotspot if it can't connect.

## 6. Automated tests

```bash
(cd raven-core && uv run pytest -q)                                   # 42 tests, no network
cd t3code && export PATH="$PWD/node_modules/.bin:$PATH"
npx vp run --filter @t3tools/provider-acp --filter t3 --filter @t3tools/web --filter @t3tools/desktop typecheck
npx vp test run packages/provider-acp/src/server/runtimeModel.test.ts apps/desktop/src/app/DesktopAppIdentity.test.ts
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile -destination 'generic/platform=iOS Simulator' build
```

## 7. Stop everything

Stop processes by the port they own (safer than killing by name):
```bash
for port in 8787 13773 5733; do p=$(lsof -nP -iTCP:$port -sTCP:LISTEN -t); [ -n "$p" ] && kill $p; done
# Raven Agent: quit the window normally (Cmd+Q); its launcher exits with it.
cua sb ls                                  # sandboxes keep running; stop with: cua sb rm raven-linux
colima stop                                # stops Docker (and the Linux sandbox)
```

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `HAI_API_KEY is not set` | key missing in repo-root `.env`; restart raven-core after editing |
| H session stays `queued` | plan limit (Free: 3 concurrent). Check: `uv run python -c "from raven_core.engine.holo import load_env;load_env();from hai_agents import Client;print(Client().sessions.get_session_quota())"`. Use `RAVEN_H_MODE=models-loop`, or ask H to release stuck slots |
| H desktop actions time out on Linux | missing `python3-tk` / `scrot` / `XDG_SESSION_TYPE=x11`; raven-core's Linux host installs them — rerun, or `docker exec -u root raven-linux apt-get install -y python3-tk scrot` |
| `no hai-agent-runtime release artifact for platform linux-arm64` | use `agents-api` or `models-loop` on the Linux container |
| Raven Agent windows keep opening | orphaned `dev-electron.mjs` from `dev:desktop`; stop it and use §4 instead |
| Devin provider "unauthenticated" | log in from Raven Agent's provider settings (not the CLI) |
| `A T3 Code server already owns …/.t3/userdata` | another Raven Agent / dev server uses that home; stop it (§7) |
| iPhone app shows "Waiting for raven-core" | wrong server URL, raven-core bound to localhost, or client isolation on Wi-Fi |
| Commit blocked by a vite hook | `git config --unset core.hookspath` (t3code's install no longer sets it) |
