# Raven — cross-platform testing with computer-use agents

Hackathon project (H Company × 42AI, Paris, Nov 7–8 2026; submission Sun 14:00).
Raven = H Company's official software-QA loop (agents-api-demos/examples/software_qa),
made cross-platform and voice-driven. Demo target: bitchat clients over Nostr geohash
channels (VMs have no Bluetooth, so never rely on BLE mesh).

## Workflow (Devin is mandatory in every configuration)
1. Voice (Gladia) or text from t3code / iPhone → prompt.
2. **Devin plans** (frontier model): reads the target code + prompt, writes one short,
   precise QA spec per platform in H's format (Setup / Task / Expected Result /
   Verification; report starts with `VERDICT: PASSED|FAILED`).
3. **H Company does all computer use**: H's desktop agent (`holo`, `h/holo-desktop`)
   runs *inside each test machine* and executes the specs in parallel.
4. On failure: **Linkup** retrieves external context (web search only; it never sees our code).
5. **Devin fixes** the code (same session), then Raven rebuilds and reruns the same specs.

Demo bug placement: Windows (home PC) is the unmodified sender; a receive-side bug is
planted in Swift bitchat (macOS + iOS) and bitchat-tui (Linux). Sources in `targets/`
(gitignored, separate repos).

## Configuration (`raven.yaml`, every axis switchable)
- `devin: local | cloud` — local = Devin in t3code (ACP) / Devin CLI on this Mac;
  cloud = app.devin.ai sessions (Devin API). No "none" option.
- `hosts.<platform>`: linux `cua-local | devin-cloud`, macos `cua-local | devin-cloud`,
  ios `mac-simulator | devin-cloud`, windows `home-pc`.
- `h_mode: local-runtime | agents-api | models-loop` — local-runtime (`holo --local`)
  is default: loop runs in the test machine, only model calls hit H. agents-api = hosted
  loop (Free plan: 3 concurrent sessions, ~60 sessions/month — budget carefully).
  models-loop = our own Holo4 loop in `engine/` (fallback only).
- `model: holo4-27b | holo4-35b-a3b`.
Cua = machines (Lume macOS VM, Linux container) and, via H's own `hai-drivers[cua-cli]`,
a driver backend under H's agent. Cua never makes decisions.

## Layout (ownership boundaries — do not edit outside your area)
- `raven-core/` Python 3.12, uv. Orchestrator + HTTP/WS API + ACP bridge.
  - `src/raven_core/events.py` — **the wire contract**. Shared by everything.
  - `engine/` agent loop (Holo4), `workers/` platform drivers, `runner.py` spec runner.
  - `server/` FastAPI app, WS event bus, Gladia voice session, artifacts.
  - `acp/` ACP stdio agent so t3code can drive Raven via its "ACP → Local command" provider.
- `t3code/` vendored t3code nightly `v0.1.8-nightly.20261107.4314` (MIT). Never rebase.
  Raven desktop = t3code + Raven ACP agent + Gladia voice button in the composer.
- `ios/RavenMobile/` SwiftUI iPhone app (all-black, agents as chat threads, voice).
- `specs/` YAML test suites. `workers/windows/` setup scripts for the home Windows PC.
- `docs/hackathon/` sponsor PDFs (reference only).

## raven-core API (default `http://localhost:8787`)
Auth: if `RAVEN_TOKEN` is set, require `Authorization: Bearer <token>` (WS: `?token=`).
- `GET  /health` → `{"ok": true}`
- `GET  /agents` → `Agent[]` · `POST /agents` `CreateAgentRequest` → `Agent`
- `GET  /agents/{id}` → `{"agent": Agent, "events": Event[]}`
- `POST /agents/{id}/messages` `SendMessageRequest` (steer a running agent / voice command)
- `POST /agents/{id}/stop`
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
`shell(cmd) -> (rc, stdout, stderr)`. Implementations: Cua sandboxes (macOS Lume VM,
Linux Docker container) and remote cua-spacesd (Windows PC over Tailscale).

## Commands
- Python: `cd raven-core && uv run raven-core serve` · tests `uv run pytest`
- Docker runtime: `colima start` (installed via brew). Cua CLI: `cua sb ls`.
- t3code: `cd t3code && pnpm install && pnpm dev` (Node 26, pnpm 12).
- iOS: open `ios/RavenMobile/RavenMobile.xcodeproj` (Xcode 27).

## Secrets
Keys live in `.env` at repo root (see `.env.example`). Never commit or log them.
