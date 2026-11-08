# Raven — small bird, big bug energy

Raven is a multi-platform testing suite that lets developers test their apps seamlessly across
macOS, iOS, Linux and Windows. Describe a test in plain words, typed or spoken; Devin turns it into
a QA plan, and H Company's computer-use agents run it on real machines and devices, then report a
pass/fail verdict with screenshots.

Website: https://roarr-v3.github.io/hackathon_h/

## How it works

1. **Ask**: type or say a task in Raven Agent (desktop) or the Raven iPhone app.
2. **Plan**: Devin reads the project and writes a QA plan (setup, task, expected result, verification).
3. **Run**: H Company's Holo agent does all clicking and typing on the target:
   - **macOS**: this Mac.
   - **iOS**: this Mac controlling your iPhone through Xcode's Device Hub.
   - **Linux**: a Cua Apple Container.
   - **Windows**: a Devin VM.
4. **Report**: `VERDICT: PASSED` or `FAILED` with screenshots. On failure, Linkup researches the
   error, and `fix and retry` asks Devin for a fix, then reruns the same checks.

## Repository

| Path | What it is |
|---|---|
| `raven-core/` | Python orchestrator: HTTP/WebSocket API, planning, H agents, platform workers |
| `t3code/` | Raven Agent desktop app (a vendored t3code build, MIT) |
| `ios/RavenMobile/` | Raven iPhone app (threads, screenshots, voice) |
| `specs/` | YAML test suites |
| `website/` | Landing page (published from the `gh-pages` branch) |
| `bin/raven` | One-command launcher for Raven Agent |

## Quick start (macOS, Apple silicon)

Requirements: Xcode 27, Node 26, pnpm 12, [uv](https://docs.astral.sh/uv/), the Devin CLI (logged in),
and an H Company API key. Optional: Gladia (voice), Linkup (failure research), the Cua CLI (Linux),
Devin API key and organization ID (Windows/Linux VMs, cloud planning).

```bash
git clone https://github.com/roarr-v3/hackathon_h.git && cd hackathon_h
cp .env.example .env        # add HAI_API_KEY (and any optional keys)
bin/raven                   # installs, downloads Electron, builds and opens Raven Agent
```

Raven Agent starts raven-core itself. Keys can also be set in Raven Agent → Settings → Raven.

## Using it

- In Raven Agent, start a thread with the **Raven · Holo4** model and type a task, for example
  `open TextEdit and type bonjour` or `on linux: open a terminal and run uname -a`.
- Prompts that mention your iPhone run the iOS workflow on a connected, unlocked iPhone with
  Developer Mode on. Prompts that name no platform run on this Mac.
- `run holo-linux-smoke on linux` runs a test suite from `specs/`; `retry` repeats the last task;
  `fix and retry` asks Devin to repair a failed free-form check.
- Pair the iPhone app by scanning the QR code in Raven Agent → Settings → Raven. Reply in a thread to
  continue it, and use the ✓ button to select and delete chats.
- Pick targets in the sidebar **Execution** panel.
- For faster planning, set `DEVIN_PLANNING_MODEL=gpt-6-sol-none-priority` in `.env`.

See [TESTING.md](TESTING.md) for manual and automated tests, and [AGENTS.md](AGENTS.md) for the
architecture and API.
