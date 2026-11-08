# Raven — cross-platform testing with computer-use agents

Hackathon project (H Company × 42AI, Paris, Nov 7–8 2026; submission Sun 14:00).
Raven = H Company's official software-QA loop (agents-api-demos/examples/software_qa),
made cross-platform and voice-driven. Demo target: bitchat clients over Nostr geohash
channels (VMs have no Bluetooth, so never rely on BLE mesh).

## Workflow (Devin is mandatory in every configuration)
1. Voice (Gladia) or text from t3code / iPhone → prompt.
2. **Devin plans before free-form execution**: uses the target repo + prompt to produce a
   validated QA task in H's format (Setup / Task / Expected Result / Verification, with
   an alternate route or blocker; report starts with `VERDICT: PASSED|FAILED`).
   Planning failure blocks H; named YAML suites retain their predefined checks.
3. **H Company does all computer use**: H's decision loop executes the checks on the
   selected targets. Cua/native drivers provide machines, input and screenshots, not decisions.
4. On failure: **Linkup** retrieves external context (web search only; it never sees our code).
5. Explicit **fix and retry** resumes the saved Devin planning session for a failed free-form
   application check. Local mutations need per-operation allow-once approval or are denied.
   Infrastructure failures block application repair. H reruns the original checks after repair;
   if the updated source/build is not deployed to the target, it must report a blocker.
   Raven does not automatically deploy arbitrary apps or weaken expected results/verification.

Demo bug placement: the Windows client is unmodified; a receive-side bug is
planted in Swift bitchat (macOS + iOS) and bitchat-tui (Linux). Sources in `targets/`
(gitignored, separate repos). Named YAML determines sender/receiver roles;
`bitchat-geohash-interop` currently uses a macOS sender.

## Execution profile (core-owned)
The sidebar **Execution → Save for next run** and `GET/PUT /execution` use
`<artifacts-directory>/execution.json` (repo default: `artifacts/execution.json`).
Changes need no restart; each agent/run captures its own profile. Request-level `execution`
or `model` overrides affect that snapshot. Old Settings H-mode/model saves are rejected.
`RAVEN_H_MODE`, `HAI_MODEL`, `RAVEN_PLANNER_MODE` and `RAVEN_LINUX_SANDBOX` only seed a missing profile.
- `planner: local | cloud` (default `local`), never none. Local uses authenticated Devin CLI ACP,
  a trusted target-repo cwd and isolated Raven-owned configuration/session metadata. Cloud uses
  v3 `DEVIN_API_KEY` + `DEVIN_ORG_ID`; selecting a separate t3code provider is not required.
- `h_mode: auto | local-runtime | agents-api | models-loop`, plus `platform_modes` overrides.
  `auto` resolves to `agents-api` for Linux/Windows and `local-runtime` for macOS/iOS.
  Local runtime runs H's loop in the test machine; Agents API hosts H's loop with a bridge to
  that machine; Models fallback runs Raven's Holo4 loop with Cua/native drivers.
- `model` defaults to `holo4-27b`; `holo4-35b-a3b` is also offered.
- Linux: `linux_target: apple-container`, `linux_space: raven-linux-apple` first; no Docker/Colima.
  `linux_fallback: true` permits one announced Devin Linux VM fallback at preflight only for
  genuine runtime incompatibility/unavailable desktop, never H/auth/assertion errors.
  It keeps the H mode unchanged. `devin-vm` can also be explicitly selected.
- Windows: `windows_target` accepts only `devin-vm`: a real Devin Windows VM, gated by
  organization access, v3 credentials and authenticated CLI SSH. Missing access is a blocker,
  not a reason to select another backend.
- macOS: native on this Mac. iOS: native Device Hub with `ios_target: simulator | device`
  and exact `ios_device_id` from `/execution/devices`. Physical devices require an explicit ID;
  omitting it is valid only for a single available simulator, never the arbitrary foreground device.
`POST /execution/check` is read-only inventory, not a live H test: no provisioning or device boot.

## Layout (ownership boundaries — do not edit outside your area)
- `raven-core/` Python 3.12, uv. Orchestrator + HTTP/WS API + ACP bridge.
  - `src/raven_core/events.py` — **the wire contract**. Shared by everything.
  - `engine/` agent loop (Holo4), `workers/` platform drivers, `runner.py` spec runner.
  - `server/` FastAPI app, WS event bus, Gladia voice session, artifacts.
  - `acp/` ACP stdio agent so t3code can drive Raven via its "ACP → Local command" provider.
- `t3code/` vendored t3code nightly `v0.1.8-nightly.20261107.4314` (MIT). Never rebase.
  Raven desktop = t3code + Raven ACP agent + Gladia voice button in the composer.
- `ios/RavenMobile/` SwiftUI iPhone app (all-black, agents as chat threads, voice).
- `specs/` YAML test suites.
- `docs/hackathon/` sponsor PDFs (reference only).

## raven-core API (default `http://localhost:8787`)
The desktop sidecar binds `0.0.0.0`. Remote requests require `Authorization: Bearer <token>`
(WS: `?token=`); local requests are trusted. The token is `RAVEN_TOKEN` or a generated, persisted
pairing token. `/health` is unauthenticated.
- `GET  /health` → `{"ok": true}`
- `GET  /pairing` → token and reachable URLs (loopback only)
- `GET/PUT /execution` → `{profile, targets}`; PUT accepts a complete `ExecutionProfile`
- `POST /execution/check` → `{profile, targets}` with read-only readiness details
- `GET  /execution/devices` → Device Hub inventory (`id`, `name`, `kind`, `available`)
- `GET  /agents` → `Agent[]` · `POST /agents` `CreateAgentRequest` → `Agent`
  (`repo_path` for planning; `repair: true` + `previous_agent_id` to resume a failed free-form check)
