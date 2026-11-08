import * as NodeHttpClient from "@effect/platform-node/NodeHttpClient";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Ref from "effect/Ref";
import * as SubscriptionRef from "effect/SubscriptionRef";
import { describe as plainDescribe, expect, it as plainIt } from "vite-plus/test";

import * as DesktopAppIdentity from "../app/DesktopAppIdentity.ts";
import * as ElectronSafeStorage from "../electron/ElectronSafeStorage.ts";
import * as RavenCoreSidecar from "./RavenCoreSidecar.ts";
import * as RavenSettings from "./RavenSettings.ts";
import * as RavenSettingsEnv from "./RavenSettingsEnv.ts";
import { ravenSettingsDir } from "./RavenPaths.ts";

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
    "saves encrypted keys and env settings, returns only masked keys, and restarts once",
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
          const saved = yield* settings.save({
            keys: { HAI_API_KEY: "test-api-key-4812" },
            settings: { hMode: "local-runtime", model: "holo4-35b-a3b", idleSuspendMin: 0 },
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
        assert.include(result.envText, "RAVEN_H_MODE=local-runtime");
        assert.include(result.envText, "HAI_MODEL=holo4-35b-a3b");
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
