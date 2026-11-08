import { describe, expect, it } from "@effect/vitest";
import * as Schema from "effect/Schema";

import { DesktopRavenExecutionProfileSchema, DesktopRavenExecutionStateSchema } from "./ipc.ts";

const decodeProfile = Schema.decodeUnknownSync(DesktopRavenExecutionProfileSchema);
const decodeState = Schema.decodeUnknownSync(DesktopRavenExecutionStateSchema);

const profile = {
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
} as const;

describe("Raven execution wire contract", () => {
  it("requires a complete core profile instead of inventing local defaults", () => {
    expect(decodeProfile(profile)).toEqual(profile);
    for (const field of Object.keys(profile)) {
      const incomplete = Object.fromEntries(
        Object.entries(profile).filter(([key]) => key !== field),
      );
      expect(() => decodeProfile(incomplete)).toThrow();
    }
    expect(() => decodeProfile({ ...profile, planner: "none" })).toThrow();
    expect(() => decodeProfile({ ...profile, h_mode: "cua" })).toThrow();
    expect(() => decodeProfile({ ...profile, platform_modes: { ios: "cua" } })).toThrow();
    expect(() => decodeProfile({ ...profile, windows_target: "remote-pc" })).toThrow();
    for (const model of ["", " with spaces", "model:tag", "/model", "a".repeat(129)]) {
      expect(() => decodeProfile({ ...profile, model })).toThrow();
    }
    expect(decodeProfile({ ...profile, model: "a".repeat(128) }).model).toBe("a".repeat(128));
    expect(() => decodeProfile({ ...profile, linux_space: "../space" })).toThrow();
    expect(() => decodeProfile({ ...profile, ios_device_id: "invalid/device" })).toThrow();
  });

  it("retains overrides, device identity, and core capability failures without interpreting readiness", () => {
    const state = {
      profile: {
        ...profile,
        model: "h/custom-holo4.v1",
        platform_modes: { macos: "models-loop", ios: "local-runtime", windows: "agents-api" },
        ios_target: "device",
        ios_device_id: "device-hub:iphone",
      },
      targets: [
        {
          platform: "ios",
          target: "device-hub:iphone",
          status: "capability-error",
          detail: "The selected H runtime does not support this physical device.",
        },
      ],
    };
    expect(decodeState(state)).toEqual(state);
  });
});
