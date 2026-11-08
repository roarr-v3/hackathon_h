import * as NodeHttpClient from "@effect/platform-node/NodeHttpClient";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  DesktopRavenExecutionProfileSchema,
  type DesktopRavenExecutionProfile,
  type DesktopRavenExecutionState,
} from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Ref from "effect/Ref";
import * as Schema from "effect/Schema";
import * as SubscriptionRef from "effect/SubscriptionRef";
import * as TestClock from "effect/testing/TestClock";
import { HttpClient, HttpClientResponse } from "effect/http";
import { describe as plainDescribe, expect, it as plainIt } from "vite-plus/test";

import * as DesktopAppIdentity from "../app/DesktopAppIdentity.ts";
import * as ElectronSafeStorage from "../electron/ElectronSafeStorage.ts";
import * as RavenCoreSidecar from "./RavenCoreSidecar.ts";
import * as RavenSettings from "./RavenSettings.ts";
import * as RavenSettingsEnv from "./RavenSettingsEnv.ts";
import { ravenSettingsDir } from "./RavenPaths.ts";

const decodeExecutionProfileBody = Schema.decodeEffect(
  Schema.fromJsonString(DesktopRavenExecutionProfileSchema),
);

const fakeSafeStorage: ElectronSafeStorage.ElectronSafeStorage["Service"] = {
  isEncryptionAvailable: Effect.succeed(true),
  encryptString: (value) =>
    Effect.sync(() => new TextEncoder().encode(value.split("").reverse().join(""))),
  decryptString: (value) =>
    Effect.sync(() => new TextDecoder().decode(value).split("").reverse().join("")),
  selectedStorageBackend: Effect.succeedNone,
};

plainDescribe("RavenSettings pure helpers", () => {
  plainIt(
    "merges managed env settings in place, appends missing keys, deletes cleared keys, and keeps comments",
    () => {
      const original = [
        "# preserve this comment",
        "export RAVEN_H_MODE=models-loop",
        "OTHER_SETTING=untouched",
        "HAI_MODEL=holo4-27b",
        "RAVEN_IDLE_SUSPEND_MIN=20",
        "",
      ].join("\n");

      const merged = RavenSettingsEnv.mergeRavenEnvFile(original, {
        RAVEN_H_MODE: "local-runtime",
        HAI_MODEL: null,
        RAVEN_IDLE_SUSPEND_MIN: null,
      });

      expect(merged).toBe(
        [
          "# preserve this comment",
          "export RAVEN_H_MODE=local-runtime",
          "OTHER_SETTING=untouched",
          "",
        ].join("\n"),
      );
      expect(RavenSettingsEnv.mergeRavenEnvFile("", { HAI_MODEL: "holo4-27b" })).toBe(
        "HAI_MODEL=holo4-27b",
      );
      expect(
        RavenSettingsEnv.parseRavenSettings(
          'export RAVEN_H_MODE="agents-api"\nRAVEN_IDLE_SUSPEND_MIN=0\n',
        ),
      ).toEqual({ hMode: "agents-api", model: null, idleSuspendMin: 0 });
    },
  );

  plainIt("masks stored keys and omits unset keys from raven-core environment", () => {
    const secret = "test-api-key-4812";
    const masked = RavenSettingsEnv.maskRavenKeys({
      HAI_API_KEY: secret,
    });
    const coreEnv = RavenSettingsEnv.composeRavenCoreEnv(
      { HAI_API_KEY: secret },
      { hMode: null, model: "holo4-35b-a3b", idleSuspendMin: null },
    );

    expect(masked.HAI_API_KEY).toEqual({ set: true, last4: "4812" });
    expect(JSON.stringify(masked)).not.toContain(secret);
    expect(coreEnv).toEqual({ HAI_API_KEY: secret, HAI_MODEL: "holo4-35b-a3b" });
    expect(coreEnv.RAVEN_H_MODE).toBeUndefined();
    expect(coreEnv.RAVEN_IDLE_SUSPEND_MIN).toBeUndefined();
  });
});

