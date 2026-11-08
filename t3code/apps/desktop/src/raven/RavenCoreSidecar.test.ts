import { describe, expect, it } from "vite-plus/test";

import {
  ravenCoreSyncStamp,
  resolveRavenCoreLaunch,
  shouldSyncRavenCore,
} from "./RavenCoreSidecar.ts";

const path = {
  dirname: (value: string) => {
    const index = value.lastIndexOf("/");
    return index <= 0 ? "/" : value.slice(0, index);
  },
  join: (...parts: ReadonlyArray<string>) => parts.join("/").replace(/\/+/g, "/"),
  resolve: (...parts: ReadonlyArray<string>) => parts.join("/").replace(/\/+/g, "/"),
};

const launchInput = {
  path,
  isPackaged: false,
  appRoot: "/repo/t3code/apps/desktop",
  resourcesPath: "/resources",
  appVersion: "1.2.3",
  userDataPath: "/userdata",
  processEnv: {},
  ravenCoreProject: "/repo/raven-core",
} as const;

describe("resolveRavenCoreLaunch", () => {
  it("resolves a dev checkout with the default port and loopback host", () => {
    const plan = resolveRavenCoreLaunch(launchInput);

    expect(plan).toEqual({
      mode: "dev",
      port: 8787,
      host: "0.0.0.0",
      url: "http://127.0.0.1:8787",
      logPath: "/repo/artifacts/logs/raven-core.log",
      serve: {
        command: "uv",
        args: [
          "run",
          "--project",
          "/repo/raven-core",
          "raven-core",
          "serve",
          "--host",
          "0.0.0.0",
          "--port",
          "8787",
        ],
        env: {},
        cwd: "/repo",
      },
    });
  });

  it("resolves packaged paths and honors the port and token", () => {
    const plan = resolveRavenCoreLaunch({
      ...launchInput,
      isPackaged: true,
      processEnv: { RAVEN_PORT: "8797", RAVEN_TOKEN: "token" },
    });

    expect(plan).toEqual({
      mode: "packaged",
      port: 8797,
      host: "0.0.0.0",
      url: "http://127.0.0.1:8797",
      dataDir: "/userdata/raven",
      logPath: "/userdata/raven/artifacts/logs/raven-core.log",
      sync: {
        command: "/resources/raven/bin/uv",
        args: [
          "sync",
          "--frozen",
          "--no-dev",
          "--no-editable",
          "--project",
          "/resources/raven/raven-core",
        ],
        env: {
          UV_PROJECT_ENVIRONMENT: "/userdata/raven/venv",
          UV_PYTHON_INSTALL_DIR: "/userdata/raven/python",
          UV_CACHE_DIR: "/userdata/raven/uv-cache",
          UV_PYTHON_PREFERENCE: "only-managed",
        },
      },
      serve: {
        command: "/userdata/raven/venv/bin/raven-core",
        args: ["serve", "--host", "0.0.0.0", "--port", "8797"],
        env: {
          RAVEN_DATA_DIR: "/userdata/raven",
          RAVEN_SPECS_DIR: "/resources/raven/specs",
          RAVEN_ENV_FILE: "/userdata/raven/raven.env",
          PYTHONDONTWRITEBYTECODE: "1",
        },
        cwd: "/userdata/raven",
      },
    });
  });

  it("uses the configured project and binds publicly only when a token is set", () => {
    const withoutToken = resolveRavenCoreLaunch({
      ...launchInput,
      processEnv: { RAVEN_PORT: "8797", RAVEN_CORE_PROJECT: "/repo/raven-core" },
    });
    const withToken = resolveRavenCoreLaunch({
      ...launchInput,
      processEnv: {
        RAVEN_PORT: "8797",
        RAVEN_TOKEN: "token",
        RAVEN_CORE_PROJECT: "/custom/raven-core",
      },
    });

    expect(withoutToken?.host).toBe("0.0.0.0");
    expect(withoutToken?.port).toBe(8797);
    expect(withToken?.host).toBe("0.0.0.0");
    expect(withToken?.serve.args).toContain("/custom/raven-core");
  });
});

describe("shouldSyncRavenCore", () => {
  it("syncs for a missing or changed stamp and skips a matching stamp", () => {
    const currentHash = "a".repeat(64);
    const changedHash = "b".repeat(64);
    const currentStamp = ravenCoreSyncStamp("1.2.3", currentHash);

    expect(shouldSyncRavenCore(undefined, currentStamp)).toBe(true);
    expect(shouldSyncRavenCore(currentStamp, currentStamp)).toBe(false);
    expect(shouldSyncRavenCore(currentStamp, ravenCoreSyncStamp("1.2.3", changedHash))).toBe(true);
  });
});
