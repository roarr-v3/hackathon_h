import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Cause from "effect/Cause";
import * as Context from "effect/Context";
import * as Deferred from "effect/Deferred";
import * as Digest from "effect/crypto/Digest";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Ref from "effect/Ref";
import * as Schema from "effect/Schema";
import * as Scope from "effect/Scope";
import * as Semaphore from "effect/Semaphore";
import * as Stream from "effect/Stream";
import * as SubscriptionRef from "effect/SubscriptionRef";
import { HttpClient, HttpClientRequest } from "effect/http";
import { ChildProcess, ChildProcessSpawner } from "effect/process";

import * as DesktopAppIdentity from "../app/DesktopAppIdentity.ts";
import * as DesktopEnvironment from "../app/DesktopEnvironment.ts";
import * as DesktopObservability from "../app/DesktopObservability.ts";
import * as DesktopUserData from "../app/DesktopUserData.ts";
import { ravenSettingsDir } from "./RavenPaths.ts";
import * as RavenSettingsEnv from "./RavenSettingsEnv.ts";

const STARTUP_TIMEOUT = Duration.seconds(60);
const HEALTH_PROBE_TIMEOUT = Duration.seconds(1);
const HEALTH_PROBE_INTERVAL = Duration.millis(250);
const TERMINATE_GRACE = Duration.seconds(5);
const MAX_OUTPUT_LENGTH = 4_000;
const MAX_START_FAILURES = 3;

const RavenCoreHealthResponse = Schema.Struct({ ok: Schema.Literal(true) });
const isRavenCoreHealthResponse = Schema.is(RavenCoreHealthResponse);

const { logInfo } = DesktopObservability.makeComponentLogger("raven-core");

type RavenPath = Pick<Path.Path, "dirname" | "join" | "resolve">;

export interface RavenCoreStatus {
  readonly state: "setting-up" | "starting" | "ready" | "external" | "stopped";
  readonly message?: string;
  readonly logPath: string;
  readonly url: string;
}

export interface RavenCoreLaunchInput {
  readonly path: RavenPath;
  readonly isPackaged: boolean;
  readonly appRoot: string;
  readonly resourcesPath: string;
  readonly appVersion: string;
  readonly userDataPath: string;
  readonly processEnv: Readonly<Record<string, string | undefined>>;
  readonly ravenCoreProject?: string;
}

interface RavenCoreLaunchPlanBase {
  readonly port: number;
  readonly host: string;
  readonly url: string;
  readonly logPath: string;
  readonly serve: {
    readonly command: string;
    readonly args: ReadonlyArray<string>;
    readonly env: Readonly<Record<string, string>>;
    readonly cwd: string;
  };
}

export interface RavenCorePackagedLaunchPlan extends RavenCoreLaunchPlanBase {
  readonly mode: "packaged";
  readonly dataDir: string;
  readonly sync: {
    readonly command: string;
    readonly args: ReadonlyArray<string>;
    readonly env: Readonly<Record<string, string>>;
  };
}

export interface RavenCoreDevLaunchPlan extends RavenCoreLaunchPlanBase {
  readonly mode: "dev";
  readonly dataDir?: never;
  readonly sync?: never;
}

export type RavenCoreLaunchPlan = RavenCorePackagedLaunchPlan | RavenCoreDevLaunchPlan;

const ravenCorePort = (processEnv: Readonly<Record<string, string | undefined>>): number => {
  const port = Number(processEnv.RAVEN_PORT ?? 8787);
  return Number.isInteger(port) && port > 0 && port <= 65_535 ? port : 8787;
};

export const ravenCoreUrl = (processEnv: Readonly<Record<string, string | undefined>>): string =>
  `http://127.0.0.1:${ravenCorePort(processEnv)}`;

