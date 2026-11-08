from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven_core.events import ExecutionProfile
from raven_core.workers import device_hub
from raven_core.workers.base import WorkerUnavailable

DEVELOPER = "/Applications/Selected Xcode.app/Contents/Developer"
APP = "/Applications/Selected Xcode.app/Contents/Applications/DeviceHub.app"
SIM_A = "AAAAAAAA-1111-2222-3333-444444444444"
SIM_B = "BBBBBBBB-1111-2222-3333-444444444444"
SIM_C = "CCCCCCCC-1111-2222-3333-444444444444"
CORE_A = "DDDDDDDD-1111-2222-3333-444444444444"
CORE_B = "EEEEEEEE-1111-2222-3333-444444444444"
PHONE_A = "00008110-001C108C0132401E"
PHONE_B = "00008110-001C108C0132401F"
IOS_RUNTIME = "com.apple.CoreSimulator.SimRuntime.iOS-27-0"


def simulator(identifier=SIM_A, name="iPhone Simulator", available=True, state="Shutdown"):
    return {"udid": identifier, "name": name, "isAvailable": available, "state": state}


def core_device(identifier=PHONE_A, core_id=CORE_A, name="Paired iPhone", reality="physical"):
    return {
        "identifier": core_id,
        "hardwareProperties": {
            "udid": identifier, "platform": "iOS", "deviceType": "iPhone", "reality": reality,
        },
        "deviceProperties": {
            "name": name, "bootState": "booted", "developerModeStatus": "enabled",
            "screenViewingURL": f"devices://device/open?id={core_id}",
        },
        "connectionProperties": {
            "pairingState": "paired", "tunnelState": "connected",
            "transportType": "sameMachine" if reality == "simulated" else "wired",
        },
    }


def core_simulator(identifier=SIM_A, name="iPhone Simulator"):
    device = core_device(identifier, CORE_B, name, "simulated")
    device["deviceProperties"]["screenViewingURL"] = f"devices://device/open?id={identifier}"
    return device


def modern(device):
    device = copy.deepcopy(device)
    device["properties"] = {
        "hardware": device.pop("hardwareProperties"),
        "connection": device.pop("connectionProperties"),
        "state": device.pop("deviceProperties"),
    }
    device["properties"]["connection"]["state"] = "available"
    return device


def envelope(result):
    return {"info": {"outcome": "success", "jsonVersion": 5}, "result": result}


class Process:
    def __init__(self, output=b"", code=0, stderr=b"", blocked=False):
        self.output = output if isinstance(output, bytes) else json.dumps(output).encode()
        self.stderr = stderr
        self.code = code
        self.returncode = None
        self.blocked = blocked
        self.communicating = asyncio.Event()
        self.release = asyncio.Event()
        self.reaping = asyncio.Event()
        self.reap_release = None
        self.kills = 0
        self.waits = 0

    async def communicate(self):
        self.communicating.set()
        if self.blocked:
            await self.release.wait()
        self.returncode = self.code
        return self.output, self.stderr

    def kill(self):
        self.kills += 1
        self.returncode = -9

    async def wait(self):
        self.waits += 1
        self.reaping.set()
        if self.reap_release is not None:
            await self.reap_release.wait()
        return self.returncode


