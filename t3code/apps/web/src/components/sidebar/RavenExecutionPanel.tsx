import type {
  DesktopRavenExecutionDevice,
  DesktopRavenExecutionProfile,
  DesktopRavenExecutionState,
} from "@t3tools/contracts";
import { Link } from "@tanstack/react-router";
import { ChevronDownIcon, SlidersHorizontalIcon } from "lucide-react";
import { useEffect, useId, useReducer, useState, type ReactNode } from "react";

import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from "../ui/collapsible";
import { Input } from "../ui/input";
import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from "../ui/select";
import { Switch } from "../ui/switch";

type Platform = DesktopRavenExecutionState["targets"][number]["platform"];
type Operation = "load" | "save" | "check";

const PLATFORMS = [
  { value: "macos", label: "Mac" },
  { value: "ios", label: "iOS" },
  { value: "linux", label: "Linux" },
  { value: "windows", label: "Windows" },
] as const;

const H_MODES = [
  { value: "auto", label: "Auto" },
  { value: "local-runtime", label: "H local runtime" },
  { value: "agents-api", label: "H hosted Agents API" },
  { value: "models-loop", label: "Holo Models API" },
] as const;

const H_MODELS = [
  { value: "holo4-27b", label: "Holo4 27B" },
  { value: "holo4-35b-a3b", label: "Holo4 35B-A3B" },
] as const;

const H_MODE_DETAILS: Record<DesktopRavenExecutionProfile["h_mode"], string> = {
  auto: "Core chooses a supported H mode for each target.",
  "local-runtime": "H's agent loop runs inside the test machine.",
  "agents-api": "H hosts the agent loop; the target must support a hosted connection.",
  "models-loop": "Raven runs the Holo Models API loop with Cua/native drivers.",
};

interface RavenExecutionPanelState {
  readonly saved: DesktopRavenExecutionState | null;
  readonly draft: DesktopRavenExecutionProfile | null;
  readonly pending: Operation | null;
  readonly error: string | null;
  readonly notice: string | null;
}

export const initialExecutionPanelState: RavenExecutionPanelState = {
  saved: null,
  draft: null,
  pending: "load",
  error: null,
  notice: null,
};

type ExecutionAction =
  | { readonly type: "start"; readonly operation: Operation }
  | {
      readonly type: "loaded";
      readonly operation: Operation;
      readonly value: DesktopRavenExecutionState;
    }
  | { readonly type: "failed"; readonly message: string }
  | { readonly type: "edit"; readonly patch: Partial<DesktopRavenExecutionProfile> };

export function ravenExecutionReducer(
  state: RavenExecutionPanelState,
  action: ExecutionAction,
): RavenExecutionPanelState {
  switch (action.type) {
    case "start":
      return { ...state, pending: action.operation, notice: null };
    case "loaded":
      if (state.pending !== action.operation) return state;
      return {
        saved: action.value,
        draft: null,
        pending: null,
        error: null,
        notice: action.operation === "save" ? "Saved in core for the next run." : null,
      };
    case "failed":
      return { ...state, pending: null, error: action.message, notice: null };
    case "edit": {
      const current = state.draft ?? state.saved?.profile;
      if (!current || state.pending) return state;
      const draft = { ...current, ...action.patch };
      if (draft.ios_target !== current.ios_target) draft.ios_device_id = null;
      return { ...state, draft, notice: null };
    }
  }
}

export function ravenExecutionReadiness(state: RavenExecutionPanelState, platform: Platform) {
  if (state.pending) {
    return { status: "Not checked", detail: "Waiting for Raven core…", target: null };
  }
  if (state.draft) {
    return {
      status: "Not checked",
      detail: "Unsaved changes. Save before checking this configuration.",
      target: null,
    };
  }
  if (state.error || !state.saved) {
    return { status: "Unknown", detail: "Reload or check core readiness.", target: null };
  }
  const target = state.saved.targets.find((entry) => entry.platform === platform);
  return {
    status: target?.status || "Not checked",
    detail: target?.detail || "Core did not return readiness details for this platform.",
    target: target?.target ?? null,
  };
}