- `GET  /agents/{id}` → `{"agent": Agent, "events": Event[]}`
- `POST /agents/{id}/messages` `SendMessageRequest` (steer a running agent; a reply to a finished agent
  re-runs it as a new agent with the earlier task as context) → `{ok, delivered, agent_id}`.
  Free-form prompts naming Xcode or Device Hub run on macOS (those are Mac apps), even if they mention an iPhone.
- `POST /agents/{id}/stop`
- `GET/POST /agents/{id}/approvals/{approval_id}` — inspect a pending local repair operation,
  then submit `{"option_id": "<offered allow_once ID>"}` or `{"option_id": null}` to deny.
  ACP forwards these through `session/request_permission`; no allow-always.
- `GET  /runs` · `POST /runs` `CreateRunRequest` → `Run` · `GET /runs/{id}`
- `GET  /specs` → `[{name, description, platforms}]`
- `POST /voice/session` → `VoiceSession` (server calls Gladia `POST /v2/live` with
  `solaria-1`, pcm 16 kHz s16 mono, partial transcripts on; client connects to `url`
  directly and streams raw PCM; final transcripts get sent to an agent via /messages).
- `GET  /artifacts/{path}` screenshots (PNG).
- `WS   /ws/events` stream of `Event` JSON (replays last 200 on connect).

## Worker interface (Python Protocol, `workers/base.py`)
`screenshot() -> bytes(PNG)`, `size() -> (w,h)`, `click(x,y,button="left")`,
`double_click(x,y)`, `type_text(text)`, `key(combo)`, `scroll(x,y,dx,dy)`,
`shell(cmd) -> (rc, stdout, stderr)`. Targets include this Mac/Device Hub, the Cua Apple
Container Space, and Devin Linux/Windows VMs; Windows is Devin-only.
H's official modes use host adapters; `models-loop` dispatches through the worker interface.

## Everyday use
`bin/raven` (symlinked to `~/.local/bin/raven`, wrapped as `~/Applications/Raven Agent.app`) opens or
rebuilds Raven Agent, reports status, and stops it. Raven Agent owns a raven-core sidecar: packaged
launches run `uv sync --frozen` when needed, then `raven-core serve`; it probes `/health`, restarts
failed children with backoff, and stops its child on quit. An already healthy server is left external.
The managed ACP bridge waits for the app's server; standalone ACP clients can start a detached local
daemon unless `RAVEN_ACP_DAEMON=0` (or `RAVEN_ACP_SERVE=0`).

Grant Screen Recording and Accessibility for native macOS/Device Hub; one agent uses the Mac's
screen at a time, Raven hides its window, and models-loop shows an agent pointer. Execution profiles
override legacy placement/host env hints. `raven_core/machines.py` validates/resumes the existing
Apple Space; creation is explicit setup, never recovery from an auth/connection failure.
Only Spaces Raven created/resumed are eligible for idle suspension; none are implicitly replaced.
`raven_core/env.py` is the single env loader: `RAVEN_DATA_DIR` selects data/artifact defaults,
`RAVEN_SPECS_DIR` overrides specs, and `RAVEN_ENV_FILE` overrides the env file.
Deadlines are layered (models-loop): one Holo request (`HAI_REQUEST_TIMEOUT_S` 45 s × `HAI_REQUEST_ATTEMPTS` 3,
clamped to the agent's remaining budget) < one agent (the spec's `timeout_s`, passed as
`CreateAgentRequest.timeout_s`; free-form: `RAVEN_AGENT_TIMEOUT_S` 600 s) < runner backstop < `RAVEN_RUN_TIMEOUT_S`.
`engine/holo.py` logs every request (sent / answered in / retrying) as `agent.message` system events and
names the phase it was in when stopped or timed out; `StopSignal.set(reason)` carries the stop's origin.
Full manual steps: `TESTING.md`.

## Commands
- Python: `cd raven-core && uv run raven-core serve` · tests `uv run pytest`
- Cua inventory (read-only): `cua --json sb ls --local --no-size`.
  Explicit preparation from `raven-core/`: `uv run python scripts/sandbox_setup.py linux --no-fallback`;
  `--create` is an opt-in for a missing Apple Space, not a repair/reprovision command.
- t3code (Raven Agent desktop), Node 26 / pnpm 12, run from `t3code/` with `PATH=$PWD/node_modules/.bin:$PATH`:
  - build once: `npx vp run --filter @t3tools/desktop --filter t3 build`
  - launch (single process, no watcher): `T3CODE_HOME=$PWD/.t3 node apps/desktop/scripts/start-electron.mjs`
  - Avoid `dev:desktop` for demos: its `dev-electron.mjs` watcher relaunches Electron on every
    rebuild and orphaned watchers respawn windows in a loop. Never point at `~/.t3` (real data).
  - Headless E2E: `node scripts/dev-runner.ts dev:server --home-dir /tmp/raven-t3-test --no-seed`, then
    `node apps/server/src/bin.ts thread start --base-dir /tmp/raven-t3-test --provider raven --model holo4-27b --wait <project-id> "help"`.
- iOS: open `ios/RavenMobile/RavenMobile.xcodeproj` (Xcode 27).

## Secrets
Set keys in Raven Agent → Settings → Raven. Electron `safeStorage` encrypts them using macOS
Keychain-backed encryption in `<userData>/raven/secrets.bin`; non-secret settings live in
`<userData>/raven/raven.env` (normally `~/Library/Application Support/raven-agent/raven/raven.env`
on macOS). Repo-root `.env` remains the terminal/dev fallback (see `.env.example`). Never commit
or log keys.
