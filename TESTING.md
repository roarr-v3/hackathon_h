# Raven — build, run and test

## Everyday use (one click)

Open **Raven Agent** from Spotlight / Launchpad (`~/Applications/Raven Agent.app`), or run `raven` in a
terminal. That's it:

- Raven Agent starts raven-core as a sidecar unless a healthy server is already running. Packaged
  launches run `uv sync --frozen` when needed; the app probes `/health`, restarts failed children
  with backoff, and stops its sidecar on quit.
- Raven Agent is built on first launch (~20 s) and opened as a single window.
- On this Mac, macOS runs use the Mac's screen by default; grant Screen Recording and Accessibility.
  One agent uses the screen at a time and Raven hides its window; models-loop shows an agent pointer.
  Ask for a VM or set `RAVEN_PLACEMENT=vm` to use a Cua macOS VM instead.
- Test machines start **when needed**: Docker (colima) and the Cua Linux container for `on linux: …`,
  or the Cua macOS VM when requested. Cua sandboxes try to open a live viewer by default.
- Machines nobody used for `RAVEN_IDLE_SUSPEND_MIN` minutes (default 20) are suspended to free RAM.

```bash
raven            # open (starts raven-core, builds if needed)
raven status     # raven-core, Raven Agent, machines
raven rebuild    # after pulling code changes
raven stop       # stop Raven Agent + raven-core (machines keep their state)
```
Set keys and settings in Raven Agent → Settings → Raven. Keys are encrypted with Electron
`safeStorage` (macOS Keychain-backed) in `<userData>/raven/secrets.bin`; non-secret settings are in
`<userData>/raven/raven.env` (normally `~/Library/Application Support/raven-agent/raven/raven.env`).
Repo-root `.env` is the terminal/dev fallback. Dev logs: `artifacts/logs/`; packaged sidecar logs:
`<userData>/raven/artifacts/logs/`. Install the launcher: `ln -sf "$PWD/bin/raven" ~/.local/bin/raven`.

Everything below is the manual / debugging path.

---

Everything below runs from a terminal on the Mac (Apple silicon, macOS 26). Paths are relative to the
repo root `hackathon_h/` unless stated otherwise.

```
raven-core (Python, :8787)  ← the brain: API, agents, voice, reports
  ├─ Raven Agent      (t3code desktop app)   talks to it over ACP
  ├─ RavenMobile      (iOS app)              talks to it over HTTP/WebSocket
  └─ test machines    (this Mac, Cua Linux/macOS sandboxes, Windows home PC)
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
For Raven Agent, use Settings → Raven. For terminal development, create the fallback file:
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

### macOS (optional Cua Lume VM) — first pull is ~23 GB
```bash
cua sb create macos --name raven-mac      # later creates clone in ~25 s
```

### Watch a machine live
```bash
cua sb view raven-linux                   # opens the HTML5 viewer in your browser
cua sb screenshot raven-linux -o /tmp/linux.png
```

## 2. Start raven-core

Raven Agent starts and supervises its own sidecar; no manual server is needed. Its managed ACP
bridge waits for that server. Standalone ACP clients can start a detached local daemon unless
`RAVEN_ACP_DAEMON=0` or `RAVEN_ACP_SERVE=0`.

For manual debugging, run a separate server instead:
```bash
cd raven-core
RAVEN_H_MODE=models-loop uv run raven-core serve --host 0.0.0.0 --port 8787
```
Leave it running in its own terminal. Health check: `curl localhost:8787/health` → `{"ok":true}`.
The desktop sidecar and the CLI default to binding `0.0.0.0`; remote clients need the configured
`RAVEN_TOKEN` or the generated pairing token, while local requests are trusted. `/pairing` is
loopback-only. To restrict a manual server to this Mac, pass `--host 127.0.0.1`.

`raven_core/env.py` loads one env file: `RAVEN_ENV_FILE`, else `RAVEN_DATA_DIR/raven.env`, else
repo-root `.env`. `RAVEN_DATA_DIR` also changes artifact defaults; `RAVEN_SPECS_DIR` overrides specs.

`RAVEN_H_MODE` picks how H drives the machine:

| Value | What runs | Notes |
|---|---|---|
| `models-loop` | Holo4 via H's Models API + Raven's loop | **use this now** — no session quota |
| `agents-api` | H's managed `h/holo-desktop` agent | counts against the plan's 3 concurrent sessions |
| `local-runtime` | H's agent loop inside the machine | not available for linux-arm64 containers |

Model: `HAI_MODEL=holo4-27b` (most capable) or `holo4-35b-a3b` (fastest/cheapest), in Settings → Raven
or the terminal's `.env`.

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
- The app starts raven-core (§2); the Raven provider connects to `http://localhost:8787`.

