import type {
  DesktopRavenCoreStatus,
  DesktopRavenKeyName,
  DesktopRavenSettingsSaveInput,
  DesktopRavenSettingsState,
} from "@t3tools/contracts";
import { useEffect, useState } from "react";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { QRCodeSvg } from "../ui/qr-code";
import { SettingsPageContainer, SettingsRow, SettingsSection } from "./settingsLayout";
import { searchableSetting, type SettingsSearchItemId } from "./settingsSearch";

const KEY_ITEMS: ReadonlyArray<{
  readonly name: DesktopRavenKeyName;
  readonly title: string;
  readonly description: string;
  readonly searchId: SettingsSearchItemId;
  readonly testable: boolean;
}> = [
  {
    name: "HAI_API_KEY",
    title: "H Models API key",
    description: "Runs Raven's H computer-use model.",
    searchId: "raven-hai-key",
    testable: true,
  },
  {
    name: "GLADIA_API_KEY",
    title: "Gladia API key",
    description: "Enables voice transcription.",
    searchId: "raven-gladia-key",
    testable: true,
  },
  {
    name: "LINKUP_API_KEY",
    title: "Linkup API key",
    description: "Provides external context after a failed QA run.",
    searchId: "raven-linkup-key",
    testable: false,
  },
  {
    name: "DEVIN_API_KEY",
    title: "Devin API key",
    description: "Enables Devin-assisted code fixes.",
    searchId: "raven-devin-key",
    testable: true,
  },
];

const STATUS_LABELS: Record<DesktopRavenCoreStatus["state"], string> = {
  "setting-up": "Setting up Raven (first launch)…",
  starting: "Starting…",
  ready: "Ready",
  external: "Running outside the app",
  stopped: "Stopped",
};

const STATUS_VARIANTS: Record<
  DesktopRavenCoreStatus["state"],
  "default" | "error" | "info" | "outline" | "success" | "warning"
> = {
  "setting-up": "warning",
  starting: "warning",
  ready: "success",
  external: "info",
  stopped: "error",
};

