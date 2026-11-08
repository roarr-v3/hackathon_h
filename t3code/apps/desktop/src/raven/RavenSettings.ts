import * as Context from "effect/Context";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as SubscriptionRef from "effect/SubscriptionRef";
import { HttpClient, HttpClientRequest } from "effect/http";

import * as RavenCoreSidecar from "./RavenCoreSidecar.ts";
import * as RavenSettingsEnv from "./RavenSettingsEnv.ts";

const RavenPairing = Schema.Struct({ token: Schema.String, urls: Schema.Array(Schema.String) });
const decodeRavenPairing = Schema.decodeUnknownEffect(RavenPairing);

export interface RavenSettingsState {
  readonly keys: RavenSettingsEnv.RavenKeysState;
  readonly settings: RavenSettingsEnv.RavenSettingsValues;
  readonly status: RavenCoreSidecar.RavenCoreStatus;
  readonly pairing: typeof RavenPairing.Type | null;
}

export class RavenSettings extends Context.Service<
  RavenSettings,
  {
    readonly get: Effect.Effect<RavenSettingsState, RavenSettingsEnv.RavenSettingsStoreError>;
    readonly save: (
      input: RavenSettingsEnv.RavenSettingsSaveInput,
    ) => Effect.Effect<RavenSettingsState, RavenSettingsEnv.RavenSettingsStoreError>;
    readonly testKey: (
      name: RavenSettingsEnv.RavenKeyName,
      value?: string,
    ) => Effect.Effect<
      RavenSettingsEnv.RavenKeyTestResult,
      RavenSettingsEnv.RavenSettingsStoreError
    >;
    readonly restart: Effect.Effect<RavenSettingsState, RavenSettingsEnv.RavenSettingsStoreError>;
    readonly ravenCoreEnv: Effect.Effect<
      Record<string, string>,
      RavenSettingsEnv.RavenSettingsStoreError
    >;
  }
>()("@t3tools/desktop/raven/RavenSettings") {}

/** @public */
export const make = Effect.fn("RavenSettings.make")(function* () {
  const settingsEnv = yield* RavenSettingsEnv.RavenSettingsEnv;
  const sidecar = yield* RavenCoreSidecar.RavenCoreSidecar;
  const httpClient = yield* HttpClient.HttpClient;

  const readPairing = (url: string) =>
    httpClient.execute(HttpClientRequest.get(`${url}/pairing`)).pipe(
      Effect.flatMap((response) => response.json),
      Effect.flatMap(decodeRavenPairing),
      Effect.timeoutOption(Duration.seconds(1)),
      Effect.map(Option.getOrNull),
      Effect.orElseSucceed(() => null),
    );

  const get = Effect.gen(function* () {
    const stored = yield* settingsEnv.get;
    const status = yield* SubscriptionRef.get(sidecar.status);
    const reachable = status.state === "ready" || status.state === "external";
    const pairing = reachable ? yield* readPairing(status.url) : null;
    return { ...stored, status, pairing };
  });

  const save = Effect.fn("RavenSettings.save")(function* (
    input: RavenSettingsEnv.RavenSettingsSaveInput,
  ) {
    yield* settingsEnv.save(input);
    yield* sidecar.restart;
    return yield* get;
  });

  const testKey = (name: RavenSettingsEnv.RavenKeyName, value?: string) =>
    settingsEnv.testKey(name, value);

  const restart = Effect.gen(function* () {
    yield* sidecar.restart;
    return yield* get;
  });

  return RavenSettings.of({
    get,
    save,
    testKey,
    restart,
    ravenCoreEnv: settingsEnv.ravenCoreEnv,
  });
});

export const layer = Layer.effect(RavenSettings, make());