In the app:
1. New thread → provider **Raven** → type `help`, `specs`, `status`, `stop`, or `on linux: <task>`.
2. Mic button (or `Cmd+Shift+D`) → speak → text lands in the composer → Enter.
3. Providers **Devin** / **Devin Cloud**: use Settings → Providers to configure or log in.

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
3. Build, install, and launch without the debugger:
```bash
export DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile \
  -destination 'generic/platform=iOS' -derivedDataPath /tmp/raven-ios-dd-dev \
  -allowProvisioningUpdates build
xcrun devicectl device install app --device <udid> \
  /tmp/raven-ios-dd-dev/Build/Products/Debug-iphoneos/RavenMobile.app
xcrun devicectl device process launch --terminate-existing --device <udid> ai.raven.mobile
```
4. In Raven Agent → Settings → Raven, scan the pairing QR code with the iPhone app to fill its URL
   and token. On cellular, turn on Tailscale on the iPhone and Mac. Venue Wi-Fi may isolate clients.

## 6. Automated tests

```bash
(cd raven-core && uv run pytest -q)
cd t3code && export PATH="$PWD/node_modules/.bin:$PATH"
npx vp run --filter @t3tools/provider-acp --filter t3 --filter @t3tools/web --filter @t3tools/desktop typecheck
npx vp test run packages/provider-acp/src/server/runtimeModel.test.ts apps/desktop/src/app/DesktopAppIdentity.test.ts
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile -destination 'generic/platform=iOS Simulator' build
```

## 7. Stop everything

Quit Raven Agent normally (Cmd+Q); its sidecar stops with it. `raven stop` also stops a raven-core
listener on the configured port if its command contains `raven`, so use it only when that listener is
yours. Stop a separately started manual server with Ctrl+C in its terminal. Sandboxes keep their state:
```bash
cua sb ls                                  # sandboxes keep running; stop with: cua sb rm raven-linux
colima stop                                # stops Docker (and the Linux sandbox)
```

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `HAI_API_KEY is not set` | set it in Settings → Raven, or in repo-root `.env` for terminal use; restart raven-core |
| H session stays `queued` | plan limit (Free: 3 concurrent). Check: `uv run python -c "from raven_core.engine.holo import load_env;load_env();from hai_agents import Client;print(Client().sessions.get_session_quota())"`. Use `RAVEN_H_MODE=models-loop`, or ask H to release stuck slots |
| H desktop actions time out on Linux | missing `python3-tk` / `scrot` / `XDG_SESSION_TYPE=x11`; raven-core's Linux host installs them — rerun, or `docker exec -u root raven-linux apt-get install -y python3-tk scrot` |
| `no hai-agent-runtime release artifact for platform linux-arm64` | use `agents-api` or `models-loop` on the Linux container |
| Raven Agent windows keep opening | orphaned `dev-electron.mjs` from `dev:desktop`; stop it and use §4 instead |
| Devin login loop | Raven Agent strips `ACP_BACKEND`; launch it from Applications |
| `A T3 Code server already owns …/.t3/userdata` | another Raven Agent / dev server uses that home; stop it (§7) |
| iPhone gets 401 / appears offline | token may be missing; scan the QR in Settings → Raven. Also check network reachability |
| `libobjc.A.dylib is being read from process memory` | Xcode's shared-cache symbol copy is missing/incomplete (~6 GB in `~/Library/Developer/Xcode/iOS DeviceSupport`); unlock the phone and let it finish in Window → Devices and Simulators, or run without the debugger |
| ATS “requires a secure connection” | `Info.plist` must not set `NSAllowsLocalNetworking` next to `NSAllowsArbitraryLoads` |
| Screen permissions lost after `brew upgrade` | pin Python with `brew pin python@3.12` |
| Commit blocked by a vite hook | `git config --unset core.hookspath` (t3code's install no longer sets it) |
