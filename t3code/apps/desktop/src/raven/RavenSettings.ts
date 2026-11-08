import {
  DesktopRavenExecutionDeviceSchema,
  DesktopRavenExecutionStateSchema,
  type DesktopRavenExecutionDevice,
  type DesktopRavenExecutionProfile,
  type DesktopRavenExecutionState,
} from "@t3tools/contracts";
import * as ByteSize from "effect/ByteSize";
import * as Context from "effect/Context";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Redacted from "effect/Redacted";
import * as Schema from "effect/Schema";
import * as SubscriptionRef from "effect/SubscriptionRef";
import { HttpClient, HttpClientRequest, HttpIncomingMessage } from "effect/http";

import * as RavenCoreSidecar from "./RavenCoreSidecar.ts";
import * as RavenSettingsEnv from "./RavenSettingsEnv.ts";

const RavenPairing = Schema.Struct({ token: Schema.String, urls: Schema.Array(Schema.String) });
const decodeRavenPairing = Schema.decodeUnknownEffect(RavenPairing);
const RavenExecutionRejection = Schema.Struct({
  detail: Schema.Union([Schema.String, Schema.Array(Schema.Struct({ msg: Schema.String }))]),
});
const decodeExecutionRejection = Schema.decodeUnknownEffect(RavenExecutionRejection);

class RavenExecutionError extends Schema.TaggedError<
  RavenExecutionError,
  { readonly brand: unique symbol }
>()("RavenExecutionError", {
  operation: Schema.Literals(["get", "save", "check", "devices"]),
  reason: Schema.Literals([
    "unavailable",
    "request",
    "timeout",
    "authentication",
    "http",
    "invalid-response",
  ]),
  status: Schema.optionalKey(Schema.Int),
  rejection: Schema.optionalKey(Schema.String),
  cause: Schema.optionalKey(Schema.Redacted(Schema.Defect(), { disallowJsonEncode: true })),
}) {
  override get message(): string {
    switch (this.reason) {
      case "unavailable":
        return "Raven core is not ready. Open Settings → Raven to check its status, then reload execution settings.";
      case "request":
        return "Could not reach Raven core. Check Settings → Raven, then reload execution settings.";
      case "timeout":
        return this.operation === "save"
          ? "Saving execution settings timed out. The core may have saved them; reload to confirm before retrying."
          : "Raven core did not respond within 15 seconds. Readiness is unknown; try again.";
      case "authentication":
        return "Raven core rejected authentication. Check the local core and its pairing configuration in Settings → Raven.";
      case "http":
        return `Raven core rejected the execution ${this.operation} request (HTTP ${this.status}).${this.rejection ? ` ${this.rejection}` : " Check that the core supports the execution API."}`;
      case "invalid-response":
        return "Raven core returned an invalid execution response. Update the core and reload; readiness is unknown.";
    }
  }
}

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
    readonly getExecution: Effect.Effect<DesktopRavenExecutionState, RavenExecutionError>;
    readonly saveExecution: (
      profile: DesktopRavenExecutionProfile,
    ) => Effect.Effect<DesktopRavenExecutionState, RavenExecutionError>;
    readonly checkExecution: Effect.Effect<DesktopRavenExecutionState, RavenExecutionError>;
    readonly listDevices: Effect.Effect<
      ReadonlyArray<DesktopRavenExecutionDevice>,
      RavenExecutionError
    >;
  }
>()("@t3tools/desktop/raven/RavenSettings") {}