export const resolveRavenCoreLaunch = (
  input: RavenCoreLaunchInput,
): RavenCoreLaunchPlan | undefined => {
  const port = ravenCorePort(input.processEnv);
  const host = "0.0.0.0";
  const url = `http://127.0.0.1:${port}`;
  const { path } = input;

  if (input.isPackaged) {
    const root = path.join(input.resourcesPath, "raven");
    const dataDir = ravenSettingsDir(path, input.userDataPath);
    const syncEnv = {
      UV_PROJECT_ENVIRONMENT: path.join(dataDir, "venv"),
      UV_PYTHON_INSTALL_DIR: path.join(dataDir, "python"),
      UV_CACHE_DIR: path.join(dataDir, "uv-cache"),
      UV_PYTHON_PREFERENCE: "only-managed",
    };
    const serveEnv = {
      RAVEN_DATA_DIR: dataDir,
      RAVEN_SPECS_DIR: path.join(root, "specs"),
      RAVEN_ENV_FILE: path.join(dataDir, "raven.env"),
      PYTHONDONTWRITEBYTECODE: "1",
    };
    return {
      mode: "packaged",
      port,
      host,
      url,
      dataDir,
      logPath: path.join(dataDir, "artifacts", "logs", "raven-core.log"),
      sync: {
        command: path.join(root, "bin", "uv"),
        args: [
          "sync",
          "--frozen",
          "--no-dev",
          "--no-editable",
          "--project",
          path.join(root, "raven-core"),
        ],
        env: syncEnv,
      },
      serve: {
        command: path.join(dataDir, "venv", "bin", "raven-core"),
        args: ["serve", "--host", host, "--port", String(port)],
        env: serveEnv,
        cwd: dataDir,
      },
    };
  }

  const project =
    input.processEnv.RAVEN_CORE_PROJECT?.trim() || input.ravenCoreProject?.trim() || undefined;
  if (!project) return undefined;
  const ravenCoreProject = path.resolve(project);
  const repoRoot = path.dirname(ravenCoreProject);
  return {
    mode: "dev",
    port,
    host,
    url,
    logPath: path.join(repoRoot, "artifacts", "logs", "raven-core.log"),
    serve: {
      command: "uv",
      args: [
        "run",
        "--project",
        ravenCoreProject,
        "raven-core",
        "serve",
        "--host",
        host,
        "--port",
        String(port),
      ],
      env: {},
      cwd: repoRoot,
    },
  };
};

export const ravenCoreSyncStamp = (appVersion: string, lockHash: string): string =>
  `${appVersion}:${lockHash}`;

export const shouldSyncRavenCore = (
  currentStamp: string | undefined,
  expectedStamp: string,
): boolean => currentStamp !== expectedStamp;

const findRavenCoreProject = Effect.fnUntraced(function* (input: {
  readonly appRoot: string;
  readonly configuredProject?: string;
}): Effect.fn.Return<string | undefined, never, FileSystem.FileSystem | Path.Path> {
  const fileSystem = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const configuredProject = input.configuredProject?.trim();
  if (configuredProject) return path.resolve(configuredProject);

  for (let directory = path.resolve(input.appRoot); ; directory = path.dirname(directory)) {
    const candidate = path.join(directory, "raven-core");
    const exists = yield* fileSystem
      .exists(path.join(candidate, "pyproject.toml"))
      .pipe(Effect.orElseSucceed(() => false));
    if (exists) return candidate;
    const parent = path.dirname(directory);
    if (parent === directory) return undefined;
  }
});

export const resolveRavenCoreLaunchEffect = Effect.fnUntraced(function* (
  processEnv: Readonly<Record<string, string | undefined>>,
): Effect.fn.Return<
  RavenCoreLaunchPlan | undefined,
  DesktopUserData.DesktopUserDataInitializationError,
  | DesktopEnvironment.DesktopEnvironment
  | DesktopAppIdentity.DesktopAppIdentity
  | FileSystem.FileSystem
  | Path.Path
> {
  const environment = yield* DesktopEnvironment.DesktopEnvironment;
  const appIdentity = yield* DesktopAppIdentity.DesktopAppIdentity;
  const userDataPath = yield* appIdentity.resolveUserDataPath;
  const ravenCoreProject = environment.isPackaged
    ? undefined
    : yield* findRavenCoreProject({
        appRoot: environment.appRoot,
        ...(processEnv.RAVEN_CORE_PROJECT === undefined
          ? {}
          : { configuredProject: processEnv.RAVEN_CORE_PROJECT }),
      });
  const projectInput = ravenCoreProject === undefined ? {} : { ravenCoreProject };
  return resolveRavenCoreLaunch({
    path: environment.path,
    isPackaged: environment.isPackaged,
    appRoot: environment.appRoot,
    resourcesPath: environment.resourcesPath,
    appVersion: environment.appVersion,
    userDataPath,
    processEnv,
    ...projectInput,
  });
});