describe("RavenSettings", () => {
  it.effect(
    "redirects legacy execution writes without side effects and still saves encrypted keys and idle settings",
    () =>
      Effect.gen(function* () {
        const fileSystem = yield* FileSystem.FileSystem;
        const path = yield* Path.Path;
        const userDataPath = yield* fileSystem.makeTempDirectoryScoped({
          prefix: "raven-settings-test-",
        });
        const restarts = yield* Ref.make(0);
        const status = yield* SubscriptionRef.make<RavenCoreSidecar.RavenCoreStatus>({
          state: "stopped",
          logPath: path.join(userDataPath, "raven", "artifacts", "logs", "raven-core.log"),
          url: "http://127.0.0.1:8787",
        });
        const sidecarLayer = Layer.succeed(RavenCoreSidecar.RavenCoreSidecar, {
          status,
          start: Effect.void,
          restart: Ref.update(restarts, (value) => value + 1),
          stop: Effect.void,
        });
        const identityLayer = Layer.succeed(DesktopAppIdentity.DesktopAppIdentity, {
          resolveUserDataPath: Effect.succeed(userDataPath),
          configure: Effect.void,
        });
        const envLayer = RavenSettingsEnv.layer.pipe(
          Layer.provideMerge(identityLayer),
          Layer.provideMerge(
            Layer.succeed(ElectronSafeStorage.ElectronSafeStorage, fakeSafeStorage),
          ),
        );
        const settingsLayer = RavenSettings.layer.pipe(
          Layer.provideMerge(sidecarLayer),
          Layer.provideMerge(envLayer),
        );

        const result = yield* Effect.gen(function* () {
          const settings = yield* RavenSettings.RavenSettings;
          for (const input of [
            { settings: { hMode: "models-loop" } },
            {
              keys: { HAI_API_KEY: "must-not-save-this-key" },
              settings: { model: "holo4-35b-a3b", idleSuspendMin: 99 },
            },
            { settings: { hMode: null, model: null } },
          ] as const) {
            const error = yield* settings.save(input).pipe(Effect.flip);
            assert.include(error.message, "sidebar Execution");
          }
          assert.equal(yield* Ref.get(restarts), 0);
          assert.deepEqual(yield* settings.ravenCoreEnv, {});
          const saved = yield* settings.save({
            keys: { HAI_API_KEY: "test-api-key-4812" },
            settings: { idleSuspendMin: 0 },
          });
          const directory = ravenSettingsDir(path, userDataPath);
          const secretsBytes = yield* fileSystem.readFile(path.join(directory, "secrets.bin"));
          const secretsText = new TextDecoder().decode(secretsBytes);
          const envText = yield* fileSystem.readFileString(path.join(directory, "raven.env"));
          const coreEnv = yield* settings.ravenCoreEnv;
          return { saved, secretsText, envText, coreEnv };
        }).pipe(Effect.provide(settingsLayer));

        assert.deepEqual(result.saved.keys.HAI_API_KEY, { set: true, last4: "4812" });
        assert.equal(JSON.stringify(result.saved).includes("test-api-key-4812"), false);
        assert.equal(result.secretsText.includes("test-api-key-4812"), false);
        assert.equal(result.envText.includes("test-api-key-4812"), false);
        assert.notInclude(result.envText, "RAVEN_H_MODE");
        assert.notInclude(result.envText, "HAI_MODEL");
        assert.include(result.envText, "RAVEN_IDLE_SUSPEND_MIN=0");
        assert.equal(result.coreEnv.HAI_API_KEY, "test-api-key-4812");
        assert.equal(result.coreEnv.RAVEN_IDLE_SUSPEND_MIN, "0");
        assert.equal(yield* Ref.get(restarts), 1);
      }).pipe(
        Effect.scoped,
        Effect.provide(Layer.mergeAll(NodeServices.layer, NodeHttpClient.layerUndici)),
      ),
  );
});