/** @public */
export const make = Effect.fn("RavenSettings.make")(function* () {
  const settingsEnv = yield* RavenSettingsEnv.RavenSettingsEnv;
  const sidecar = yield* RavenCoreSidecar.RavenCoreSidecar;
  const httpClient = yield* HttpClient.HttpClient;

  const readPairing = Effect.fn("RavenSettings.readPairing")((url: string) =>
    httpClient.execute(HttpClientRequest.get(`${url}/pairing`)).pipe(
      Effect.flatMap((response) => response.json),
      Effect.flatMap(decodeRavenPairing),
      Effect.timeoutOption(Duration.seconds(1)),
      Effect.map(Option.getOrNull),
      Effect.orElseSucceed(() => null),
    ),
  );

  const execute = Effect.fn("RavenSettings.execute")(
    (request: HttpClientRequest.HttpClientRequest, operation: RavenExecutionError["operation"]) =>
      httpClient.execute(request).pipe(
        Effect.mapError(
          (cause) =>
            new RavenExecutionError({
              operation,
              reason: "request",
              cause: Redacted.make(cause),
            }),
        ),
      ),
  );

  const requestExecution = Effect.fn("RavenSettings.requestExecution")(
    function* <A>(
      operation: RavenExecutionError["operation"],
      schema: Schema.Decoder<A>,
      profile?: DesktopRavenExecutionProfile,
    ): Effect.fn.Return<A, RavenExecutionError> {
      const status = yield* SubscriptionRef.get(sidecar.status);
      if (status.state !== "ready" && status.state !== "external") {
        return yield* new RavenExecutionError({ operation, reason: "unavailable" });
      }
      const path =
        operation === "devices"
          ? "/execution/devices"
          : operation === "check"
            ? "/execution/check"
            : "/execution";
      const url = `${status.url.replace(/\/+$/, "")}${path}`;
      let request =
        operation === "save"
          ? HttpClientRequest.put(url)
          : operation === "check"
            ? HttpClientRequest.post(url)
            : HttpClientRequest.get(url);
      if (profile !== undefined) {
        request = yield* HttpClientRequest.bodyJson(request, profile).pipe(
          Effect.mapError(
            (cause) =>
              new RavenExecutionError({
                operation,
                reason: "request",
                cause: Redacted.make(cause),
              }),
          ),
        );
      }

      let response = yield* execute(request, operation);
      let token: string | undefined;
      if (response.status === 401 || response.status === 403) {
        const pairing = yield* readPairing(status.url);
        if (!pairing?.token) {
          return yield* new RavenExecutionError({ operation, reason: "authentication" });
        }
        token = pairing.token;
        response = yield* execute(HttpClientRequest.bearerToken(request, token), operation);
      }
      if (response.status === 401 || response.status === 403) {
        return yield* new RavenExecutionError({ operation, reason: "authentication" });
      }
      if (response.status < 200 || response.status >= 300) {
        const failure = yield* response.json.pipe(
          Effect.flatMap(decodeExecutionRejection),
          Effect.orElseSucceed(() => null),
        );
        const detail =
          typeof failure?.detail === "string"
            ? failure.detail
            : failure?.detail.map((issue) => issue.msg).join("; ");
        const rejection = (token ? detail?.replaceAll(token, "[redacted]") : detail)
          ?.replace(/Bearer\s+\S+/gi, "Bearer [redacted]")
          .replace(/([?&]token=)[^&#\s]+/gi, "$1[redacted]")
          .slice(0, 1_000);
        return yield* new RavenExecutionError({
          operation,
          reason: "http",
          status: response.status,
          ...(rejection ? { rejection } : {}),
        });
      }
      return yield* response.json.pipe(
        Effect.flatMap(Schema.decodeUnknownEffect(schema)),
        Effect.mapError(
          (cause) =>
            new RavenExecutionError({
              operation,
              reason: "invalid-response",
              cause: Redacted.make(cause),
            }),
        ),
      );
    },
    (effect, operation) =>
      effect.pipe(
        Effect.provideService(HttpIncomingMessage.MaxBodySize, ByteSize.mebibytes(1)),
        Effect.timeout(Duration.seconds(15)),
        Effect.catchTags({
          TimeoutError: () =>
            Effect.fail(new RavenExecutionError({ operation, reason: "timeout" })),
        }),
      ),
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
    if (input.settings?.hMode !== undefined || input.settings?.model !== undefined) {
      return yield* new RavenSettingsEnv.RavenSettingsError({
        message:
          "H mode and model are owned by Raven core. Use the sidebar Execution panel or the /execution API to change them. No settings were changed.",
      });
    }
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
    getExecution: requestExecution("get", DesktopRavenExecutionStateSchema),
    saveExecution: Effect.fn("RavenSettings.saveExecution")((profile) =>
      requestExecution("save", DesktopRavenExecutionStateSchema, profile),
    ),
    checkExecution: requestExecution("check", DesktopRavenExecutionStateSchema),
    listDevices: requestExecution("devices", Schema.Array(DesktopRavenExecutionDeviceSchema)),
  });
});

export const layer = Layer.effect(RavenSettings, make());