class Commands:
    def __init__(self):
        self.simulators = [simulator()]
        self.groups = {}
        self.devices = []
        self.details = core_simulator()
        self.lock = {"deviceIdentifier": CORE_A, "passcodeRequired": False, "unlockedSinceBoot": True}
        self.developer = DEVELOPER
        self.app_exists = True
        self.calls = []
        self.processes = []
        self.overrides = {}
        self.spawn_stage = None
        self.spawning = asyncio.Event()
        self.spawn_release = asyncio.Event()

    @staticmethod
    def stage(argv):
        if argv == ("/usr/bin/xcode-select", "--print-path"):
            return "xcode"
        if argv[:3] == ("/usr/bin/xcrun", "simctl", "list"):
            return "sim-list"
        if argv[:4] == ("/usr/bin/xcrun", "devicectl", "list", "devices"):
            return "core-list"
        if argv[:3] == ("/usr/bin/xcrun", "simctl", "bootstatus"):
            return "boot"
        if argv[:5] == ("/usr/bin/xcrun", "devicectl", "device", "info", "details"):
            return "details"
        if argv[:5] == ("/usr/bin/xcrun", "devicectl", "device", "info", "lockState"):
            return "lock"
        if argv[:2] == ("/usr/bin/open", "-a"):
            return "open"
        raise AssertionError(f"Unexpected subprocess: {argv}")

    async def spawn(self, *argv, **kwargs):
        stage = self.stage(argv)
        self.calls.append((stage, argv, kwargs))
        if stage == self.spawn_stage:
            self.spawning.set()
            await self.spawn_release.wait()
        override = self.overrides.get(stage)
        if isinstance(override, BaseException):
            raise override
        if isinstance(override, Process):
            process = override
        else:
            output = {
                "xcode": (self.developer + "\n").encode(),
                "sim-list": {"devices": {IOS_RUNTIME: self.simulators, **self.groups}},
                "core-list": envelope({"devices": self.devices}),
                "boot": b"",
                "details": envelope(self.details),
                "lock": envelope(self.lock),
                "open": b"",
            }[stage]
            process = Process(output)
        self.processes.append(process)
        return process

    def args(self, stage):
        return [argv for name, argv, _ in self.calls if name == stage]

    def only_inventory(self):
        assert all(stage in {"xcode", "sim-list", "core-list"} for stage, _, _ in self.calls)

    def physical(self):
        self.devices = [core_device()]
        self.details = core_device()
        return ExecutionProfile(ios_target="device", ios_device_id=PHONE_A)