const executionProfile: DesktopRavenExecutionProfile = {
  planner: "local",
  model: "holo4-27b",
  h_mode: "auto",
  platform_modes: {},
  linux_target: "apple-container",
  linux_space: "raven-linux-apple",
  linux_fallback: true,
  windows_target: "devin-vm",
  ios_target: "simulator",
  ios_device_id: null,
};

const executionState: DesktopRavenExecutionState = {
  profile: executionProfile,
  targets: [
    {
      platform: "windows",
      target: "devin-vm",
      status: "unavailable",
      detail: "Windows VM provisioning is unavailable.",
    },
  ],
};

const executionLayer = Effect.fn("test.RavenSettings.executionLayer")(function* (
  client: HttpClient.HttpClient,
  state: RavenCoreSidecar.RavenCoreStatus["state"] = "external",
) {
  const restarts = yield* Ref.make(0);
  const status = yield* SubscriptionRef.make<RavenCoreSidecar.RavenCoreStatus>({
    state,
    logPath: "/unused/raven-core.log",
    url: "http://127.0.0.1:9876",
  });
  const layer = RavenSettings.layer.pipe(
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(RavenSettingsEnv.RavenSettingsEnv, {}),
        Layer.succeed(RavenCoreSidecar.RavenCoreSidecar, {
          status,
          start: Effect.die("execution settings must not start the sidecar"),
          restart: Ref.update(restarts, (value) => value + 1),
          stop: Effect.die("execution settings must not stop the sidecar"),
        }),
        Layer.succeed(HttpClient.HttpClient, client),
      ),
    ),
  );
  return { layer, restarts };
});

