import type { DesktopRavenExecutionProfile, DesktopRavenExecutionState } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  initialExecutionPanelState,
  ravenExecutionDeviceChoices,
  ravenExecutionReadiness,
  ravenExecutionReducer,
} from "./RavenExecutionPanel";

const profile: DesktopRavenExecutionProfile = {
  planner: "local",
  model: "h/custom-holo4.v1",
  h_mode: "auto",
  platform_modes: { macos: "models-loop" },
  linux_target: "apple-container",
  linux_space: "raven-linux-apple",
  linux_fallback: true,
  windows_target: "devin-vm",
  ios_target: "simulator",
  ios_device_id: "sim-1",
};

const core: DesktopRavenExecutionState = {
  profile,
  targets: [
    { platform: "macos", target: "native", status: "ready", detail: "Permissions granted." },
    { platform: "ios", target: "sim-1", status: "ready", detail: "Simulator is available." },
    {
      platform: "windows",
      target: "devin-vm",
      status: "capability-error",
      detail: "This account cannot provision a Windows VM.",
    },
  ],
};

const loaded = () =>
  ravenExecutionReducer(initialExecutionPanelState, {
    type: "loaded",
    operation: "load",
    value: core,
  });

describe("Raven execution panel state", () => {
  it("never invents defaults or reuses saved readiness for an unsaved profile", () => {
    expect(initialExecutionPanelState.saved).toBeNull();
    expect(ravenExecutionReadiness(initialExecutionPanelState, "macos").status).not.toBe("ready");
    const state = loaded();
    expect(ravenExecutionReadiness(state, "macos").status).toBe("ready");

    const edited = ravenExecutionReducer(state, {
      type: "edit",
      patch: { planner: "cloud", h_mode: "agents-api", linux_fallback: false },
    });
    expect(edited.saved).toBe(core);
    expect(edited.draft).toEqual({
      ...profile,
      planner: "cloud",
      h_mode: "agents-api",
      linux_fallback: false,
    });
    expect(edited.draft?.model).toBe("h/custom-holo4.v1");
    expect(ravenExecutionReadiness(edited, "macos")).toMatchObject({
      status: "Not checked",
      target: null,
    });
    expect(edited.notice).toBeNull();

    const saving = ravenExecutionReducer(edited, { type: "start", operation: "save" });
    expect(ravenExecutionReducer(saving, { type: "edit", patch: { planner: "local" } })).toBe(
      saving,
    );
    expect(saving.saved?.profile).toBe(profile);
    expect(saving.notice).toBeNull();
    expect(ravenExecutionReadiness(saving, "macos").status).not.toBe("ready");

    const canonical: DesktopRavenExecutionState = {
      profile: { ...profile, planner: "cloud", linux_fallback: false },
      targets: [
        {
          platform: "macos",
          target: "native",
          status: "unavailable",
          detail: "Devin cloud credentials are missing.",
        },
      ],
    };
    const saved = ravenExecutionReducer(saving, {
      type: "loaded",
      operation: "save",
      value: canonical,
    });
    expect(saved.saved).toBe(canonical);
    expect(saved.draft).toBeNull();
    expect(saved.notice).not.toBeNull();
    expect(ravenExecutionReadiness(saved, "macos").detail).toBe(
      "Devin cloud credentials are missing.",
    );
    expect(state.saved).toBe(core);
  });

  it("keeps rejected drafts and errors visible until core confirms a reload or save", () => {
    const edited = ravenExecutionReducer(loaded(), {
      type: "edit",
      patch: { platform_modes: { ...profile.platform_modes, windows: "agents-api" } },
    });
    const saving = ravenExecutionReducer(edited, { type: "start", operation: "save" });
    const failed = ravenExecutionReducer(saving, {
      type: "failed",
      message: "The selected H mode is unavailable for this Windows VM.",
    });
    expect(failed.draft).toEqual({
      ...profile,
      platform_modes: { ...profile.platform_modes, windows: "agents-api" },
    });
    expect(failed.error).toBe("The selected H mode is unavailable for this Windows VM.");
    expect(failed.notice).toBeNull();
    expect(failed.pending).toBeNull();
    expect(ravenExecutionReadiness(failed, "macos").status).not.toBe("ready");

    const retrying = ravenExecutionReducer(failed, { type: "start", operation: "load" });
    expect(retrying.error).toBe(failed.error);
    expect(
      ravenExecutionReducer(retrying, { type: "loaded", operation: "save", value: core }),
    ).toBe(retrying);
    const reloaded = ravenExecutionReducer(retrying, {
      type: "loaded",
      operation: "load",
      value: core,
    });
    expect(reloaded.draft).toBeNull();
    expect(reloaded.error).toBeNull();
    expect(reloaded.saved?.profile.windows_target).toBe("devin-vm");
    expect(reloaded.saved?.profile.platform_modes).toEqual(profile.platform_modes);
  });

  it("does not convert capability failures, missing targets, or failed checks into readiness", () => {
    const state = loaded();
    expect(ravenExecutionReadiness(state, "windows")).toMatchObject({
      status: "capability-error",
      detail: "This account cannot provision a Windows VM.",
    });
    expect(ravenExecutionReadiness(state, "linux").status).toBe("Not checked");
    const checking = ravenExecutionReducer(state, { type: "start", operation: "check" });
    expect(ravenExecutionReadiness(checking, "macos").status).not.toBe("ready");
    const failed = ravenExecutionReducer(checking, {
      type: "failed",
      message: "Raven core is unavailable.",
    });
    expect(failed.saved).toBe(core);
    expect(ravenExecutionReadiness(failed, "macos").status).toBe("Unknown");
    expect(failed.error).toBe("Raven core is unavailable.");
  });

  it("requires an explicit DeviceHub selection and clears identity when switching target kind", () => {
    const state = loaded();
    const switched = ravenExecutionReducer(state, {
      type: "edit",
      patch: { ios_target: "device" },
    });
    expect(switched.draft).toEqual({ ...profile, ios_target: "device", ios_device_id: null });
    expect(state.saved?.profile.ios_device_id).toBe("sim-1");

    const choices = ravenExecutionDeviceChoices(profile, [
      { id: "other-sim", name: "iPad", kind: "simulator", available: true },
      { id: "offline", name: "iPhone offline", kind: "simulator", available: false },
      { id: "phone", name: "Physical iPhone", kind: "device", available: true },
    ]);
    expect(choices.find((choice) => choice.value === "sim-1")?.disabled).toBe(true);
    expect(choices.find((choice) => choice.value === "offline")?.disabled).toBe(true);
    expect(choices.find((choice) => choice.value === "other-sim")?.disabled).toBe(false);
    expect(choices.some((choice) => choice.value === "phone")).toBe(false);
    expect(
      ravenExecutionDeviceChoices(profile, null).find((choice) => choice.value === "sim-1"),
    ).toMatchObject({ disabled: true });
    expect(profile.ios_device_id).toBe("sim-1");
  });
});
