# Raven — build, run and test

## Everyday use (one click)

Open **Raven Agent** from Spotlight / Launchpad (`~/Applications/Raven Agent.app`), or run `raven` in a
terminal. That's it:

- Raven Agent starts raven-core as a sidecar unless a healthy server is already running. Packaged
  launches run `uv sync --frozen` when needed; the app probes `/health`, restarts failed children
  with backoff, and stops its sidecar on quit.
- Raven Agent is built on first launch (~20 s) and opened as a single window.
- Sidebar **Execution → Save for next run** controls the core-owned profile without a restart.
  Active agents/runs keep their captured settings; **Check readiness** is read-only inventory,
  not a live H test.
- macOS is native; iOS uses the exact Device Hub simulator/connected device selected in Execution.
  Grant Screen Recording and Accessibility. They share the Mac's screen lock; Raven hides its
  window during computer use and models-loop shows an agent pointer.
- Linux tries the existing Apple Container Space `raven-linux-apple` first, never Docker/Colima.
  One announced Devin Linux VM fallback is allowed only for genuine runtime incompatibility or
  an unavailable desktop, not H/auth/assertion errors. Windows uses only real Devin Windows
  VMs, subject to organization access.
- Only local Spaces Raven created/resumed are eligible for idle suspension after
  `RAVEN_IDLE_SUSPEND_MIN` minutes (default 20). Missing/broken Spaces are not silently replaced.

```bash
raven            # open (starts raven-core, builds if needed)
raven status     # raven-core, Raven Agent, machines
raven rebuild    # after pulling code changes
raven stop       # stop Raven Agent + raven-core (machines keep their state)
```
Set keys in Raven Agent → Settings → Raven; set execution defaults in the sidebar. Keys are encrypted
with Electron `safeStorage` (macOS Keychain-backed) in `<userData>/raven/secrets.bin`; non-secret settings are in
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
  └─ test targets     (native Mac/Device Hub, Apple Linux Space, Devin Linux/Windows VMs)
        └─ Devin plans; H Company computer use (Holo4) clicks and types