export function ravenExecutionDeviceChoices(
  profile: DesktopRavenExecutionProfile,
  devices: ReadonlyArray<DesktopRavenExecutionDevice> | null,
) {
  const choices = (devices ?? [])
    .filter((device) => device.kind === profile.ios_target)
    .map((device) => ({
      value: device.id,
      label: `${device.name}${device.available ? "" : " (unavailable)"}`,
      disabled: !device.available,
    }));
  if (profile.ios_device_id && !choices.some((choice) => choice.value === profile.ios_device_id)) {
    choices.unshift({
      value: profile.ios_device_id,
      label: `${profile.ios_device_id} (${devices === null ? "not checked" : "not in DeviceHub list"})`,
      disabled: true,
    });
  }
  return [{ value: "", label: "No device selected", disabled: false }, ...choices];
}

function ExecutionSelect<Value extends string>({
  label,
  value,
  items,
  disabled,
  onChange,
}: {
  label: string;
  value: Value;
  items: ReadonlyArray<{ value: Value; label: string; disabled?: boolean }>;
  disabled: boolean;
  onChange: (value: Value) => void;
}) {
  const id = useId();
  return (
    <div className="grid min-w-0 gap-1">
      <label className="text-xs text-muted-foreground" htmlFor={id}>
        {label}
      </label>
      <Select
        disabled={disabled}
        items={items}
        value={value}
        onValueChange={(next) => {
          if (next !== null) onChange(next);
        }}
      >
        <SelectTrigger id={id} size="compact">
          <SelectValue />
        </SelectTrigger>
        <SelectPopup alignItemWithTrigger={false}>
          {items.map((item) => (
            <SelectItem key={item.value} value={item.value} disabled={item.disabled}>
              {item.label}
            </SelectItem>
          ))}
        </SelectPopup>
      </Select>
    </div>
  );
}

function ExecutionTarget({
  platform,
  state,
  children,
}: {
  platform: Platform;
  state: RavenExecutionPanelState;
  children: ReactNode;
}) {
  const readiness = ravenExecutionReadiness(state, platform);
  const variant =
    readiness.status === "ready"
      ? "success"
      : /unavailable|unsupported|error|failed/.test(readiness.status)
        ? "error"
        : "warning";
  return (
    <section className="grid min-w-0 gap-2 border-t border-border/60 pt-3">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-1">
        <h4 className="text-xs font-medium">
          {PLATFORMS.find((entry) => entry.value === platform)?.label}
        </h4>
        <Badge size="sm" variant={variant}>
          {readiness.status}
        </Badge>
      </div>
      {children}
      <div className="space-y-1 text-2xs text-muted-foreground" aria-live="polite">
        {readiness.target ? <p className="break-words">Core target: {readiness.target}</p> : null}
        <p className="break-words">{readiness.detail}</p>
      </div>
    </section>
  );
}

function executionErrorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Raven core could not complete this request. Reload to confirm the saved defaults.";
}

