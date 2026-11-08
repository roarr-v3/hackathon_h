import * as Context from "effect/Context";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
import * as Schema from "effect/Schema";
import { HttpClient, HttpClientRequest } from "effect/http";

import * as DesktopAppIdentity from "../app/DesktopAppIdentity.ts";
import * as ElectronSafeStorage from "../electron/ElectronSafeStorage.ts";
import { ravenSettingsDir } from "./RavenPaths.ts";

const RAVEN_KEY_NAMES = [
  "HAI_API_KEY",
  "GLADIA_API_KEY",
  "LINKUP_API_KEY",
  "DEVIN_API_KEY",
] as const;

export type RavenKeyName = (typeof RAVEN_KEY_NAMES)[number];

const RAVEN_H_MODES = ["local-runtime", "agents-api", "models-loop"] as const;
const RAVEN_MODELS = ["holo4-27b", "holo4-35b-a3b"] as const;

export type RavenSettingsValues = {
  readonly hMode: string | null;
  readonly model: string | null;
  readonly idleSuspendMin: number | null;
};

export interface RavenKeyState {
  readonly set: boolean;
  readonly last4: string | null;
}

export type RavenKeysState = Record<RavenKeyName, RavenKeyState>;
export type RavenSecretValues = Partial<Record<RavenKeyName, string>>;
export type RavenEnvUpdates = Partial<
  Record<"RAVEN_H_MODE" | "HAI_MODEL" | "RAVEN_IDLE_SUSPEND_MIN", string | null>
>;

export interface RavenSettingsValuesInput {
  readonly hMode?: (typeof RAVEN_H_MODES)[number] | null;
  readonly model?: (typeof RAVEN_MODELS)[number] | null;
  readonly idleSuspendMin?: number | null;
}

export interface RavenSettingsSaveInput {
  readonly keys?: Partial<Record<RavenKeyName, string | null>>;
  readonly settings?: RavenSettingsValuesInput;
}

export interface RavenKeyTestResult {
  readonly ok: boolean;
  readonly message: string;
}

export class RavenSettingsError extends Schema.TaggedError<RavenSettingsError>()(
  "RavenSettingsError",
  { message: Schema.String },
) {}

export type RavenSettingsStoreError = RavenSettingsError | PlatformError.PlatformError;

export const RavenSecretsSchema = Schema.Struct({
  HAI_API_KEY: Schema.optionalKey(Schema.String),
  GLADIA_API_KEY: Schema.optionalKey(Schema.String),
  LINKUP_API_KEY: Schema.optionalKey(Schema.String),
  DEVIN_API_KEY: Schema.optionalKey(Schema.String),
});

export const maskRavenKeys = (secrets: RavenSecretValues): RavenKeysState =>
  Object.fromEntries(
    RAVEN_KEY_NAMES.map((name) => {
      const value = secrets[name];
      return [
        name,
        {
          set: value !== undefined && value.length > 0,
          last4: value !== undefined && value.length >= 4 ? value.slice(-4) : null,
        },
      ];
    }),
  ) as RavenKeysState;