```

## 0. One-time setup

### Tools
```bash
brew install uv node pnpm gh
curl -fsSL https://cua.ai/install.sh | sh -s -- --cli-only      # Cua CLI → ~/.local/bin/cua
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && exec zsh
```
Xcode 27 or later with Device Hub and an iOS runtime is needed for iOS. Install/authenticate
the Devin CLI for local planning and Devin VM SSH. Run it normally in the target repository
to resolve workspace trust; Raven does not bypass that prompt. No Docker/Colima setup is needed.

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
| `DEVIN_API_KEY` | v3 cloud planning and Devin VM lifecycle (service-user/PAT credential) | app.devin.ai → Settings |
| `DEVIN_ORG_ID` | cloud planning and Devin Linux/Windows VMs | your enabled Devin organization |

Local planning uses the authenticated CLI, not `DEVIN_API_KEY`. Configure the non-secret
`DEVIN_ORG_ID` in raven-core's active environment file; it is not currently a Settings field.
Devin VMs also require CLI SSH access; Windows must be enabled for the organization.

Check which keys are set (names only):
```bash
grep -oE '^[A-Z_]+=.' .env | sed 's/=.$/ is set/'
```

### Dependencies
```bash
(cd raven-core && uv sync)
(cd t3code && pnpm install)
```

## 1. Select and prepare targets

### Linux — existing Apple Container Space first
`raven-linux-apple` must be a local Cua `microvm` with runtime `containerization`.
Normal runs may resume this Space and prepare H's harness, but never implicitly create or
replace it. Read-only inventory and an explicit local-only preparation command:
```bash
cua --json sb ls --local --no-size
(cd raven-core && uv run python scripts/sandbox_setup.py linux --no-fallback)
```
Only if you deliberately want to create a **missing** Space, add `--create` to that setup
command; it is not a connection-repair/reprovision step. The setup script uses its own
`--mode` (default `auto`) and `--space` options, not the server's saved execution profile.
Install/build/launch the application under test using its own documented setup; Raven's
H bootstrap does not deploy it. Use `cua sb view local:raven-linux-apple` to view the Space.

With profile `linux_fallback: true`, eligible preflight failures announce **one** switch to
a Devin Linux VM before suite setup. H mode stays unchanged; there is no mid-test switch.
Missing credentials, H errors/quota/latency, assertion failures and unclassified transport
errors do not justify fallback. Disable fallback in Execution to diagnose the local Space.

### Native macOS, Windows, and iOS
- **macOS:** this Mac, not a VM. Profiles override legacy `RAVEN_PLACEMENT`/host hints.
- **Windows:** `windows_target` accepts only `devin-vm`. Raven requests a real Windows VM
  through Devin v3, then uses authenticated CLI SSH. It needs org platform access and a usable
  guest desktop with Python/uv. Missing credentials, CLI access or platform enablement block
  Windows testing; there is no alternate Windows backend.
- **iOS:** refresh `/execution/devices` or the sidebar's Device Hub list, then save
  `ios_target: simulator | device` and its exact `ios_device_id`. Physical devices require
  an ID, pairing/trust, Developer Mode and unlock. With no ID, only a single available
  simulator is allowed; multiple simulators block rather than selecting `booted`/foreground.
  Preparation verifies the ID/type and Device Hub screen URL; it never substitutes a phone
  for a simulator or silently selects another device.

## 2. Start raven-core

Raven Agent starts and supervises its own sidecar; no manual server is needed. Its managed ACP
bridge waits for that server. Standalone ACP clients can start a detached local daemon unless
`RAVEN_ACP_DAEMON=0` or `RAVEN_ACP_SERVE=0`.

For manual debugging, run a separate server instead:
```bash
cd raven-core
uv run raven-core serve --host 0.0.0.0 --port 8787
```
Leave it running in its own terminal. Health check: `curl localhost:8787/health` → `{"ok":true}`.
The desktop sidecar and the CLI default to binding `0.0.0.0`; remote clients need the configured
`RAVEN_TOKEN` or the generated pairing token, while local requests are trusted. `/pairing` is
loopback-only. To restrict a manual server to this Mac, pass `--host 127.0.0.1`.

`raven_core/env.py` loads one env file: `RAVEN_ENV_FILE`, else `RAVEN_DATA_DIR/raven.env`, else
repo-root `.env`. `RAVEN_DATA_DIR` also changes artifact defaults; `RAVEN_SPECS_DIR` overrides specs.

### Execution defaults, not legacy H environment saves
Core persists the profile in `<artifacts-directory>/execution.json` (repo default
`artifacts/execution.json`). Sidebar **Save for next run** uses `PUT /execution`; existing
agents/runs keep their snapshots. Request-level `execution` or `model` overrides are also
captured per run. No restart is needed. Old Settings H-mode/model saves are rejected.
`RAVEN_H_MODE`, `HAI_MODEL`, `RAVEN_PLANNER_MODE`, `RAVEN_LINUX_SANDBOX` seed a missing profile only.

`GET /execution` returns `{profile, targets}`. `PUT /execution` accepts the **complete profile**,
not a patch. To change just the Linux H mode while preserving other settings:
```bash
curl -fsS localhost:8787/execution |
  jq '.profile | .platform_modes.linux = "models-loop"' |
  curl -fsS -X PUT localhost:8787/execution -H 'content-type: application/json' --data-binary @-
