# RavenMobile

SwiftUI iPhone companion for Raven. All-black UI, every agent is a conversation thread,
live screenshots, run matrix + report, Gladia push-to-talk voice. iOS 18+, no dependencies.

## Build / run

```sh
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile \
  -destination 'generic/platform=iOS Simulator' build

# run in a simulator
xcrun simctl boot "iPhone 17 Pro" 2>/dev/null; open -a Simulator
xcodebuild -project ios/RavenMobile/RavenMobile.xcodeproj -scheme RavenMobile \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -derivedDataPath ios/RavenMobile/build build
xcrun simctl install booted ios/RavenMobile/build/Build/Products/Debug-iphonesimulator/RavenMobile.app
xcrun simctl launch booted ai.raven.mobile
```

The project uses a file-system-synchronized group: any file added under `RavenMobile/`
is compiled automatically (no pbxproj edits). `project.yml` is an optional XcodeGen spec.

### Launch arguments (screenshots / demos)
`xcrun simctl launch booted ai.raven.mobile -demo YES -screen thread`

- `-demo YES|NO` force demo mode · `-server http://192.168.1.20:8787` override server
- `-screen thread|runs|run|settings` deep-link (`-agent <id>`, `-run <id>` optional;
  demo ids: `agt_demo_win`, `agt_demo_mac`, `agt_demo_linux`, `agt_demo_ios`, `run_demo_geohash`)

## Layout
- `Models/` Codable mirrors of `raven-core/src/raven_core/events.py` (+ tolerant `JSONValue`)
- `Networking/` `APIClient` (REST), `EventSocket` (WS `/ws/events`, auto-reconnect, pings),
  `RavenStore` (@Observable app state), `DemoEngine` (offline simulator), `Keychain`, `ImageLoader`
- `Voice/` `VoiceRecorder` — AVAudioEngine → 16 kHz mono s16le → Gladia WebSocket
- `Views/` home thread list, agent thread, runs/specs/matrix/report, settings, composer
- `DesignSystem/` theme, pulsing dots, status dot, platform icons

## Backend expectations
- REST + WS exactly as in `AGENTS.md`; Bearer token via `Authorization` header (WS: `?token=`).
- `GET /artifacts/...` should accept the same Bearer header (the app sends it).
- `POST /voice/command {"text"}` (home-screen voice/typed commands; not yet in AGENTS.md).
  Any JSON response is fine; if it contains `agent` (an Agent) it is shown immediately, a
  `message`/`reply` string is flashed as a banner.
- `POST /agents/{id}/messages` should echo an `agent.message` event with `role:"user"`
  (the app shows an optimistic bubble and de-dupes on the echo).
- `POST /voice/session` → `VoiceSession` with Gladia `url` (wss). Gladia config must be
  `encoding: wav/pcm, bit_depth: 16, sample_rate: 16000, channels: 1` and
  `messages_config.receive_partial_transcripts: true` for live partials.