export function RavenSettings() {
  const bridge = typeof window === "undefined" ? undefined : window.desktopBridge?.raven;
  const [state, setState] = useState<DesktopRavenSettingsState | null>(null);
  const [keyValues, setKeyValues] = useState<Record<DesktopRavenKeyName, string>>({
    HAI_API_KEY: "",
    GLADIA_API_KEY: "",
    LINKUP_API_KEY: "",
    DEVIN_API_KEY: "",
  });
  const [draft, setDraft] = useState<{ idleSuspendMin?: string }>({});
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);

  // oxlint-disable-next-line t3code/no-unreasoned-state-sync -- raven-core status lives in the desktop main process, which pushes no events, so the page polls it while mounted.
  useEffect(() => {
    if (!bridge) return;
    let mounted = true;
    const load = async () => {
      try {
        const next = await bridge.getState();
        if (mounted) setState(next);
      } catch {
        if (mounted) setFeedback("Could not load Raven settings.");
      }
    };
    void load();
    const interval = window.setInterval(() => void load(), 2_000);
    return () => {
      mounted = false;
      window.clearInterval(interval);
    };
  }, [bridge]);

  const idleSuspendMin = draft.idleSuspendMin ?? String(state?.settings.idleSuspendMin ?? 20);
  const settingsDirty = Object.keys(draft).length > 0;

  if (!bridge) {
    return (
      <p className="px-3 py-4 text-sm text-muted-foreground">
        Raven settings are available in the Raven Agent desktop app.
      </p>
    );
  }

  const updateKey = (name: DesktopRavenKeyName, value: string) =>
    setKeyValues((current) => ({ ...current, [name]: value }));
  const keyTitle = (name: DesktopRavenKeyName) =>
    KEY_ITEMS.find((item) => item.name === name)?.title ?? name;

  const run = (action: () => Promise<void>, failure: string) => {
    setBusy(true);
    setFeedback(null);
    void action()
      .catch((error: unknown) => setFeedback(error instanceof Error ? error.message : failure))
      .then(() => setBusy(false));
  };

  const saveKey = (name: DesktopRavenKeyName, value: string | null) =>
    run(async () => {
      const input: DesktopRavenSettingsSaveInput = { keys: { [name]: value } };
      setState(await bridge.save(input));
      updateKey(name, "");
      setFeedback(`${keyTitle(name)} ${value === null ? "cleared" : "saved"}.`);
    }, "Could not save this key.");

  const testKey = (name: DesktopRavenKeyName) =>
    run(async () => {
      const result = await bridge.testKey(name, keyValues[name] || undefined);
      setFeedback(`${keyTitle(name)}: ${result.message}`);
    }, "Could not test this key.");

  const restart = () =>
    run(async () => setState(await bridge.restart()), "Could not restart raven-core.");

  const openLog = () =>
    run(async () => {
      if (!(await bridge.openLog())) setFeedback("Could not open the raven-core log.");
    }, "Could not open the raven-core log.");

  const saveSettings = () => {
    const minutes = Number(idleSuspendMin);
    if (!Number.isInteger(minutes) || minutes < 0) {
      setFeedback("Idle suspend minutes must be a whole number greater than or equal to zero.");
      return;
    }
    run(async () => {
      const input: DesktopRavenSettingsSaveInput = {
        settings: {
          idleSuspendMin: minutes,
        },
      };
      setState(await bridge.save(input));
      setDraft({});
      setFeedback("Idle suspend setting saved.");
    }, "Could not save Raven settings.");
  };

  const status = state?.status;
  return (
    <SettingsPageContainer>
      <SettingsSection title="Raven core">
        <SettingsRow
          {...searchableSetting("raven-core-status")}
          title="Status"
          description={status?.url ?? "Checking raven-core…"}
          status={
            status ? (
              <Badge variant={STATUS_VARIANTS[status.state]}>{STATUS_LABELS[status.state]}</Badge>
            ) : (
              <Badge variant="outline">Checking…</Badge>
            )
          }
          control={
            <div className="flex items-center gap-1">
              {status?.state === "stopped" ? (
                <Button size="xs" variant="outline" disabled={busy} onClick={() => restart()}>
                  Retry
                </Button>
              ) : null}
              <Button
                size="xs"
                variant="outline"
                disabled={!status || busy}
                onClick={() => openLog()}
              >
                Open log
              </Button>
            </div>
          }
        />
        {status?.state === "stopped" && status.message ? (
          <details className="px-4 pb-3 text-xs text-muted-foreground">
            <summary className="cursor-pointer">Last output</summary>
            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted/40 p-3 font-mono">
              {status.message}
            </pre>
          </details>
        ) : null}
        {status?.state === "external" ? (
          <p className="px-4 pb-3 text-xs text-muted-foreground">
            raven-core was started outside Raven Agent; restart it to apply API key or idle-setting
            changes. Execution defaults apply to future runs without a restart.
          </p>
        ) : null}
      </SettingsSection>

      {state?.pairing ? (
        <SettingsSection title="iPhone">
          <SettingsRow
            {...searchableSetting("raven-pair-iphone")}
            title="Pair your iPhone"
            description={`Scan with the iPhone camera to connect the Raven app to this Mac${
              state.pairing.urls[0] ? ` (${state.pairing.urls[0]})` : ""
            }. Both devices must be on the same network or tailnet.`}
            control={
              <QRCodeSvg
                value={`raven://pair?${new URLSearchParams([
                  ["token", state.pairing.token],
                  ...state.pairing.urls.map((url): [string, string] => ["url", url]),
                ]).toString()}`}
                size={132}
              />
            }
          />
        </SettingsSection>
      ) : null}

      <SettingsSection title="API keys">
        {KEY_ITEMS.map((item) => (
          <SettingsRow
            key={item.name}
            {...searchableSetting(item.searchId)}
            title={item.title}
            description={
              item.testable
                ? item.description
                : `${item.description} Key verification is currently unavailable.`
            }
            control={
              <div className="flex min-w-0 flex-wrap items-center justify-end gap-1.5">
                <Input
                  type="password"
                  autoComplete="new-password"
                  className="w-44"
                  value={keyValues[item.name]}
                  placeholder={
                    state?.keys[item.name].set
                      ? `•••• ${state.keys[item.name].last4 ?? ""}`
                      : "Not set"
                  }
                  aria-label={item.title}
                  onChange={(event) => updateKey(item.name, event.target.value)}
                />
                {item.testable ? (
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={busy || !state}
                    onClick={() => testKey(item.name)}
                  >
                    Test
                  </Button>
                ) : null}
                <Button
                  size="xs"
                  variant="outline"
                  disabled={busy || keyValues[item.name].length === 0}
                  onClick={() => saveKey(item.name, keyValues[item.name])}
                >
                  Save
                </Button>
                <Button
                  size="xs"
                  variant="ghost"
                  disabled={busy || !state?.keys[item.name].set}
                  onClick={() => saveKey(item.name, null)}
                >
                  Clear
                </Button>
              </div>
            }
          />
        ))}
      </SettingsSection>

      <SettingsSection title="Execution defaults">
        <SettingsRow
          {...searchableSetting("raven-h-mode")}
          title="H mode"
          description="Managed by Raven core. Expand Execution at the bottom of the sidebar to choose the H decision loop and per-platform overrides."
          control={<span className="text-xs text-muted-foreground">Sidebar → Execution</span>}
        />
        <SettingsRow
          {...searchableSetting("raven-model")}
          title="Model"
          description="Choose the Holo model in the sidebar Execution panel. Core saves the next-run default; active runs keep their captured model. No sidecar restart is needed."
          control={<span className="text-xs text-muted-foreground">Sidebar → Execution</span>}
        />
      </SettingsSection>

      <SettingsSection title="Machine settings">
        <SettingsRow
          {...searchableSetting("raven-idle-suspend")}
          title="Idle suspend minutes"
          description="Suspend idle test machines after this many minutes. Set to zero to disable."
          control={
            <Input
              type="number"
              min={0}
              step={1}
              className="w-24"
              value={idleSuspendMin}
              aria-label="Idle suspend minutes"
              onChange={(event) =>
                setDraft((current) => ({ ...current, idleSuspendMin: event.target.value }))
              }
            />
          }
        />
        <div className="flex justify-end px-4 py-2">
          <Button
            size="sm"
            variant="outline"
            disabled={busy || !settingsDirty}
            onClick={() => saveSettings()}
          >
            Save idle setting
          </Button>
        </div>
      </SettingsSection>

      {feedback ? (
        <p className="px-4 text-xs text-muted-foreground" role="status">
          {feedback}
        </p>
      ) : null}
      <span className="sr-only" aria-live="polite">
        {state ? STATUS_LABELS[state.status.state] : "Loading Raven settings"}
      </span>
    </SettingsPageContainer>
  );
}