curl -fsS -X POST localhost:8787/execution/check
curl -fsS localhost:8787/execution/devices
```
Checks do not create/resume machines, boot devices or call H. `unchecked` is not ready;
`ready` means a matching worker passed local preflight, not that H authentication or a QA
test passed. Read the detail before running.

H mode is independent of hardware (an Apple Space or Devin VM is a host, not a decision loop):

| Value | What runs | Notes |
|---|---|---|
| `auto` (default) | Linux/Windows: `agents-api`; macOS/iOS: `local-runtime` | per-platform `platform_modes` overrides supported |
| `local-runtime` | H's official loop in the test machine | Linux ARM64 runtime is unavailable; an eligible Linux VM fallback retains this mode |
| `agents-api` | H hosts the loop; its desktop bridge controls the selected machine | subject to H session quota |
| `models-loop` | Raven's loop calls Holo Models API and uses Cua/native input/screenshot drivers | explicit fallback; no Agents API session quota |

Profile `model` defaults to `holo4-27b`; the sidebar also offers `holo4-35b-a3b`.
Profile `planner` is `local` (default) or `cloud`, never disabled. Free-form tasks require a
validated Devin plan before H executes; named YAML suites keep their predefined checks.

Fake-worker debugging: `RAVEN_FAKE_WORKERS=1 uv run raven-core serve` supplies fake screens,
but agents still call Devin/H and can spend credits. Use mocked tests for zero-provider-cost checks.

## 3. Test from the terminal

### One agent, followed live
```bash
cd raven-core
uv run raven-core agent linux "Open a terminal window and run: uname -a. Pass if the Linux kernel line is visible."
```
Prints every action and screenshot path, then `PASSED`/`FAILED`; exit code 0 = passed.
Ctrl+C stops the agent. Screenshots: `<artifacts-directory>/<agent-id>/NNN.png`.
The CLI agent command has no repo flag; use the target repo as the ACP project cwd or pass
`repo_path` to `POST /agents` for code-aware planning and repair.

bitchat example (start bitchat first, see §1):
```bash
uv run raven-core agent linux 'In the bitchat window, click the "Type a message" box, type /j #u09t and press Enter. Then type: hello from raven and press Enter. Pass if the message appears in the channel.'
```

### A whole test suite (several platforms in parallel)
```bash
uv run raven-core run bitchat-geohash-interop --platforms linux,macos,windows
```
Suites live in `specs/*.yaml`. This command runs in-process with the environment/artifact
directory of that terminal; `POST /runs` uses the running server's profile instead.
`bitchat-geohash-interop` currently declares a macOS sender and requires Linux **and Windows**
replies; prepare all three targets. A Windows sender requires an explicit suite change,
not a host switch.

### Raw API (curl)
```bash
curl -s localhost:8787/specs | jq
curl -s -X POST localhost:8787/agents -H 'content-type: application/json' \
  -d '{"platform":"linux","task":"Open Firefox and go to example.com. Pass if the title is Example Domain.","repo_path":"/path/to/target-repo"}' | jq
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
3. Sidebar **Execution** chooses the planner, H loop/model and targets for future runs.
   The separate **Devin** / **Devin Cloud** providers are not required to insert planning into Raven.

## 5. Build and run RavenMobile (iOS)

### Simulator
```bash
SIM_ID="<exact simulator UUID from /execution/devices>"
xcrun simctl bootstatus "$SIM_ID" -b
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile \
  -destination "platform=iOS Simulator,id=$SIM_ID" -derivedDataPath /tmp/raven-ios-build build
xcrun simctl install "$SIM_ID" /tmp/raven-ios-build/Build/Products/Debug-iphonesimulator/RavenMobile.app
xcrun simctl launch "$SIM_ID" ai.raven.mobile -demo NO -server http://localhost:8787
```
`-demo YES` runs a fully simulated backend (no keys needed). Screenshot:
`xcrun simctl io "$SIM_ID" screenshot /tmp/ios.png`. To use this as an iOS test target,
save the same ID with `ios_target: simulator`; Raven opens its verified Device Hub screen.

### Your iPhone
1. Connect/pair it in Device Hub, trust this Mac, enable Developer Mode and unlock the phone.
2. Find your team id: Xcode → Settings → Accounts.
3. Build, install, and launch without the debugger:
```bash
export DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer
DEVICE_ID="<exact physical-device ID from /execution/devices>"
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile \
  -destination "platform=iOS,id=$DEVICE_ID" -derivedDataPath /tmp/raven-ios-dd-dev \
  -allowProvisioningUpdates build
xcrun devicectl device install app --device "$DEVICE_ID" \
  /tmp/raven-ios-dd-dev/Build/Products/Debug-iphoneos/RavenMobile.app
xcrun devicectl device process launch --terminate-existing --device "$DEVICE_ID" ai.raven.mobile
```
4. In Raven Agent → Settings → Raven, scan the pairing QR code with the iPhone app to fill its URL
   and token. On cellular, turn on Tailscale on the iPhone and Mac. Venue Wi-Fi may isolate clients.
5. Only if testing this phone, save `ios_target: device` and the same `ios_device_id` in Execution.
   Pairing RavenMobile as a controller does not select it as a test target.

These commands install RavenMobile, not the application under test; deploy that app separately.

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
yours. Stop a separately started manual server with Ctrl+C in its terminal. Local Spaces keep their
state; do not remove/reprovision one to recover from a connection failure. The Devin adapter archives
only sessions it created (requires `ManageOrgSessions`), not explicitly attached sessions.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `HAI_API_KEY is not set` | set it in Settings → Raven, or in repo-root `.env` for terminal use; restart raven-core |
| H session stays `queued` | check the H plan/session quota; explicitly save `models-loop` in Execution if appropriate. H quota/latency errors do not justify switching machines |
| H desktop preflight fails on Linux | check the selected Apple Space's GUI session and native-driver dependencies (`python3-tk`, `scrot`, `XDG_SESSION_TYPE=x11`); missing dependencies are blockers, not application bugs |
| `no hai-agent-runtime release artifact for platform linux-arm64` | explicitly select `agents-api`/`models-loop`, or allow the single preflight fallback to a compatible Devin Linux VM without changing `local-runtime` |
| Apple Space reports `No route to host` | the desktop is unavailable even if Cua lists the Space as running. Diagnose connectivity; do not delete/reprovision it or claim a smoke pass |
| Devin VM blocked / Windows access denied | check `DEVIN_API_KEY`, `DEVIN_ORG_ID`, CLI authentication/SSH and organization platform access; Windows testing stays blocked until these are available |
| Local planning blocked before H starts | authenticate Devin CLI and resolve trust in the target-repo cwd; inspect the planning verdict. Do not bypass trust/permissions or execute the raw task |
| iOS selection blocked | refresh `/execution/devices`, choose matching kind/exact ID, and check pairing/trust, Developer Mode, unlock and the Device Hub screen URL |
| Raven Agent windows keep opening | orphaned `dev-electron.mjs` from `dev:desktop`; stop it and use §4 instead |
| Devin login loop | Raven Agent strips `ACP_BACKEND`; launch it from Applications |
| `A T3 Code server already owns …/.t3/userdata` | another Raven Agent / dev server uses that home; stop it (§7) |
| iPhone gets 401 / appears offline | token may be missing; scan the QR in Settings → Raven. Also check network reachability |
| `libobjc.A.dylib is being read from process memory` | Xcode's shared-cache symbol copy is missing/incomplete (~6 GB in `~/Library/Developer/Xcode/iOS DeviceSupport`); unlock the phone and let it finish in Window → Devices and Simulators, or run without the debugger |
| ATS “requires a secure connection” | `Info.plist` must not set `NSAllowsLocalNetworking` next to `NSAllowsArbitraryLoads` |
| Screen permissions lost after `brew upgrade` | pin Python with `brew pin python@3.12` |
| Commit blocked by a vite hook | `git config --unset core.hookspath` (t3code's install no longer sets it) |
| Screenshot, then nothing until the suite times out | a Holo request stalled. The timeline now says so: `Holo request N still pending after 15 s`, then `timed out after 45s … retrying (attempt 2/3)`; the verdict names the request/step it was waiting on. Lower `HAI_REQUEST_TIMEOUT_S` or check `api.hcompany.ai` reachability |

## Real Holo desktop smoke suite

In sidebar Execution, select Apple Container Space `raven-linux-apple`, save the Linux H mode
you intend to test (`agents-api` under `auto`, or explicitly `models-loop`), then check readiness.
No restart is required. To keep this check local-only, disable Linux VM fallback.
With the Raven provider, send `/run holo-linux-smoke on linux`. The suite opens a terminal on
the resolved Linux target, generates a fresh code per attempt, and asks H to type it and verify
the result on screen. It requires a working desktop, `xfce4-terminal`, `setsid`, and an H key.
Setup and execution use the same prepared worker. Use one suite at a time on that desktop.

A suite can opt into up to three total attempts with `attempts:` in its YAML. Use this only
when repeating setup and all selected test actions is safe. The smoke suite uses two
attempts. Setup failures, session errors, missing verdicts and timeouts can trigger a retry.
An observed application assertion failure ends the suite and keeps the evidence. These are
QA-attempt retries, not permission to switch targets after an H error or assertion failure.
The suite logs the attempt number and error reason in Raven Agent and in its run events.
After a final failure, open `<artifacts-directory>/<run-id>/failure.md` for the timeline, verdicts,
recent actions, and screenshot paths. The notes are written locally before external research.

### Deadlines and the Holo request log (models-loop)

Deadlines are layered so a stall is attributable to one layer:

| Layer | Setting | Default | Ends with |
|---|---|---|---|
| one Holo request | `HAI_REQUEST_TIMEOUT_S` (× `HAI_REQUEST_ATTEMPTS`) | 45 s, 3 attempts | `Holo request N timed out after 45s …; retrying (attempt 2/3)`, then `Holo did not answer: …` |
| one agent | the suite's `timeout_s` (free-form agents: `RAVEN_AGENT_TIMEOUT_S`) | spec value / 600 s | `Timed out after 75s before reaching a verdict while waiting for Holo request #2 (step 2, …)` |
| one QA attempt | `timeout_s` + 60 s grace + sender delay (runner backstop) | | `QA attempt exceeded 155s while waiting for computer use` |
| the whole run | `RAVEN_RUN_TIMEOUT_S` | 900 s | `Run exceeded its 900s time limit …` |

Free-form planning, target preparation and repair have separate caps:
`RAVEN_PLANNING_TIMEOUT_S=90`, `RAVEN_PREPARE_TIMEOUT_S=300`, `RAVEN_FIX_TIMEOUT_S=300`.

A request's deadline is always clamped to what is left of the agent's budget, and no request is
sent with less than 5 s left. The SDK's own retries are off; Raven retries itself so every attempt
shows up. Each request is logged to the raven-core log when sent (`holo #3 sent: step 2 attempt 1/3
model=holo4-27b images=2 limit=45s`) and answered (`holo #3 answered in 3.5s: click`), and to the
run timeline as system messages: `Holo loop on holo4-27b: 75s budget, 45s per request, …` at the
start, `Holo answered request 3 in 3.5 s: click` per step, `Holo request 3 still pending after 15 s`
when it is slow, and the retry / give-up lines above. A stop that did not come from the user keeps
its origin (`QA attempt exceeded … while waiting for Holo request #2 …`) instead of `Stopped by user`.

### Which layer controls the desktop?

- Core owns profiles, planning, preparation, timelines and artifacts. Devin plans/fixes;
  H alone makes computer-use decisions in all three execution modes.
- Cua supplies the Apple Space and `cua sb exec --stdin` transport. Devin VMs use CLI SSH.
  H's official modes use the target's native desktop bridge; Models fallback dispatches
  through Cua/native workers. macOS and Device Hub use this Mac's native screen.

## Planning and repair with Devin

Free-form requests must produce a validated **Setup / Task / Expected Result / Verification**
plan with an alternate route or blocker before H runs. No plan means blocked, not raw H execution.
Named YAML suites keep their predefined checks; the smoke suite does not require a new planning turn.

- **Local:** authenticated Devin CLI ACP, using the trusted target-repo cwd (`repo_path` over HTTP,
  project cwd over ACP). Raven uses `--config`, `--permission-mode normal`,
  `--respect-workspace-trust true`, and ACP Plan mode. Optional `DEVIN_CLI` and
  `DEVIN_PLANNING_MODEL` (default `DEVIN_MODEL` or `opus`) configure the planner.
  Isolated configs/session mappings live under `(RAVEN_DATA_DIR or artifacts)/devin/`;
  Raven does not search or resume unrelated personal CLI sessions.
- **Cloud:** v3 `DEVIN_API_KEY` + `DEVIN_ORG_ID`. A completed, fresh structured plan is required;
  session creation alone is not success. User-input/approval blockers are not bypassed.

After an application assertion fails, send **`fix and retry`** in the same Raven ACP thread.
HTTP callers create an agent with the original `platform`, `task`, `repo_path`,
`"previous_agent_id":"<failed-agent-id>"` and `"repair":true`. This resumes the saved planning
session; a named-suite failure or an infrastructure/planning/auth/timeout failure is not a repair target.
Normal retries retain the original expected results and verification; repair retains the entire plan.

Local repair operations request ACP `session/request_permission`. For an HTTP client, watch
`agent.message` events with `phase: approval`, then:
- `GET /agents/{id}/approvals/{approval_id}` to inspect the full command/file changes and options.
- `POST` to that path with `{"option_id":"<offered allow_once optionId>"}` to allow once,
  or `{"option_id":null}` to deny. Only allow-once/deny is supported; do not approve from a title alone.

Plans, failure context and repair reports are saved under `<artifacts-directory>/<agent-id>/`
as `plan.json`, `failure.json`, and (after a repair) `fix.json`. A repair report is not a pass.
H must rebuild/relaunch using the application's documented setup and execute the **original checks
against the updated app**. If the repaired source/build is unavailable on the target, H must block.
Raven has no generic automatic deployment path for arbitrary applications.

### Recorded verification limits (not a fresh live pass)

- Recorded checks saw the existing `raven-linux-apple` as running in read-only metadata, but
  preparation, screenshot capture and direct Cua exec failed: guest `192.168.64.2:3211` was
  unreachable (`No route to host`). No H request or action occurred.
  No machines were stopped, deleted, or reprovisioned.
- Presence checks (no values): `HAI_API_KEY` and `DEVIN_API_KEY` present; `DEVIN_ORG_ID` absent.
  Browser-panel tools were unavailable; no visual verification was performed.
- Focused tests: **51 passed**. Earlier broad run: **388 passed**, with a mock failure subsequently
  fixed. The full final run is still pending; these are recorded results, not rerun by this docs update.
