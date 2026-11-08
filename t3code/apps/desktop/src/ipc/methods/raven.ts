import {
  DesktopRavenExecutionDeviceSchema,
  DesktopRavenExecutionProfileSchema,
  DesktopRavenExecutionStateSchema,
  DesktopRavenKeyNameSchema,
  DesktopRavenKeyTestResultSchema,
  DesktopRavenSettingsSaveInputSchema,
  DesktopRavenSettingsStateSchema,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import * as ElectronShell from "../../electron/ElectronShell.ts";
import * as RavenSettings from "../../raven/RavenSettings.ts";
import * as IpcChannels from "../channels.ts";
import { makeIpcMethod } from "../DesktopIpc.ts";

export const getRavenSettingsState = makeIpcMethod({
  channel: IpcChannels.RAVEN_GET_STATE_CHANNEL,
  payload: Schema.Void,
  result: DesktopRavenSettingsStateSchema,
  handler: Effect.fn("desktop.ipc.raven.getState")(function* () {
    return yield* (yield* RavenSettings.RavenSettings).get;
  }),
});

export const saveRavenSettings = makeIpcMethod({
  channel: IpcChannels.RAVEN_SAVE_CHANNEL,
  payload: DesktopRavenSettingsSaveInputSchema,
  result: DesktopRavenSettingsStateSchema,
  handler: Effect.fn("desktop.ipc.raven.save")(function* (input) {
    return yield* (yield* RavenSettings.RavenSettings).save(input);
  }),
});

const TestKeyInput = Schema.Struct({
  name: DesktopRavenKeyNameSchema,
  value: Schema.optionalKey(Schema.String),
});

export const testRavenKey = makeIpcMethod({
  channel: IpcChannels.RAVEN_TEST_KEY_CHANNEL,
  payload: TestKeyInput,
  result: DesktopRavenKeyTestResultSchema,
  handler: Effect.fn("desktop.ipc.raven.testKey")(function* ({ name, value }) {
    return yield* (yield* RavenSettings.RavenSettings).testKey(name, value);
  }),
});

export const restartRavenCore = makeIpcMethod({
  channel: IpcChannels.RAVEN_RESTART_CHANNEL,
  payload: Schema.Void,
  result: DesktopRavenSettingsStateSchema,
  handler: Effect.fn("desktop.ipc.raven.restart")(function* () {
    return yield* (yield* RavenSettings.RavenSettings).restart;
  }),
});

export const openRavenCoreLog = makeIpcMethod({
  channel: IpcChannels.RAVEN_OPEN_LOG_CHANNEL,
  payload: Schema.Void,
  result: Schema.Boolean,
  handler: Effect.fn("desktop.ipc.raven.openLog")(function* () {
    const state = yield* (yield* RavenSettings.RavenSettings).get;
    return yield* ElectronShell.openPath(state.status.logPath);
  }),
});

export const getRavenExecution = makeIpcMethod({
  channel: IpcChannels.RAVEN_GET_EXECUTION_CHANNEL,
  payload: Schema.Void,
  result: DesktopRavenExecutionStateSchema,
  handler: Effect.fn("desktop.ipc.raven.getExecution")(function* () {
    return yield* (yield* RavenSettings.RavenSettings).getExecution;
  }),
});

export const saveRavenExecution = makeIpcMethod({
  channel: IpcChannels.RAVEN_SAVE_EXECUTION_CHANNEL,
  payload: DesktopRavenExecutionProfileSchema,
  result: DesktopRavenExecutionStateSchema,
  handler: Effect.fn("desktop.ipc.raven.saveExecution")(function* (profile) {
    return yield* (yield* RavenSettings.RavenSettings).saveExecution(profile);
  }),
});

export const checkRavenExecution = makeIpcMethod({
  channel: IpcChannels.RAVEN_CHECK_EXECUTION_CHANNEL,
  payload: Schema.Void,
  result: DesktopRavenExecutionStateSchema,
  handler: Effect.fn("desktop.ipc.raven.checkExecution")(function* () {
    return yield* (yield* RavenSettings.RavenSettings).checkExecution;
  }),
});

export const listRavenDevices = makeIpcMethod({
  channel: IpcChannels.RAVEN_LIST_DEVICES_CHANNEL,
  payload: Schema.Void,
  result: Schema.Array(DesktopRavenExecutionDeviceSchema),
  handler: Effect.fn("desktop.ipc.raven.listDevices")(function* () {
    return yield* (yield* RavenSettings.RavenSettings).listDevices;
  }),
});