@pytest.fixture(autouse=True)
def commands(monkeypatch):
    commands = Commands()

    class MockPath(type(Path())):
        def is_dir(self):
            return commands.app_exists and str(self) == APP

    monkeypatch.setattr(device_hub, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(device_hub, "Path", MockPath)
    monkeypatch.setattr(device_hub.asyncio, "create_subprocess_exec", commands.spawn)
    return commands


def test_list_devices_is_read_only_sorted_ios_only_and_has_no_raw_metadata(commands):
    commands.simulators = [
        simulator(SIM_B, "Zeta", state="Booted"),
        simulator(SIM_A, "Alpha"),
        simulator(SIM_C, "Missing Runtime", available=False),
    ]
    commands.groups = {
        "com.apple.CoreSimulator.SimRuntime.tvOS-27-0": [simulator(CORE_A, "Apple TV")],
        "com.apple.CoreSimulator.SimRuntime.watchOS-27-0": [simulator(CORE_B, "Apple Watch")],
    }
    phone = core_device()
    phone["deviceProperties"]["privateData"] = "not-public"
    watch = core_device(PHONE_B, CORE_B)
    watch["hardwareProperties"]["platform"] = "watchOS"
    commands.devices = [phone, core_simulator(SIM_B), watch]

    result = asyncio.run(device_hub.list_devices())

    assert result == [
        {"id": SIM_A, "name": "Alpha", "kind": "simulator", "available": True},
        {"id": SIM_C, "name": "Missing Runtime", "kind": "simulator", "available": False},
        {"id": SIM_B, "name": "Zeta", "kind": "simulator", "available": True},
        {"id": PHONE_A, "name": "Paired iPhone", "kind": "device", "available": True},
    ]
    commands.only_inventory()
    assert len(commands.calls) == 3
    assert commands.args("sim-list") == [("/usr/bin/xcrun", "simctl", "list", "devices", "--json")]
    assert commands.args("core-list") == [
        ("/usr/bin/xcrun", "devicectl", "list", "devices", "--timeout", "25", "--json-output", "-"),
    ]


def test_modern_properties_and_idle_available_connection(commands):
    phone = modern(core_device())
    phone["properties"]["connection"]["tunnelState"] = "disconnected"
    commands.devices = [phone]
    commands.details = phone
    execution = ExecutionProfile(ios_target="device", ios_device_id=PHONE_A)

    result = asyncio.run(device_hub.prepare_device(execution))

    assert result == {"id": PHONE_A, "name": "Paired iPhone", "kind": "device", "available": True}
    assert commands.args("boot") == []
    assert commands.args("open") == [
        ("/usr/bin/open", "-a", APP, "-u", f"devices://device/open?id={CORE_A}"),
    ]


def test_missing_reality_uses_physical_udid_and_transport_not_name(commands):
    phone = core_device()
    phone["hardwareProperties"].pop("reality")
    sim = core_simulator()
    sim["hardwareProperties"].pop("reality")
    commands.devices = [phone, sim]
    result = asyncio.run(device_hub.list_devices())
    assert [(entry["id"], entry["kind"]) for entry in result] == [(SIM_A, "simulator"), (PHONE_A, "device")]


@pytest.mark.parametrize("host", ["linux", "win32"])
@pytest.mark.parametrize("api", ["list", "prepare"])
def test_native_mac_required_before_any_subprocess(commands, monkeypatch, host, api):
    monkeypatch.setattr(device_hub, "sys", SimpleNamespace(platform=host))
    with pytest.raises(WorkerUnavailable, match="requires macOS"):
        asyncio.run(device_hub.list_devices() if api == "list" else device_hub.prepare_device(ExecutionProfile()))
    assert commands.calls == []


@pytest.mark.parametrize("developer,exists", [
    ("/Library/Developer/CommandLineTools", True),
    (DEVELOPER, False),
    ("relative/Developer", True),
])
def test_selected_xcode_must_include_device_hub(commands, developer, exists):
    commands.developer = developer
    commands.app_exists = exists
    with pytest.raises(WorkerUnavailable, match="DEVELOPER_DIR"):
        asyncio.run(device_hub.list_devices())
    assert [stage for stage, _, _ in commands.calls] == ["xcode"]


def test_explicit_simulator_beats_other_booted_target_and_uses_exact_udid(commands):
    commands.simulators = [simulator(SIM_A, state="Booted"), simulator(SIM_B, "Requested")]
    commands.details = core_simulator(SIM_B, "Requested")

    result = asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator", ios_device_id=SIM_B.lower())))

    assert result == {"id": SIM_B, "name": "Requested", "kind": "simulator", "available": True}
    assert commands.args("boot") == [("/usr/bin/xcrun", "simctl", "bootstatus", SIM_B, "-b")]
    assert commands.args("details")[0][5:7] == ("--device", SIM_B)
    assert commands.args("open") == [
        ("/usr/bin/open", "-a", APP, "-u", f"devices://device/open?id={SIM_B}"),
    ]
    assert commands.args("lock") == []


def test_default_uses_any_available_iphone_not_a_simulator(commands):
    ipad = core_device(PHONE_B, CORE_B, "Paired iPad")
    ipad["hardwareProperties"]["deviceType"] = "iPad"
    commands.devices = [ipad, core_device()]
    commands.details = core_device()
    result = asyncio.run(device_hub.prepare_device(ExecutionProfile()))
    assert result == {"id": PHONE_A, "name": "Paired iPhone", "kind": "device", "available": True}
    assert commands.args("boot") == []


def test_default_without_an_available_iphone_never_falls_back_to_a_simulator(commands):
    with pytest.raises(WorkerUnavailable, match="No physical iPhone is available"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile()))
    commands.only_inventory()


def test_selected_simulator_target_uses_the_first_available_simulator(commands):
    commands.simulators.append(simulator(SIM_B, state="Booted"))
    commands.devices = [core_device()]
    result = asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    assert result["id"] == SIM_A
    assert commands.args("boot")[0][3] == SIM_A