describe("Raven execution proxy", () => {
  it.effect(
    "persists full profiles only in core and checks readiness without restarting or provisioning",
    () =>
      Effect.gen(function* () {
        let stored = executionState;
        const requests: string[] = [];
        const devices = [
          { id: "sim-1", name: "iPhone", kind: "simulator", available: false },
        ] as const;
        const client = HttpClient.make((request) =>
          Effect.gen(function* () {
            requests.push(`${request.method} ${request.url}`);
            if (request.method === "PUT") {
              assert.equal(request.body._tag, "Uint8Array");
              if (request.body._tag === "Uint8Array") {
                stored = {
                  ...stored,
                  profile: yield* decodeExecutionProfileBody(
                    new TextDecoder().decode(request.body.body),
                  ),
                };
              }
            } else {
              assert.equal(request.body._tag, "Empty");
            }
            return HttpClientResponse.fromWeb(
              request,
              Response.json(request.url.endsWith("/devices") ? devices : stored),
            );
          }).pipe(Effect.orDie),
        );
        const { layer, restarts } = yield* executionLayer(client);
        yield* Effect.gen(function* () {
          const settings = yield* RavenSettings.RavenSettings;
          const before = yield* settings.getExecution;
          const profile: DesktopRavenExecutionProfile = {
            ...before.profile,
            planner: "cloud",
            model: "h/custom-holo4.v1",
            h_mode: "agents-api",
            platform_modes: { macos: "models-loop", windows: "local-runtime" },
            ios_target: "device",
            ios_device_id: "device-1",
            linux_target: "devin-vm",
            linux_fallback: false,
          };
          const saved = yield* settings.saveExecution(profile);
          assert.deepEqual(saved.profile, profile);
          assert.deepEqual(yield* settings.getExecution, saved);
          assert.deepEqual(yield* settings.checkExecution, saved);
          assert.deepEqual(yield* settings.listDevices, devices);
          assert.deepEqual(before.profile, executionProfile);
          assert.deepEqual(saved.targets, executionState.targets);
        }).pipe(Effect.provide(layer));
        assert.deepEqual(requests, [
          "GET http://127.0.0.1:9876/execution",
          "PUT http://127.0.0.1:9876/execution",
          "GET http://127.0.0.1:9876/execution",
          "POST http://127.0.0.1:9876/execution/check",
          "GET http://127.0.0.1:9876/execution/devices",
        ]);
        assert.equal(yield* Ref.get(restarts), 0);
      }),
  );

  it.effect(
    "authenticates only when required and preserves capability errors without exposing pairing tokens",
    () =>
      Effect.gen(function* () {
        const token = "private-test-pairing-token";
        const requests: string[] = [];
        const client = HttpClient.make((request) =>
          Effect.sync(() => {
            requests.push(`${request.method} ${request.url}`);
            const response = request.url.endsWith("/pairing")
              ? Response.json({ token, urls: ["http://127.0.0.1:9876"] })
              : request.headers.authorization !== `Bearer ${token}`
                ? Response.json({ detail: "Unauthorized" }, { status: 401 })
                : request.method === "PUT"
                  ? Response.json(
                      { detail: `H hosted loop cannot control this target. Bearer ${token}` },
                      { status: 422 },
                    )
                  : Response.json(executionState);
            return HttpClientResponse.fromWeb(request, response);
          }),
        );
        const { layer, restarts } = yield* executionLayer(client);
        yield* Effect.gen(function* () {
          const settings = yield* RavenSettings.RavenSettings;
          const state = yield* settings.getExecution;
          assert.deepEqual(state, executionState);
          assert.notInclude(JSON.stringify(state), token);
          const error = yield* settings.saveExecution(executionProfile).pipe(Effect.flip);
          assert.equal(error.reason, "http");
          assert.equal(error.status, 422);
          assert.include(error.message, "H hosted loop cannot control this target");
          assert.notInclude(error.message, token);
          assert.notInclude(JSON.stringify(error), token);
        }).pipe(Effect.provide(layer));
        assert.equal(requests.filter((request) => request.endsWith("/pairing")).length, 2);
        assert.equal(yield* Ref.get(restarts), 0);
      }),
  );

  it.effect(
    "reports stopped, unauthenticated, outdated, and malformed cores as errors rather than ready",
    () =>
      Effect.gen(function* () {
        const stopped = yield* executionLayer(
          HttpClient.make(() => Effect.die("a stopped core must not receive requests")),
          "stopped",
        );
        const unavailable = yield* Effect.gen(function* () {
          return yield* (yield* RavenSettings.RavenSettings).getExecution.pipe(Effect.flip);
        }).pipe(Effect.provide(stopped.layer));
        assert.equal(unavailable.reason, "unavailable");

        for (const [status, body, reason] of [
          [401, { detail: "Unauthorized" }, "authentication"],
          [404, { detail: "Not Found" }, "http"],
          [200, { profile: { planner: "none" }, targets: [] }, "invalid-response"],
        ] as const) {
          const { layer } = yield* executionLayer(
            HttpClient.make((request) =>
              Effect.succeed(HttpClientResponse.fromWeb(request, Response.json(body, { status }))),
            ),
          );
          const error = yield* Effect.gen(function* () {
            return yield* (yield* RavenSettings.RavenSettings).getExecution.pipe(Effect.flip);
          }).pipe(Effect.provide(layer));
          assert.equal(error.reason, reason);
        }
      }),
  );

  it.effect("bounds a stalled save and warns that its persistence outcome is unknown", () =>
    Effect.gen(function* () {
      const started = yield* Deferred.make<void>();
      const interrupted = yield* Ref.make(false);
      const { layer, restarts } = yield* executionLayer(
        HttpClient.make(() =>
          Deferred.succeed(started, undefined).pipe(
            Effect.andThen(Effect.never),
            Effect.onInterrupt(() => Ref.set(interrupted, true)),
          ),
        ),
      );
      const fiber = yield* Effect.gen(function* () {
        return yield* (yield* RavenSettings.RavenSettings)
          .saveExecution(executionProfile)
          .pipe(Effect.flip);
      }).pipe(Effect.provide(layer), Effect.forkChild);
      yield* Deferred.await(started);
      yield* TestClock.adjust(Duration.seconds(16));
      const error = yield* Fiber.join(fiber);
      assert.equal(error.reason, "timeout");
      assert.include(error.message, "reload to confirm");
      assert.isTrue(yield* Ref.get(interrupted));
      assert.equal(yield* Ref.get(restarts), 0);
    }),
  );
});