export class RavenCoreSidecar extends Context.Service<
  RavenCoreSidecar,
  {
    readonly status: SubscriptionRef.SubscriptionRef<RavenCoreStatus>;
    readonly start: Effect.Effect<void>;
    readonly restart: Effect.Effect<void>;
    readonly stop: Effect.Effect<void>;
  }
>()("@t3tools/desktop/raven/RavenCoreSidecar") {}

/** @public */
export const make = Effect.fn("RavenCoreSidecar.make")(function* () {
  const environment = yield* DesktopEnvironment.DesktopEnvironment;
  const appIdentity = yield* DesktopAppIdentity.DesktopAppIdentity;
  const processEnv = yield* HostProcess.Environment;
  const fileSystem = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const spawner = yield* ChildProcessSpawner.ChildProcessSpawner;
  const httpClient = yield* HttpClient.HttpClient;
  const digest = yield* Digest.Digest;
  const ravenSettingsEnv = yield* RavenSettingsEnv.RavenSettingsEnv;
  const scope = yield* Scope.Scope;
  let launchPlan: RavenCoreLaunchPlan | undefined;
  const statusUrl = ravenCoreUrl(processEnv);
  const statusLogPath = path.join(environment.rootDir, "artifacts", "logs", "raven-core.log");
  const status = yield* SubscriptionRef.make<RavenCoreStatus>({
    state: "stopped",
    logPath: statusLogPath,
    url: statusUrl,
  });
  const outputTail = yield* Ref.make("");
  const supervisorFiber = yield* Ref.make<Option.Option<Fiber.Fiber<void>>>(Option.none());
  const desiredRunning = yield* Ref.make(false);
  const supervising = yield* Ref.make(false);
  const operations = yield* Semaphore.make(1);

  const transition = Effect.fn("RavenCoreSidecar.transition")(function* (
    state: RavenCoreStatus["state"],
    message?: string,
  ) {
    const current = yield* SubscriptionRef.get(status);
    const next: RavenCoreStatus = {
      state,
      ...(message === undefined ? {} : { message }),
      logPath: launchPlan?.logPath ?? statusLogPath,
      url: launchPlan?.url ?? statusUrl,
    };
    if (
      current.state === next.state &&
      current.message === next.message &&
      current.logPath === next.logPath &&
      current.url === next.url
    ) {
      return;
    }
    yield* SubscriptionRef.set(status, next);
    yield* logInfo("raven-core status changed", {
      state: next.state,
      url: next.url,
      logPath: next.logPath,
    });
  });

  const appendOutput = (logPath: string, decoder: TextDecoder) => (chunk: Uint8Array) =>
    Effect.gen(function* () {
      const text = decoder.decode(chunk, { stream: true });
      if (text.length > 0) {
        yield* Ref.update(outputTail, (current) => `${current}${text}`.slice(-MAX_OUTPUT_LENGTH));
      }
      yield* fileSystem.writeFile(logPath, chunk, { flag: "a" });
    });

  const spawnCaptured = Effect.fn("RavenCoreSidecar.spawnCaptured")(function* (
    command: string,
    args: ReadonlyArray<string>,
    cwd: string,
    env: Readonly<Record<string, string>>,
    logPath: string,
  ) {
    yield* fileSystem.makeDirectory(path.dirname(logPath), { recursive: true });
    const settingEnv = yield* ravenSettingsEnv.ravenCoreEnv;
    const handle = yield* spawner.spawn(
      ChildProcess.make(command, args, {
        cwd,
        env: { ...env, ...settingEnv },
        extendEnv: true,
        stdin: "ignore",
        stdout: "pipe",
        stderr: "pipe",
        killSignal: "SIGTERM",
        forceKillAfter: TERMINATE_GRACE,
      }),
    );
    const stdoutFiber = yield* handle.stdout.pipe(
      Stream.runForEach(appendOutput(logPath, new TextDecoder())),
      Effect.ignoreCause,
      Effect.forkScoped,
    );
    const stderrFiber = yield* handle.stderr.pipe(
      Stream.runForEach(appendOutput(logPath, new TextDecoder())),
      Effect.ignoreCause,
      Effect.forkScoped,
    );
    yield* Effect.addFinalizer(() =>
      handle.kill({ killSignal: "SIGTERM", forceKillAfter: TERMINATE_GRACE }).pipe(Effect.ignore),
    );
    return { handle, outputFibers: [stdoutFiber, stderrFiber] as const };
  });

  const waitForOutput = (outputFibers: ReadonlyArray<Fiber.Fiber<void>>) =>
    Effect.forEach(outputFibers, Fiber.await, {
      concurrency: "unbounded",
      discard: true,
    });

  const runSync = (plan: RavenCorePackagedLaunchPlan) =>
    Effect.scoped(
      Effect.gen(function* () {
        yield* Ref.set(outputTail, "");
        const { handle, outputFibers } = yield* spawnCaptured(
          plan.sync.command,
          plan.sync.args,
          plan.dataDir,
          plan.sync.env,
          plan.logPath,
        );
        const exit = yield* handle.exitCode.pipe(Effect.exit);
        yield* waitForOutput(outputFibers);
        return Exit.isSuccess(exit) ? Number(exit.value) : undefined;
      }),
    );

  const healthCheck = (url: string) =>
    httpClient.execute(HttpClientRequest.get(`${url}/health`)).pipe(
      Effect.flatMap((response) =>
        response.status === 200
          ? response.json.pipe(Effect.map((body) => isRavenCoreHealthResponse(body)))
          : Effect.succeed(false),
      ),
      Effect.timeoutOption(HEALTH_PROBE_TIMEOUT),
      Effect.map(Option.getOrElse(() => false)),
      Effect.orElseSucceed(() => false),
    );

  const waitForReady = (url: string) =>
    healthCheck(url).pipe(
      Effect.flatMap((ready) =>
        ready ? Effect.succeed(true) : Effect.sleep(HEALTH_PROBE_INTERVAL).pipe(Effect.as(false)),
      ),
      Effect.repeat({ while: (ready) => !ready }),
      Effect.timeoutOption(STARTUP_TIMEOUT),
      Effect.map(Option.getOrElse(() => false)),
    );

  const runServe = (plan: RavenCoreLaunchPlan) =>
    Effect.scoped(
      Effect.gen(function* () {
        yield* Ref.set(outputTail, "");
        const { handle, outputFibers } = yield* spawnCaptured(
          plan.serve.command,
          plan.serve.args,
          plan.serve.cwd,
          plan.serve.env,
          plan.logPath,
        );
        const exited = handle.exitCode.pipe(
          Effect.as("exited" as const),
          Effect.orElseSucceed(() => "exited" as const),
        );
        const startup = yield* Effect.raceFirst(
          waitForReady(plan.url).pipe(
            Effect.map((ready) => ({ _tag: "readiness" as const, ready })),
          ),
          exited.pipe(Effect.as({ _tag: "exited" as const })),
        );
        if (startup._tag === "exited") {
          yield* waitForOutput(outputFibers);
          return false;
        }
        if (!startup.ready) {
          yield* handle
            .kill({ killSignal: "SIGTERM", forceKillAfter: TERMINATE_GRACE })
            .pipe(Effect.ignore);
          yield* waitForOutput(outputFibers);
          return false;
        }
        yield* transition("ready");
        yield* exited;
        yield* waitForOutput(outputFibers);
        return true;
      }),
    );

  const runSupervisor = Effect.fn("RavenCoreSidecar.supervise")(function* () {
    launchPlan = yield* resolveRavenCoreLaunchEffect(processEnv).pipe(
      Effect.orElseSucceed(() => undefined),
    );
    const url = launchPlan?.url ?? statusUrl;
    const external = yield* healthCheck(url);
    if (external) {
      yield* transition("external");
      return;
    }
    if (!launchPlan) {
      yield* transition("stopped", "raven-core not found");
      return;
    }

    let failuresWithoutReadiness = 0;
    while (yield* Ref.get(desiredRunning)) {
      if (launchPlan.mode === "packaged") {
        yield* transition("setting-up");
        const lockPath = path.join(environment.resourcesPath, "raven", "raven-core", "uv.lock");
        const lockFile = yield* fileSystem.readFileString(lockPath);
        const lockHash = yield* digest.digestHex(Digest.sha256, new TextEncoder().encode(lockFile));
        const stamp = ravenCoreSyncStamp(environment.appVersion, lockHash);
        const stampPath = path.join(launchPlan.dataDir, "venv", ".raven-stamp");
        const currentStamp = yield* fileSystem.readFileString(stampPath).pipe(Effect.option);
        if (shouldSyncRavenCore(Option.getOrUndefined(currentStamp), stamp)) {
          yield* fileSystem.makeDirectory(path.dirname(stampPath), { recursive: true });
          const exitCode = yield* runSync(launchPlan);
          if (exitCode !== 0) {
            const output = yield* Ref.get(outputTail);
            yield* transition("stopped", output || "raven-core sync failed");
            return;
          }
          yield* fileSystem.writeFileString(stampPath, stamp);
        }
      }

      if (!(yield* Ref.get(desiredRunning))) return;
      yield* transition("starting");
      const ready = yield* runServe(launchPlan);
      if (!(yield* Ref.get(desiredRunning))) return;
      if (ready) {
        failuresWithoutReadiness = 0;
      } else {
        failuresWithoutReadiness += 1;
        if (failuresWithoutReadiness >= MAX_START_FAILURES) {
          const output = yield* Ref.get(outputTail);
          yield* transition("stopped", output || "raven-core failed to become ready");
          return;
        }
      }
      yield* transition("starting");
      yield* Effect.sleep(
        Duration.millis(Math.min(500 * 2 ** Math.max(failuresWithoutReadiness - 1, 0), 10_000)),
      );
    }
  });

  const supervise = runSupervisor().pipe(
    Effect.catchCause((cause) =>
      Effect.gen(function* () {
        if (!(yield* Ref.get(desiredRunning))) return;
        const output = yield* Ref.get(outputTail);
        yield* transition("stopped", output || Cause.pretty(cause));
      }),
    ),
  );

  const startSupervisor = Effect.gen(function* () {
    if (yield* Ref.get(supervising)) return;
    yield* Ref.set(desiredRunning, true);
    yield* Ref.set(supervising, true);
    const startGate = yield* Deferred.make<void>();
    const fiber = yield* Effect.forkIn(
      Effect.gen(function* () {
        yield* Deferred.await(startGate);
        yield* supervise.pipe(
          Effect.ensuring(
            Effect.gen(function* () {
              yield* Ref.set(supervising, false);
              yield* Ref.set(supervisorFiber, Option.none());
            }),
          ),
        );
      }),
      scope,
    );
    yield* Ref.set(supervisorFiber, Option.some(fiber));
    yield* Deferred.succeed(startGate, undefined);
  });

  const stopSupervisor = Effect.gen(function* () {
    yield* Ref.set(desiredRunning, false);
    const fiber = yield* Ref.get(supervisorFiber);
    if (Option.isSome(fiber)) yield* Fiber.interrupt(fiber.value);
    yield* Ref.set(supervisorFiber, Option.none());
    yield* Ref.set(supervising, false);
    yield* transition("stopped");
  });

  const start = operations
    .withPermits(1)(startSupervisor)
    .pipe(
      Effect.provideService(DesktopEnvironment.DesktopEnvironment, environment),
      Effect.provideService(DesktopAppIdentity.DesktopAppIdentity, appIdentity),
      Effect.provideService(FileSystem.FileSystem, fileSystem),
      Effect.provideService(Path.Path, path),
    );
  const stop = operations.withPermits(1)(stopSupervisor);
  const restart = operations
    .withPermits(1)(
      Effect.gen(function* () {
        yield* stopSupervisor;
        yield* startSupervisor;
      }),
    )
    .pipe(
      Effect.provideService(DesktopEnvironment.DesktopEnvironment, environment),
      Effect.provideService(DesktopAppIdentity.DesktopAppIdentity, appIdentity),
      Effect.provideService(FileSystem.FileSystem, fileSystem),
      Effect.provideService(Path.Path, path),
    );

  yield* Effect.addFinalizer(() => stopSupervisor);

  return RavenCoreSidecar.of({ status, start, restart, stop });
});

export const layer = Layer.effect(RavenCoreSidecar, make());