@pytest.mark.parametrize("simulators", [[], [simulator(available=False)]])
def test_selected_simulator_target_never_falls_back_to_a_physical_device(commands, simulators):
    commands.simulators = simulators
    commands.devices = [core_device()]
    with pytest.raises(WorkerUnavailable, match="No available iOS simulator"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    commands.only_inventory()


@pytest.mark.parametrize("identifier", ["booted", "all", "iPhone", SIM_A[:8], SIM_B])
def test_unknown_id_name_prefix_and_booted_alias_never_fall_back(commands, identifier):
    commands.simulators[0]["name"] = "iPhone"
    with pytest.raises(WorkerUnavailable, match="not found uniquely"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator", ios_device_id=identifier)))
    commands.only_inventory()


@pytest.mark.parametrize("kind,identifier", [("device", SIM_A), ("simulator", PHONE_A), ("simulator", CORE_A)])
def test_requested_kind_must_match_id(commands, kind, identifier):
    commands.devices = [core_device()]
    with pytest.raises(WorkerUnavailable, match="not ios_target"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target=kind, ios_device_id=identifier)))
    commands.only_inventory()


@pytest.mark.parametrize("identifier", [PHONE_A, PHONE_A.lower(), CORE_A, CORE_A.lower()])
def test_physical_id_or_coredevice_alias_is_resolved_to_exact_udid(commands, identifier):
    commands.physical()
    commands.devices.append(core_device(PHONE_B, CORE_B, "Other phone"))
    result = asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="device", ios_device_id=identifier)))
    assert result["id"] == PHONE_A
    assert commands.args("details")[0][5:7] == ("--device", PHONE_A)
    assert commands.args("lock")[0][5:7] == ("--device", PHONE_A)
    assert commands.args("boot") == []
    assert commands.args("open")[0][-1] == f"devices://device/open?id={CORE_A}"


def test_reported_simulator_coredevice_alias_resolves_without_duplicate_inventory(commands):
    commands.devices = [core_simulator()]
    result = asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator", ios_device_id=CORE_B)))
    assert result["id"] == SIM_A
    assert commands.args("boot")[0][3] == SIM_A
    assert len(asyncio.run(device_hub.list_devices())) == 1


def test_simulator_coredevice_alias_cannot_be_requested_as_physical_device(commands):
    commands.devices = [core_simulator()]
    with pytest.raises(WorkerUnavailable, match="not ios_target=device"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="device", ios_device_id=CORE_B)))
    commands.only_inventory()


@pytest.mark.parametrize("change,message", [
    ({"isAvailable": False}, "unavailable"),
    ({"isAvailable": "true"}, "unavailable"),
    ({"state": "Booting"}, "not ready"),
    ({"state": "Shutting Down"}, "not ready"),
])
def test_unavailable_simulator_is_never_booted(commands, change, message):
    commands.simulators[0].update(change)
    with pytest.raises(WorkerUnavailable, match=message):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator", ios_device_id=SIM_A)))
    commands.only_inventory()


@pytest.mark.parametrize("group,key,value,message", [
    ("connectionProperties", "pairingState", "unpaired", "not paired/authorized"),
    ("connectionProperties", "pairingState", None, "not paired/authorized"),
    ("connectionProperties", "tunnelState", "unavailable", "unavailable"),
    ("connectionProperties", "tunnelState", "disconnected", "unavailable"),
    ("connectionProperties", "state", "unavailable", "unavailable"),
    ("deviceProperties", "developerModeStatus", "disabled", "Developer Mode"),
    ("deviceProperties", "developerModeStatus", None, "Developer Mode"),
    ("deviceProperties", "bootState", "shutdown", "not booted"),
])
def test_physical_availability_and_authorization_fail_closed(commands, group, key, value, message):
    execution = commands.physical()
    commands.devices[0][group][key] = value
    result = asyncio.run(device_hub.list_devices())
    assert result[-1]["available"] is False
    with pytest.raises(WorkerUnavailable, match=message):
        asyncio.run(device_hub.prepare_device(execution))
    commands.only_inventory()