export function RavenExecutionPanel() {
  const desktopRaven = typeof window === "undefined" ? undefined : window.desktopBridge?.raven;
  const bridge =
    typeof desktopRaven?.getExecution === "function" &&
    typeof desktopRaven.saveExecution === "function" &&
    typeof desktopRaven.checkExecution === "function" &&
    typeof desktopRaven.listDevices === "function"
      ? desktopRaven
      : undefined;
  const [open, setOpen] = useState(false);
  const [showOverrides, setShowOverrides] = useState<boolean | null>(null);
  const [state, dispatch] = useReducer(ravenExecutionReducer, initialExecutionPanelState);
  const [devices, setDevices] = useState<ReadonlyArray<DesktopRavenExecutionDevice> | null>(null);
  const [deviceError, setDeviceError] = useState<string | null>(null);
  const [devicesLoading, setDevicesLoading] = useState(Boolean(bridge));
  const spaceId = useId();
  const fallbackId = useId();

  // oxlint-disable-next-line t3code/no-unreasoned-state-sync -- Execution defaults and DeviceHub inventory are owned by core, not renderer storage.
  useEffect(() => {
    if (!bridge) return;
    let active = true;
    void bridge.getExecution().then(
      (value) => {
        if (active) dispatch({ type: "loaded", operation: "load", value });
      },
      (error: unknown) => {
        if (active) dispatch({ type: "failed", message: executionErrorMessage(error) });
      },
    );
    void bridge.listDevices().then(
      (value) => {
        if (!active) return;
        setDevices(value);
        setDeviceError(null);
        setDevicesLoading(false);
      },
      (error: unknown) => {
        if (!active) return;
        setDeviceError(executionErrorMessage(error));
        setDevicesLoading(false);
      },
    );
    return () => {
      active = false;
    };
  }, [bridge]);

  const profile = state.draft ?? state.saved?.profile;
  const models =
    profile && !H_MODELS.some((item) => item.value === profile.model)
      ? [...H_MODELS, { value: profile.model, label: profile.model }]
      : H_MODELS;
  const busy = state.pending !== null;
  const hasOverrides = profile !== undefined && Object.keys(profile.platform_modes).length > 0;
  const edit = (patch: Partial<DesktopRavenExecutionProfile>) => dispatch({ type: "edit", patch });
  const run = (operation: Operation) => {
    if (
      !bridge ||
      busy ||
      (operation === "save" && !state.draft) ||
      (operation === "check" && state.draft)
    )
      return;
    dispatch({ type: "start", operation });
    const request =
      operation === "save" && state.draft
        ? bridge.saveExecution(state.draft)
        : operation === "check"
          ? bridge.checkExecution()
          : bridge.getExecution();
    void request.then(
      (value) => dispatch({ type: "loaded", operation, value }),
      (error: unknown) => dispatch({ type: "failed", message: executionErrorMessage(error) }),
    );
  };
  const refreshDevices = () => {
    if (!bridge || devicesLoading) return;
    setDevicesLoading(true);
    setDevices(null);
    void bridge.listDevices().then(
      (value) => {
        setDevices(value);
        setDeviceError(null);
        setDevicesLoading(false);
      },
      (error: unknown) => {
        setDeviceError(executionErrorMessage(error));
        setDevicesLoading(false);
      },
    );
  };
  const changeOpen = (next: boolean) => {
    setOpen(next);
    if (next && !state.draft) {
      run("load");
      refreshDevices();
    }
  };
  const changeOverride = (
    platform: Platform,
    mode: DesktopRavenExecutionProfile["h_mode"] | "inherit",
  ) => {
    if (!profile) return;
    const platform_modes = { ...profile.platform_modes };
    if (mode === "inherit") delete platform_modes[platform];
    else platform_modes[platform] = mode;
    edit({ platform_modes });
  };
  const reviewTargets =
    state.saved !== null &&
    PLATFORMS.some(
      ({ value }) =>
        state.saved?.targets.find((target) => target.platform === value)?.status !== "ready",
    );
  const summary = !bridge
    ? "Desktop only"
    : state.error || deviceError
      ? "Error"
      : busy
        ? state.pending === "save"
          ? "Saving"
          : state.pending === "check"
            ? "Checking"
            : "Loading"
        : state.draft
          ? "Unsaved"
          : reviewTargets
            ? "Review targets"
            : "Next run";

  return (
    <Collapsible open={open} onOpenChange={changeOpen}>
      <CollapsibleTrigger
        render={
          <Button variant="ghost" size="sm" className="w-full" aria-label="Raven execution" />
        }
      >
        <SlidersHorizontalIcon className="size-3.5" />
        <span className="flex-1 text-left">Execution</span>
        <Badge size="sm" variant={state.error || deviceError ? "error" : "outline"}>
          {summary}
        </Badge>
        <ChevronDownIcon className={open ? "size-3.5 rotate-180" : "size-3.5"} />
      </CollapsibleTrigger>
      <CollapsiblePanel animate={false}>
        <div className="max-h-[55vh] space-y-3 overflow-y-auto overscroll-contain px-2 py-3">
          <p className="text-xs text-muted-foreground">
            {desktopRaven ? "This desktop's Raven core" : "Raven core"} · defaults for the{" "}
            <strong>next run</strong>. Active runs keep their frozen profile.
          </p>
          {!bridge ? (
            <p className="text-xs text-muted-foreground">
              {desktopRaven
                ? "Update Raven Agent to configure execution here."
                : "Open Raven Agent desktop to configure execution here."}{" "}
              Defaults remain available to ACP and iOS through the core execution API.
            </p>
          ) : (
            <>
              {state.error ? (
                <p role="alert" className="break-words text-xs text-destructive-foreground">
                  {state.error}
                </p>
              ) : null}
              {state.notice ? (
                <p role="status" className="text-xs text-muted-foreground">
                  {state.notice}
                </p>
              ) : null}
              {!profile ? (
                <p className="text-xs text-muted-foreground">
                  {busy
                    ? "Loading defaults from Raven core…"
                    : "No execution profile loaded. Controls are unavailable until core responds."}
                </p>
              ) : (
                <>
                  <ExecutionSelect
                    label="Devin planner"
                    value={profile.planner}
                    items={[
                      { value: "local", label: "Local" },
                      { value: "cloud", label: "Cloud" },
                    ]}
                    disabled={busy}
                    onChange={(planner) => edit({ planner })}
                  />
                  <ExecutionSelect
                    label="H decision loop"
                    value={profile.h_mode}
                    items={H_MODES}
                    disabled={busy}
                    onChange={(h_mode) => edit({ h_mode })}
                  />
                  <ExecutionSelect
                    label="Holo model"
                    value={profile.model}
                    items={models}
                    disabled={busy}
                    onChange={(model) => edit({ model })}
                  />
                  <p className="text-2xs text-muted-foreground">
                    {H_MODE_DETAILS[profile.h_mode]} Devin plans; H makes computer-use decisions.
                    Cua/native drivers send input and capture screens. A Devin VM is a host, not an
                    H mode.
                  </p>
                  <ExecutionTarget platform="macos" state={state}>
                    <p className="text-xs">Mac native · this Mac</p>
                  </ExecutionTarget>
                  <ExecutionTarget platform="ios" state={state}>
                    <ExecutionSelect
                      label="iOS target"
                      value={profile.ios_target}
                      items={[
                        { value: "simulator", label: "DeviceHub simulator" },
                        { value: "device", label: "DeviceHub device" },
                      ]}
                      disabled={busy}
                      onChange={(ios_target) => edit({ ios_target })}
                    />
                    <ExecutionSelect
                      label="DeviceHub selection"
                      value={profile.ios_device_id ?? ""}
                      items={ravenExecutionDeviceChoices(profile, devices)}
                      disabled={busy || devicesLoading || devices === null}
                      onChange={(id) => edit({ ios_device_id: id || null })}
                    />
                    {deviceError ? (
                      <p role="alert" className="break-words text-xs text-destructive-foreground">
                        {deviceError}
                      </p>
                    ) : null}
                    {devices !== null &&
                    !devices.some(
                      (device) => device.kind === profile.ios_target && device.available,
                    ) ? (
                      <p className="text-2xs text-muted-foreground">
                        No available {profile.ios_target === "simulator" ? "simulators" : "devices"}{" "}
                        reported by core.
                      </p>
                    ) : null}
                    <div className="flex flex-wrap gap-1">
                      <Button
                        size="xs"
                        variant="ghost"
                        disabled={devicesLoading || busy}
                        onClick={refreshDevices}
                      >
                        {devicesLoading ? "Loading devices…" : "Refresh devices"}
                      </Button>
                      <Button size="xs" variant="ghost" render={<Link to="/settings/devices" />}>
                        DeviceHub
                      </Button>
                    </div>
                  </ExecutionTarget>
                  <ExecutionTarget platform="linux" state={state}>
                    <ExecutionSelect
                      label="Linux host"
                      value={profile.linux_target}
                      items={[
                        { value: "apple-container", label: "Apple Container Space" },
                        { value: "devin-vm", label: "Devin Linux VM" },
                      ]}
                      disabled={busy}
                      onChange={(linux_target) => edit({ linux_target })}
                    />
                    {profile.linux_target === "apple-container" ? (
                      <>
                        <div className="grid gap-1">
                          <label className="text-xs text-muted-foreground" htmlFor={spaceId}>
                            Space name
                          </label>
                          <Input
                            id={spaceId}
                            size="compact"
                            value={profile.linux_space}
                            placeholder="raven-linux-apple"
                            disabled={busy}
                            onChange={(event) => edit({ linux_space: event.target.value })}
                          />
                        </div>
                        <div className="flex items-center justify-between gap-2">
                          <label className="text-xs text-muted-foreground" htmlFor={fallbackId}>
                            Allow Devin Linux VM fallback
                          </label>
                          <Switch
                            id={fallbackId}
                            size="sm"
                            disabled={busy}
                            checked={profile.linux_fallback}
                            onCheckedChange={(linux_fallback) => edit({ linux_fallback })}
                          />
                        </div>
                      </>
                    ) : null}
                  </ExecutionTarget>
                  <ExecutionTarget platform="windows" state={state}>
                    <p className="text-xs">Devin Windows VM</p>
                  </ExecutionTarget>
                  <Collapsible open={showOverrides ?? hasOverrides} onOpenChange={setShowOverrides}>
                    <CollapsibleTrigger render={<Button size="xs" variant="ghost" />}>
                      Per-platform H overrides
                      {hasOverrides ? ` (${Object.keys(profile.platform_modes).length})` : ""}
                    </CollapsibleTrigger>
                    <CollapsiblePanel animate={false}>
                      <div className="grid gap-2 pt-2">
                        {PLATFORMS.map(({ value, label }) => (
                          <ExecutionSelect
                            key={value}
                            label={`${label} H mode`}
                            value={profile.platform_modes[value] ?? "inherit"}
                            items={[{ value: "inherit", label: "Use global H mode" }, ...H_MODES]}
                            disabled={busy}
                            onChange={(mode) => changeOverride(value, mode)}
                          />
                        ))}
                      </div>
                    </CollapsiblePanel>
                  </Collapsible>
                  <div className="grid gap-2">
                    <Button size="sm" disabled={busy || !state.draft} onClick={() => run("save")}>
                      {state.pending === "save" ? "Saving…" : "Save for next run"}
                    </Button>
                    <Button
                      size="xs"
                      variant="outline"
                      disabled={busy || Boolean(state.draft)}
                      onClick={() => run("check")}
                    >
                      {state.pending === "check" ? "Checking…" : "Check readiness"}
                    </Button>
                    <p className="text-2xs text-muted-foreground">
                      {state.draft ? "Save or discard changes before checking. " : ""}
                      Checks are read-only: no provisioning, device boot, or sidecar restart.
                    </p>
                  </div>
                </>
              )}
              <Button size="xs" variant="ghost" disabled={busy} onClick={() => run("load")}>
                {state.draft ? "Discard changes & reload" : "Reload from core"}
              </Button>
            </>
          )}
          <Button size="xs" variant="ghost" render={<Link to="/settings/raven" />}>
            Raven configuration
          </Button>
        </div>
      </CollapsiblePanel>
    </Collapsible>
  );
}