const parseEnvAssignment = (
  line: string,
): { readonly prefix: string; readonly key: string; readonly value: string } | undefined => {
  const match = /^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$/.exec(line);
  if (!match) return undefined;
  const [, prefix = "", key = "", value = ""] = match;
  return { prefix, key, value: value.trim().replace(/^["']|["']$/g, "") };
};

export const parseRavenSettings = (content: string): RavenSettingsValues => {
  const values = new Map<string, string>();
  for (const line of content.split(/\r?\n/)) {
    const assignment = parseEnvAssignment(line);
    if (assignment) values.set(assignment.key, assignment.value);
  }
  const rawIdle = values.get("RAVEN_IDLE_SUSPEND_MIN");
  const idle = rawIdle === undefined || rawIdle === "" ? null : Number(rawIdle);
  return {
    hMode: values.get("RAVEN_H_MODE") || null,
    model: values.get("HAI_MODEL") || null,
    idleSuspendMin: idle !== null && Number.isInteger(idle) && idle >= 0 ? idle : null,
  };
};

const ravenEnvKeys = ["RAVEN_H_MODE", "HAI_MODEL", "RAVEN_IDLE_SUSPEND_MIN"] as const;

export const mergeRavenEnvFile = (content: string, updates: RavenEnvUpdates): string => {
  const newline = content.includes("\r\n") ? "\r\n" : "\n";
  const hadTrailingNewline = content.endsWith("\n");
  const lines = content.length === 0 ? [] : content.split(/\r?\n/);
  if (hadTrailingNewline) lines.pop();
  const pending = new Map<string, string | null | undefined>(Object.entries(updates));
  const written = new Set<string>();
  const merged: Array<string> = [];

  for (const line of lines) {
    const assignment = parseEnvAssignment(line);
    if (!assignment || !pending.has(assignment.key)) {
      merged.push(line);
      continue;
    }
    const value = pending.get(assignment.key);
    if (value === null || value === undefined || written.has(assignment.key)) continue;
    written.add(assignment.key);
    merged.push(`${assignment.prefix}${assignment.key}=${value}`);
  }

  for (const key of ravenEnvKeys) {
    const value = pending.get(key);
    if (value !== null && value !== undefined && !written.has(key)) merged.push(`${key}=${value}`);
  }

  const result = merged.join(newline);
  return hadTrailingNewline && result.length > 0 ? `${result}${newline}` : result;
};

export const composeRavenCoreEnv = (
  secrets: RavenSecretValues,
  settings: RavenSettingsValues,
): Record<string, string> => {
  const env: Record<string, string> = {};
  for (const name of RAVEN_KEY_NAMES) {
    const value = secrets[name];
    if (value) env[name] = value;
  }
  if (settings.hMode !== null) env.RAVEN_H_MODE = settings.hMode;
  if (settings.model !== null) env.HAI_MODEL = settings.model;
  if (settings.idleSuspendMin !== null) {
    env.RAVEN_IDLE_SUSPEND_MIN = String(settings.idleSuspendMin);
  }
  return env;
};

export class RavenSettingsEnv extends Context.Service<
  RavenSettingsEnv,
  {
    readonly get: Effect.Effect<
      { readonly keys: RavenKeysState; readonly settings: RavenSettingsValues },
      RavenSettingsStoreError
    >;
    readonly save: (
      input: RavenSettingsSaveInput,
    ) => Effect.Effect<
      { readonly keys: RavenKeysState; readonly settings: RavenSettingsValues },
      RavenSettingsStoreError
    >;
    readonly ravenCoreEnv: Effect.Effect<Record<string, string>, RavenSettingsStoreError>;
    readonly testKey: (
      name: RavenKeyName,
      value?: string,
    ) => Effect.Effect<RavenKeyTestResult, RavenSettingsStoreError>;
  }
>()("@t3tools/desktop/raven/RavenSettingsEnv") {}

const readMissingEnvFile = (fileSystem: FileSystem.FileSystem, path: string) =>
  fileSystem.readFileString(path).pipe(
    Effect.catchTags({
      PlatformError: (error) =>
        error.reason._tag === "NotFound" ? Effect.succeed("") : Effect.fail(error),
    }),
  );

const describeKeyTestStatus = (status: number): RavenKeyTestResult =>
  status === 200
    ? { ok: true, message: "Key accepted." }
    : status === 401 || status === 403
      ? { ok: false, message: "Key rejected" }
      : { ok: false, message: `Unexpected HTTP ${status}` };

const KEY_TEST_REQUESTS: Partial<
  Record<RavenKeyName, (key: string) => HttpClientRequest.HttpClientRequest>
> = {
  HAI_API_KEY: (key) =>
    HttpClientRequest.get("https://api.hcompany.ai/v1/models").pipe(
      HttpClientRequest.bearerToken(key),
    ),
  GLADIA_API_KEY: (key) =>
    HttpClientRequest.get("https://api.gladia.io/v2/live?limit=1").pipe(
      HttpClientRequest.setHeader("x-gladia-key", key),
    ),
  DEVIN_API_KEY: (key) =>
    HttpClientRequest.get("https://api.devin.ai/v1/sessions?limit=1").pipe(
      HttpClientRequest.bearerToken(key),
    ),
};

const runKeyTest = (
  client: HttpClient.HttpClient,
  request: HttpClientRequest.HttpClientRequest,
): Effect.Effect<RavenKeyTestResult> =>
  client.execute(request).pipe(
    Effect.flatMap((response) =>
      response.text.pipe(Effect.ignore, Effect.as(describeKeyTestStatus(response.status))),
    ),
    Effect.timeoutOption(Duration.seconds(10)),
    Effect.map(
      Option.getOrElse((): RavenKeyTestResult => ({
        ok: false,
        message: "Request timed out after 10 seconds.",
      })),
    ),
    Effect.catch((error) =>
      Effect.succeed<RavenKeyTestResult>({ ok: false, message: error.message }),
    ),
  );

const RavenSecretsJson = Schema.fromJsonString(RavenSecretsSchema);
const decodeRavenSecrets = Schema.decodeEffect(RavenSecretsJson);
const encodeRavenSecrets = Schema.encodeEffect(RavenSecretsJson);

/** @public */
export const make = Effect.fn("RavenSettingsEnv.make")(function* () {
  const appIdentity = yield* DesktopAppIdentity.DesktopAppIdentity;
  const safeStorage = yield* ElectronSafeStorage.ElectronSafeStorage;
  const fileSystem = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const httpClient = yield* HttpClient.HttpClient;
  const userDataPath = yield* appIdentity.resolveUserDataPath;
  const directory = ravenSettingsDir(path, userDataPath);
  const secretsPath = path.join(directory, "secrets.bin");
  const envPath = path.join(directory, "raven.env");

  const readSecrets = Effect.gen(function* () {
    if (!(yield* fileSystem.exists(secretsPath))) return {};
    const plain = yield* safeStorage.decryptString(yield* fileSystem.readFile(secretsPath));
    const secrets: RavenSecretValues = yield* decodeRavenSecrets(plain);
    return secrets;
  }).pipe(
    Effect.tapError(() =>
      Effect.logWarning("Stored Raven keys could not be read; they show as unset."),
    ),
    Effect.orElseSucceed((): RavenSecretValues => ({})),
  );

  const readSettings = readMissingEnvFile(fileSystem, envPath).pipe(Effect.map(parseRavenSettings));

  const get = Effect.gen(function* () {
    const secrets = yield* readSecrets;
    const settings = yield* readSettings;
    return { keys: maskRavenKeys(secrets), settings };
  });

  const ravenCoreEnv = Effect.gen(function* () {
    const secrets = yield* readSecrets;
    const settings = yield* readSettings;
    return composeRavenCoreEnv(secrets, settings);
  });

  const save = Effect.fn("RavenSettingsEnv.save")(function* (input: RavenSettingsSaveInput) {
    if (
      input.settings?.hMode !== undefined &&
      input.settings.hMode !== null &&
      !RAVEN_H_MODES.includes(input.settings.hMode)
    ) {
      return yield* new RavenSettingsError({ message: "Invalid Raven H mode." });
    }
    if (
      input.settings?.model !== undefined &&
      input.settings.model !== null &&
      !RAVEN_MODELS.includes(input.settings.model)
    ) {
      return yield* new RavenSettingsError({ message: "Invalid Raven model." });
    }
    if (
      input.settings?.idleSuspendMin !== undefined &&
      input.settings.idleSuspendMin !== null &&
      (!Number.isInteger(input.settings.idleSuspendMin) || input.settings.idleSuspendMin < 0)
    ) {
      return yield* new RavenSettingsError({
        message: "Idle suspend minutes must be a whole number greater than or equal to zero.",
      });
    }

    if (input.keys && Object.keys(input.keys).length > 0) {
      const available = yield* safeStorage.isEncryptionAvailable.pipe(
        Effect.mapError(
          () => new RavenSettingsError({ message: "Encrypted storage could not be checked." }),
        ),
      );
      if (!available) {
        return yield* new RavenSettingsError({
          message: "Raven API keys cannot be saved because encrypted storage is unavailable.",
        });
      }

      const secrets: RavenSecretValues = { ...(yield* readSecrets) };
      for (const name of RAVEN_KEY_NAMES) {
        if (!Object.prototype.hasOwnProperty.call(input.keys, name)) continue;
        const value = input.keys[name]?.trim();
        if (value === undefined || value === "") delete secrets[name];
        else secrets[name] = value;
      }
      const json = yield* encodeRavenSecrets(secrets).pipe(
        Effect.mapError(
          () => new RavenSettingsError({ message: "Raven API keys could not be encoded." }),
        ),
      );
      const encrypted = yield* safeStorage
        .encryptString(json)
        .pipe(
          Effect.mapError(
            () => new RavenSettingsError({ message: "Raven API keys could not be encrypted." }),
          ),
        );
      yield* fileSystem.makeDirectory(directory, { recursive: true });
      yield* fileSystem.writeFile(secretsPath, encrypted, { mode: 0o600 });
      yield* fileSystem.chmod(secretsPath, 0o600);
    }

    if (input.settings) {
      const updates: RavenEnvUpdates = {};
      if (Object.prototype.hasOwnProperty.call(input.settings, "hMode")) {
        updates.RAVEN_H_MODE = input.settings.hMode ?? null;
      }
      if (Object.prototype.hasOwnProperty.call(input.settings, "model")) {
        updates.HAI_MODEL = input.settings.model ?? null;
      }
      if (Object.prototype.hasOwnProperty.call(input.settings, "idleSuspendMin")) {
        updates.RAVEN_IDLE_SUSPEND_MIN =
          input.settings.idleSuspendMin === null || input.settings.idleSuspendMin === undefined
            ? null
            : String(input.settings.idleSuspendMin);
      }
      const existing = yield* readMissingEnvFile(fileSystem, envPath);
      const merged = mergeRavenEnvFile(existing, updates);
      yield* fileSystem.makeDirectory(directory, { recursive: true });
      yield* fileSystem.writeFileString(envPath, merged);
    }
    return yield* get;
  });

  const testKey = Effect.fn("RavenSettingsEnv.testKey")(function* (
    name: RavenKeyName,
    value?: string,
  ) {
    const key = (value === undefined ? (yield* readSecrets)[name] : value)?.trim();
    if (!key) return { ok: false, message: "No key is set." } satisfies RavenKeyTestResult;
    const request = KEY_TEST_REQUESTS[name];
    if (!request) {
      return { ok: false, message: "This key can't be tested." } satisfies RavenKeyTestResult;
    }
    return yield* runKeyTest(httpClient, request(key));
  });

  return RavenSettingsEnv.of({ get, save, ravenCoreEnv, testKey });
});

export const layer = Layer.effect(RavenSettingsEnv, make());