@pytest.mark.parametrize("target", ["simulator", "device"])
def test_duplicate_ids_fail_instead_of_choosing_first(commands, target):
    if target == "simulator":
        commands.simulators.append(simulator())
    else:
        commands.devices = [core_device(), core_device(core_id=CORE_B)]
    with pytest.raises(WorkerUnavailable, match="duplicate/ambiguous"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    commands.only_inventory()


def test_physical_and_simulated_coredevice_alias_collision_fails(commands):
    commands.devices = [core_device(core_id=CORE_B), core_simulator()]
    with pytest.raises(WorkerUnavailable, match="duplicate/ambiguous"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    commands.only_inventory()


def test_fresh_simulator_details_cannot_reuse_another_devices_alias(commands):
    commands.devices = [core_device(core_id=CORE_B)]
    with pytest.raises(WorkerUnavailable, match="identity/type changed"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    assert commands.args("open") == []


def test_coredevice_identifier_must_not_be_a_name(commands):
    commands.devices = [core_device(core_id="named-iPhone")]
    with pytest.raises(WorkerUnavailable, match="non-UUID CoreDevice identifier"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    commands.only_inventory()


def test_conflicting_kind_is_not_silently_classified_as_physical(commands):
    phone = core_device()
    phone["connectionProperties"]["transportType"] = "sameMachine"
    commands.devices = [phone]
    with pytest.raises(WorkerUnavailable, match="conflicting physical/simulator"):
        asyncio.run(device_hub.list_devices())
    commands.only_inventory()


def test_unknown_kind_is_not_inferred_from_device_name(commands):
    phone = core_device(CORE_A)
    phone["hardwareProperties"].pop("reality")
    commands.devices = [phone]
    with pytest.raises(WorkerUnavailable, match="Cannot verify whether"):
        asyncio.run(device_hub.list_devices())


@pytest.mark.parametrize("details,message", [
    (core_simulator(SIM_B), "identity/type changed"),
    (core_device(SIM_A), "conflicting physical/simulator"),
    (core_device(), "identity/type changed"),
    ({"hardwareProperties": {"platform": "tvOS"}}, "identity/type changed"),
])
def test_simulator_details_must_still_identify_requested_ios_simulator(commands, details, message):
    commands.details = details
    with pytest.raises(WorkerUnavailable, match=message):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator", ios_device_id=SIM_A)))
    assert commands.args("open") == []
    assert commands.args("lock") == []


def test_coredevice_alias_cannot_hide_changed_hardware_id(commands):
    commands.details = core_simulator(SIM_B)
    commands.details["identifier"] = SIM_A
    with pytest.raises(WorkerUnavailable, match="identity/type changed"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    assert commands.args("open") == []


def test_physical_device_is_revalidated_before_open(commands):
    execution = commands.physical()
    commands.details["connectionProperties"]["pairingState"] = "unpaired"
    with pytest.raises(WorkerUnavailable, match="not paired/authorized"):
        asyncio.run(device_hub.prepare_device(execution))
    assert commands.args("open") == []
    assert commands.args("lock") == []


def test_physical_details_cannot_switch_to_other_phone_with_same_core_alias(commands):
    execution = commands.physical()
    commands.details = core_device(PHONE_B, CORE_A)
    with pytest.raises(WorkerUnavailable, match="identity/type changed"):
        asyncio.run(device_hub.prepare_device(execution))
    assert commands.args("open") == []


def test_wrapped_device_details_supported(commands):
    commands.details = {"device": commands.details}
    assert asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))["id"] == SIM_A


def test_modern_properties_take_precedence_over_deprecated_availability(commands):
    commands.devices = [core_device()]
    commands.devices[0]["properties"] = {"connection": {"pairingState": "unpaired"}}
    result = asyncio.run(device_hub.list_devices())
    assert result[-1]["available"] is False


def test_conflicting_modern_screen_urls_do_not_open_any_target(commands):
    commands.details["properties"] = {
        "state": {"screenViewingURL": f"devices://device/open?id={SIM_A}"},
        "other": {"screenViewingURL": f"devices://device/open?id={SIM_B}"},
    }
    with pytest.raises(WorkerUnavailable, match="conflicting screenViewingURL"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    assert commands.args("open") == []


def test_started_simulator_must_be_reported_booted_before_open(commands):
    commands.details["deviceProperties"]["bootState"] = "shutdown"
    with pytest.raises(WorkerUnavailable, match="not booted"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    assert commands.args("open") == []


@pytest.mark.parametrize("url", [
    None,
    "",
    f"https://example.test/{SIM_A}",
    f"devices://device/open?id={SIM_B}",
    f"devices://device/open?id={SIM_A}&id={SIM_B}",
    f"devices://device/open?id={SIM_A}&token=do-not-log",
    f"devices://device/open?id={SIM_A}#other-device",
    f"devices://user@device/open?id={SIM_A}",
    f"devices://device/other?id={SIM_A}",
    f"devices://device/open?id={SIM_A}\n",
    "devices://device/open?id",
])
def test_missing_wrong_or_unsafe_screen_url_never_opens_generic_hub(commands, url):
    commands.details["deviceProperties"]["screenViewingURL"] = url
    with pytest.raises(WorkerUnavailable, match="screenViewingURL") as error:
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    assert "do-not-log" not in str(error.value)
    assert commands.args("open") == []


@pytest.mark.parametrize("change,message", [
    ({"deviceIdentifier": CORE_B}, "different target"),
    ({"passcodeRequired": True}, "locked"),
    ({"passcodeRequired": None}, "authorization cannot be verified"),
    ({"passcodeRequired": "false"}, "authorization cannot be verified"),
    ({"unlockedSinceBoot": False}, "authorization cannot be verified"),
    ({"unlockedSinceBoot": None}, "authorization cannot be verified"),
])
def test_physical_device_lock_probe_is_pinned_and_requires_authorization(commands, change, message):
    execution = commands.physical()
    commands.lock.update(change)
    with pytest.raises(WorkerUnavailable, match=message):
        asyncio.run(device_hub.prepare_device(execution))
    assert commands.args("open") == []


@pytest.mark.parametrize("stage,payload,message", [
    ("sim-list", b"not-json-secret", "invalid JSON"),
    ("sim-list", [], "unsupported JSON"),
    ("sim-list", {}, "no device inventory"),
    ("sim-list", {"devices": {IOS_RUNTIME: {}}}, "unsupported iOS inventory"),
    ("sim-list", {"devices": {IOS_RUNTIME: [None]}}, "invalid device record"),
    ("core-list", b"not-json-secret", "invalid JSON"),
    ("core-list", {"result": {"devices": []}}, "could not verify"),
    ("core-list", {"info": {"outcome": "failed"}, "result": {"devices": []}}, "could not verify"),
    ("core-list", {"info": {"outcome": "success"}, "result": []}, "no structured result"),
    ("core-list", envelope({}), "no device inventory"),
    ("core-list", envelope({"devices": [None]}), "invalid device record"),
])
def test_malformed_inventory_never_becomes_empty_success(commands, stage, payload, message):
    commands.overrides[stage] = Process(payload)
    with pytest.raises(WorkerUnavailable, match=message) as error:
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    assert "not-json-secret" not in str(error.value)
    commands.only_inventory()


@pytest.mark.parametrize("identifier", ["booted", "all", "-help", "a;open", "name-not-a-uuid"])
def test_bad_simulator_identifiers_never_reach_boot(commands, identifier):
    commands.simulators[0]["udid"] = identifier
    with pytest.raises(WorkerUnavailable, match="identifier"):
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    commands.only_inventory()


@pytest.mark.parametrize("stage", ["xcode", "sim-list", "core-list", "boot", "details", "open"])
def test_command_errors_do_not_leak_output_or_fall_back(commands, stage):
    commands.overrides[stage] = Process(b"secret stdout", code=1, stderr=b"secret stderr")
    with pytest.raises(WorkerUnavailable, match="command failed") as error:
        asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    assert "secret" not in str(error.value)
    assert commands.calls[-1][0] == stage
    assert all(process.kills == 0 for process in commands.processes)


def test_device_lock_probe_failure_does_not_open_device(commands):
    execution = commands.physical()
    commands.overrides["lock"] = Process(b"", code=1)
    with pytest.raises(WorkerUnavailable, match="pair, trust, unlock"):
        asyncio.run(device_hub.prepare_device(execution))
    assert commands.args("open") == []


def test_missing_executable_has_actionable_error_without_raw_os_error(commands):
    commands.overrides["xcode"] = FileNotFoundError("private OS detail")
    with pytest.raises(WorkerUnavailable, match="Xcode 27") as error:
        asyncio.run(device_hub.list_devices())
    assert "private OS detail" not in str(error.value)


def test_environment_does_not_forward_secrets_and_pins_selected_xcode(commands, monkeypatch):
    monkeypatch.setenv("RAVEN_TOKEN", "private-token")
    monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    monkeypatch.setenv("DEVICECTL_CHILD_PASSWORD", "private-password")
    monkeypatch.setenv("DEVELOPER_DIR", "/Applications/Other Xcode.app/Contents/Developer")
    asyncio.run(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
    for stage, _, options in commands.calls:
        assert options["stdin"] == asyncio.subprocess.DEVNULL
        assert options["stdout"] == options["stderr"] == asyncio.subprocess.PIPE
        assert options["env"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
        assert not {"RAVEN_TOKEN", "OPENAI_API_KEY", "DEVICECTL_CHILD_PASSWORD"} & options["env"].keys()
        if stage != "xcode":
            assert options["env"]["DEVELOPER_DIR"] == DEVELOPER


def test_devicectl_envelope_metadata_is_not_returned(commands):
    payload = envelope({"devices": [core_device()]})
    payload["info"]["environment"] = {"TOKEN": "private-token"}
    commands.overrides["core-list"] = Process(payload)
    result = asyncio.run(device_hub.list_devices())
    assert "private-token" not in json.dumps(result)


def test_structured_error_never_exposes_error_payload(commands):
    payload = envelope({"devices": [core_device()]})
    payload["error"] = {"description": "private device failure", "environment": {"KEY": "private-key"}}
    commands.overrides["core-list"] = Process(payload)
    with pytest.raises(WorkerUnavailable, match="could not verify") as error:
        asyncio.run(device_hub.list_devices())
    assert "private" not in str(error.value)


def test_empty_inventory_is_returned_without_launching_anything(commands):
    commands.simulators = []
    assert asyncio.run(device_hub.list_devices()) == []
    commands.only_inventory()


def test_ipados_simulators_and_physical_devices_are_included(commands):
    commands.simulators = []
    commands.groups = {"com.apple.CoreSimulator.SimRuntime.iPadOS-27-0": [simulator(SIM_B, "iPad Simulator")]}
    ipad = core_device(name="iPad")
    ipad["hardwareProperties"].update(platform="iPadOS", deviceType="iPad")
    commands.devices = [ipad]
    result = asyncio.run(device_hub.list_devices())
    assert [(device["id"], device["name"]) for device in result] == [(SIM_B, "iPad Simulator"), (PHONE_A, "iPad")]


def test_timeout_kills_and_reaps_only_spawned_process(commands):
    blocked = Process(blocked=True)
    unrelated = Process(blocked=True)
    commands.overrides["boot"] = blocked

    async def scenario():
        with pytest.raises(WorkerUnavailable, match="timed out"):
            await asyncio.wait_for(device_hub._run(
                "/usr/bin/xcrun", "simctl", "bootstatus", SIM_A, "-b", timeout=0.01,
            ), 1)

    asyncio.run(scenario())
    assert blocked.kills == blocked.waits == 1
    assert unrelated.kills == unrelated.waits == 0


@pytest.mark.parametrize("stage", ["xcode", "sim-list", "core-list", "boot", "details", "open", "lock"])
def test_preparation_cancellation_kills_owned_command_and_stops_followup(commands, stage):
    blocked = Process(blocked=True)
    commands.overrides[stage] = blocked
    execution = commands.physical() if stage == "lock" else ExecutionProfile(ios_target="simulator")

    async def scenario():
        task = asyncio.create_task(device_hub.prepare_device(execution))
        await asyncio.wait_for(blocked.communicating.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert commands.calls[-1][0] == stage
        assert blocked.kills == blocked.waits == 1
        assert all(process.kills == 0 for process in commands.processes if process is not blocked)

    asyncio.run(scenario())


def test_cancellation_during_spawn_waits_for_owned_process_then_kills_it(commands):
    blocked = Process(blocked=True)
    commands.overrides["boot"] = blocked
    commands.spawn_stage = "boot"

    async def scenario():
        task = asyncio.create_task(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
        await asyncio.wait_for(commands.spawning.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        commands.spawn_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert blocked.kills == blocked.waits == 1
        assert not blocked.communicating.is_set()
        assert commands.calls[-1][0] == "boot"

    asyncio.run(scenario())


def test_repeated_cancellation_cannot_interrupt_owned_process_cleanup(commands):
    blocked = Process(blocked=True)
    commands.overrides["boot"] = blocked

    async def scenario():
        blocked.reap_release = asyncio.Event()
        task = asyncio.create_task(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
        await asyncio.wait_for(blocked.communicating.wait(), 1)
        task.cancel()
        await asyncio.wait_for(blocked.reaping.wait(), 1)
        task.cancel()
        blocked.reap_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert blocked.kills == blocked.waits == 1
        assert commands.calls[-1][0] == "boot"

    asyncio.run(scenario())


def test_cancellation_cleanup_is_bounded_when_reaping_stalls(commands, monkeypatch):
    blocked = Process(blocked=True)
    commands.overrides["boot"] = blocked
    monkeypatch.setattr(device_hub, "_CLEANUP_TIMEOUT", 0.01)

    async def scenario():
        blocked.reap_release = asyncio.Event()
        task = asyncio.create_task(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
        await asyncio.wait_for(blocked.communicating.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert blocked.kills == blocked.waits == 1

    asyncio.run(scenario())


def test_spawn_timeout_is_bounded_and_does_not_leave_pending_spawn(commands, monkeypatch):
    commands.spawn_stage = "boot"
    monkeypatch.setattr(device_hub, "_CLEANUP_TIMEOUT", 0.01)

    async def scenario():
        with pytest.raises(WorkerUnavailable, match="timed out"):
            await asyncio.wait_for(device_hub._run(
                "/usr/bin/xcrun", "simctl", "bootstatus", SIM_A, "-b", timeout=0.01,
            ), 1)
        await asyncio.sleep(0)
        assert commands.processes == []
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_exited_process_is_not_signalled_when_communication_is_cancelled(commands):
    blocked = Process(blocked=True)
    blocked.returncode = 0
    commands.overrides["boot"] = blocked

    async def scenario():
        task = asyncio.create_task(device_hub.prepare_device(ExecutionProfile(ios_target="simulator")))
        await asyncio.wait_for(blocked.communicating.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert blocked.kills == 0
        assert blocked.waits == 1

    asyncio.run(scenario())
