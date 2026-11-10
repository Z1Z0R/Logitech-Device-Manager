#!/usr/bin/env python3
"""Local hardware bridge. All writes are checked against freshly discovered capabilities."""
import concurrent.futures
import errno
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VOLUME_TYPES = {"sinks": "sink", "sources": "source"}
HEADSET_SETTINGS = {
    "inactive": ("CAP_INACTIVE_TIME", "-i", "Inactivity Power Off", 90),
    "preset": ("CAP_EQUALIZER_PRESET", "-p", "Equalizer Profile", 255),
}
EQ_CURVES = {
    "gaming": ("Gaming", (-3, -2, 0, 3, 1)),
    "music": ("Music", (3, 2, 0, 1, 2)),
    "movie": ("Movie", (3, 0, 1, 3, 0)),
}
INPUT_LABELS = {
    "dpi": "DPI", "dpi-old": "DPI", "dpi_extended": "DPI",
    "report_rate": "Polling rate", "report_rate_extended": "Polling rate",
    "onboard_profiles": "Onboard profile",
    "rgb_control": "Software lighting control", "led_control": "Software lighting control",
    "headset_led_control": "Software lighting control", "headset-onboard-effect": "Headset lighting",
}
DPI_KEYS = {"dpi", "dpi-old", "dpi_extended:0", "dpi_extended:1", "dpi_extended:both"}
ZONE_PREFIXES = ("rgb_zone_", "led_zone_")
HITS_KEY = re.compile(r"analog-button-tuning_(actuation|rapid-trigger|haptics)-([01])")
HEADSET_RGB_EFFECT = "headset-onboard-effect"
WAVE_DIRECTIONS = ((1, "left to right"), (6, "right to left"), (3, "center out"))
PATTERN_LABELS = {
    "static": "Solid color", "fixed": "Solid color", "breathe": "Breathing",
    "cycle": "Color cycling", "color cycle": "Color cycling", "wave": "Color Wave",
}
GAMING_MODEL = re.compile(r"\b(?:G\d{3,4}\w*|G PRO|PRO X|PRO 2|LIGHTSPEED|LIGHTSYNC|GAMING)\b", re.I)


def run(*args, allow_failure=False):
    result = subprocess.run(args, capture_output=True, text=True, timeout=4,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode and not allow_failure:
        raise ValueError((result.stderr or result.stdout).strip()[:240] or f"{args[0]} failed.")
    return result.stdout


def hardware_id(namespace, serial):
    if not isinstance(serial, str):
        return None
    serial = serial.strip().upper()
    if not serial or len(serial) > 200 or serial in {"UNKNOWN", "NONE"} or not serial.strip("0-:"):
        return None
    return f"{namespace}:{serial}"


def read_sysfs(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def usb_headset_path(address, root=Path("/sys/bus/usb/devices")):
    vendor, product = address.split(":")
    matches = [node for node in root.iterdir()
               if read_sysfs(node / "idVendor") == vendor and read_sysfs(node / "idProduct") == product]
    return matches[0].resolve() if len(matches) == 1 else None


def usb_headset_identity(address):
    path = usb_headset_path(address)
    return hardware_id(f"usb:{address.split(':')[0]}", read_sysfs(path / "serial")) if path else None


YETI_MODEL = re.compile(r"\byeti\b", re.I)
YETI_GX_AUDIO_FEATURE = 0x8370


def usb_parent(node):
    return next((parent for parent in (node, *node.parents) if (parent / "idVendor").is_file()), None)


def audio_usb_path(properties):
    path = properties.get("sysfs.path")
    if isinstance(path, str) and path.startswith("/devices/"):
        return usb_parent(Path("/sys") / path.lstrip("/"))
    card = str(properties.get("alsa.card", properties.get("api.alsa.card", "")))
    if card.isdecimal():
        return usb_parent((Path("/sys/class/sound") / f"card{card}").resolve())
    return None


def standalone_microphone(node, name):
    props = node.get("properties", {})
    return props.get("device.form_factor") == "microphone" or bool(
        YETI_MODEL.search(f"{name} {node['name']}".replace("_", " ")))


def input_kind(device):
    kind = str(device.kind).lower()
    if kind in ("mouse", "keyboard", "headset", "microphone"):
        return kind
    if YETI_MODEL.search(str(device.name)):
        return "microphone"
    return kind


def yeti_gx_gain(device):
    if str(device.product_id).casefold() != "0afc" or not YETI_MODEL.search(str(device.name)):
        return None
    reply = device.feature_request(YETI_GX_AUDIO_FEATURE, 0x20, 0, 1, 0)
    if reply and len(reply) >= 4 and reply[:2] == b"\x00\x01" and reply[3] <= 100:
        return reply[3]
    return None


def audio_identity(properties):
    path = properties.get("sysfs.path")
    if isinstance(path, str) and path.startswith("/devices/"):
        node = Path("/sys") / path.lstrip("/")
        for parent in (node, *node.parents):
            vendor = read_sysfs(parent / "idVendor")
            if vendor:
                return hardware_id(f"usb:{vendor}", read_sysfs(parent / "serial"))
    vendor = str(properties.get("device.vendor.id", "")).lower().removeprefix("0x")
    if vendor:
        return hardware_id(f"usb:{vendor}", properties.get("device.serial"))
    return hardware_id("audio", properties.get("device.serial"))


def audio_nodes():
    for group, direction in VOLUME_TYPES.items():
        for node in json.loads(run("pactl", "-f", "json", "list", group)):
            props = node.get("properties", {})
            if (node["name"].endswith(".monitor") or node.get("monitor_of_sink") not in
                    (None, "", "n/a", 4294967295)):
                continue
            vendor = str(props.get("device.vendor.id", "")).lower().removeprefix("0x")
            brand = str(props.get("device.vendor.name", "")).lower()
            bluetooth = str(props.get("api.bluez5.device", ""))
            name = str(node.get("description", node["name"]))[:120]
            blue_mic = standalone_microphone(node, name) and (
                vendor == "b58e" or YETI_MODEL.search(f"{name} {node['name']}".replace("_", " ")))
            if vendor != "046d" and "logitech" not in brand and not blue_mic and not (
                    bluetooth and re.search(r"\b(?:logitech|astro|g(?:435|535|733|735| pro))\b", name, re.I)):
                continue
            levels = [int(channel["value"]) for channel in node.get("volume", {}).values()]
            if levels:
                yield direction, node, name, levels


def audio():
    devices = []
    for direction, node, name, levels in audio_nodes():
        standalone = standalone_microphone(node, name)
        label = ("Headphone volume" if standalone else "Headset audio") if direction == "sink" else "Microphone volume"
        kind = "Microphone" if standalone and direction == "source" else label
        usb_path = audio_usb_path(node.get("properties", {})) if kind == "Microphone" else None
        devices.append(dict(id=f"{direction}:{node['name']}", name=name, kind=kind,
                            microphoneUsbPath=str(usb_path) if usb_path else None,
                            hardwareId=audio_identity(node.get("properties", {})),
                            controls=[dict(key="volume", label=label, type="slider",
                                           value=round(max(levels) * 100 / 65536),
                                           muted=bool(node.get("mute")), min=0, max=100)]))
    return devices


def headset_nodes():
    raw = run("headsetcontrol", "-b", "-o", "json", allow_failure=True)
    data = json.loads(raw)
    for node in data.get("devices", [])[:32]:
        vendor = int(str(node.get("id_vendor", "0")), 16)
        product = int(str(node.get("id_product", "0")), 16)
        name = str(node.get("device", ""))[:120]
        if 0 < product <= 65535 and (vendor == 0x046D or
                                     (vendor == 0x9886 and "astro" in name.lower())):
            yield f"{vendor:04x}:{product:04x}", name, node


def equalizer_profiles(node):
    names = node.get("equalizer_presets")
    if not isinstance(names, dict) or not 0 < len(names) <= 256:
        return []
    # HeadsetControl emits the names in preset order. A separate count can
    # describe slots without names, so only map a complete named list.
    count = node.get("equalizer_presets_count", len(names))
    if count != len(names) or any(not name.strip() for name in names):
        return []
    return [dict(value=index, label=name.strip()[:100]) for index, name in enumerate(names)]


def equalizer_spec(node):
    if "CAP_EQUALIZER" not in node.get("capabilities", []):
        return None
    info = node.get("equalizer")
    if not isinstance(info, dict):
        return None
    bands = info.get("bands")
    low, high, step, baseline = (info.get(key) for key in ("min", "max", "step", "baseline"))
    if type(bands) is not int or not 1 <= bands <= 64:
        return None
    if any(type(value) not in (int, float) or not math.isfinite(value)
           for value in (low, high, step, baseline)):
        return None
    if not low <= baseline <= high or not 0 < step <= high - low:
        return None
    max_steps = math.floor((high - low) / step + 1e-9)
    return dict(bands=bands, min=low - baseline, max=round(low + max_steps * step - baseline, 6),
                step=step, baseline=baseline)


def eq_curve(spec, selection):
    profile = selection.get("profile") if isinstance(selection, dict) else None
    count, low, high, step = (spec[key] for key in ("bands", "min", "max", "step"))
    if profile == "custom":
        values = selection.get("bands")
        if not isinstance(values, list) or len(values) != count or any(
                type(value) not in (int, float) or not low <= value <= high for value in values):
            raise ValueError("Enter a supported gain for each EQ band.")
    elif isinstance(profile, str) and profile in EQ_CURVES:
        _, anchors = EQ_CURVES[profile]
        values = []
        for index in range(count):
            position = index * (len(anchors) - 1) / (count - 1) if count > 1 else 2
            left = min(int(position), len(anchors) - 2)
            values.append(anchors[left] + (position - left) * (anchors[left + 1] - anchors[left]))
    else:
        raise ValueError("Choose an equalizer profile.")
    # Align to the device's reported range and increment.
    values = [round(min(high, low + round((min(high, max(low, value)) - low) / step) * step), 6)
              for value in values]
    return profile, values


def headsets():
    devices = []
    nodes = list(headset_nodes())
    custom_profiles = read_preferences(link_preferences_path().with_name("headset-eq.json"))
    for address, name, node in nodes:
        data = node.get("battery", {})
        state = {"BATTERY_AVAILABLE": "Discharging", "BATTERY_CHARGING": "Charging",
                 "BATTERY_UNAVAILABLE": "Unavailable"}.get(data.get("status"), "Unknown")
        level = data.get("level")
        battery = dict(level=level if type(level) is int and 0 <= level <= 100 else None, state=state,
                       directLevel=type(level) is int and 0 <= level <= 100)
        caps = node.get("capabilities", [])
        controls = []
        hardware = usb_headset_identity(address)
        eq = equalizer_spec(node) if sum(other == address for other, _, _ in nodes) == 1 else None
        for key, (cap, _, label, maximum) in HEADSET_SETTINGS.items():
            if cap not in caps or sum(other == address for other, _, _ in nodes) != 1:
                continue
            if key == "preset":
                if eq:
                    continue
                profiles = equalizer_profiles(node)
                if profiles:
                    controls.append(dict(key=key, label=label, type="choice", options=profiles,
                                         value=None, cached=True))
                else:
                    controls.append(dict(key=key, label=label, type="unavailable",
                                         error="This headset does not report named EQ profiles."))
            else:
                controls.append(dict(key=key, label=label, type="number", min=0, max=maximum,
                                     value=None, cached=True))
        if eq:
            custom = custom_profiles.get(hardware or address)
            try:
                _, custom = eq_curve(eq, {"profile": "custom", "bands": custom})
            except ValueError:
                custom = [0] * eq["bands"]
            options = [dict(value=key, label=label) for key, (label, _) in EQ_CURVES.items()]
            options.append(dict(value="custom", label="Custom"))
            controls.append(dict(key="equalizer", label="Equalizer Profile", type="eq", value=None, cached=True,
                                 options=options, custom=custom, **eq))
        usb_path = usb_headset_path(address)
        devices.append(dict(id=f"headset:{address}", name=name, kind="Headset",
                            headsetUsbPath=str(usb_path) if usb_path else None, hardwareId=hardware,
                            battery=battery if data or "CAP_BATTERY_STATUS" in caps else None,
                            controls=controls))
    return devices


class SolaarHandoff(Exception):
    """A HID node vanished or is waiting for the desktop's udev permissions."""


def is_hid_handoff(error):
    hid_errors = (SolaarHandoff,)
    try:
        from logitech_receiver.exceptions import NoReceiver, NoSuchDevice
        hid_errors += (NoReceiver, NoSuchDevice)
    except ImportError:
        pass

    while error is not None:
        if isinstance(error, hid_errors):
            return True
        if isinstance(error, OSError) and error.errno in (
            errno.ENOENT, errno.ENODEV, errno.ENXIO, errno.EIO, errno.EACCES, errno.EPERM
        ):
            return True
        error = error.__cause__ or error.__context__
    return False


def scan_inputs():
    try:
        return input_devices()
    except (Exception, SystemExit) as error:
        if not is_hid_handoff(error):
            raise
        time.sleep(0.25)
        return input_devices()


def peripherals():
    from logitech_receiver import base
    from solaar.cli import _receivers_and_devices

    available = list(base.receivers_and_devices())
    accessible = [info for info in available if os.access(info.path, os.R_OK | os.W_OK)]
    if available and not accessible:
        if any(os.path.exists(info.path) for info in available):
            raise SolaarHandoff("No access to Logitech HID devices. Check the Solaar udev rules and reconnect the device.")
        raise SolaarHandoff("Logitech HID device nodes are unavailable. Reconnect the device if this persists.")
    # A device can leave the receiver and reappear as a direct USB device
    # when plugged in. A failing transport must not discard the other one.
    errors = []
    found = False
    for path in dict.fromkeys(info.path for info in sorted(accessible, key=lambda info: not info.isDevice)):
        try:
            for item in _receivers_and_devices(path):
                devices = [item] if item.isDevice else item
                for device in devices:
                    online = device.online if item.isDevice else device.ping()
                    if online and input_kind(device) in ("mouse", "keyboard", "headset", "microphone"):
                        serial = str(device.unitId or device.serial or "")
                        identity = json.dumps([item.path, device.number, serial], separators=(",", ":"))
                        found = True
                        yield identity, device
        except (Exception, SystemExit) as error:
            errors.append(error)
    if errors and not found:
        # Preserve the underlying errno, even when the Solaar CLI wrapped it.
        raise next((error for error in errors if not is_hid_handoff(error)), errors[0])


def live_read(setting):
    # A cached Solaar preference is different from a confirmed hardware read.
    if not getattr(setting._validator, "readable", True):
        return None
    reply = setting._rw.read(setting._device)
    return setting._validator.validate_read(reply) if reply else None


def choices(values, polling=False):
    result = []
    for value in values:
        label = str(value)
        match = re.fullmatch(r"(\d+)(ms|us)", label) if polling else None
        if match:
            hz = (1000 if match[2] == "ms" else 1000000) / int(match[1])
            label = f"{hz:.4g} Hz ({label})"
        result.append(dict(value=int(value), label=label))
    return result


def advertised_rgb_effects(setting):
    if setting.name != HEADSET_RGB_EFFECT:
        return {option.ID for option in setting._validator.options or ()}

    # Solaar offers guessed effects if the headset's capability reply cannot
    # be parsed. Use only complete entries from the actual cluster reply.
    reply = setting._device.feature_request(setting.feature, 0x10, bytes([setting._CLUSTER]))
    if not reply:
        return set()
    count = min(reply[0], (len(reply) - 1) // 4)
    return {int.from_bytes(reply[1 + index * 4:3 + index * 4], "big") for index in range(count)}


def rgb_options(setting, current, keyboard):
    possible_fields = {field["name"]: field for field in setting.possible_fields}
    allowed = advertised_rgb_effects(setting)
    options = []
    for effect, (name, fields, *metadata) in setting.fields_map.items():
        if effect not in allowed:
            continue
        label = PATTERN_LABELS.get(str(name).casefold(), str(name))
        speed = None
        for param in ("period", "speed"):
            if param in fields and param in possible_fields:
                descriptor = possible_fields[param]
                overrides = metadata[1] if len(metadata) > 1 else {}
                low, high = overrides.get(param, (descriptor["min"], descriptor["max"]))
                default = (metadata[0] if metadata else {}).get(param, 5000 if param == "period" else 100)
                speed = dict(field=param, min=low, max=high, default=max(low, min(high, default)))
                break
        directions = [(None, label)]
        if keyboard and "direction" in fields and "direction" in possible_fields and label == "Color Wave":
            allowed_directions = possible_fields["direction"]["choices"]
            directions = [(direction, f"Color Wave ({caption})")
                          for direction, caption in WAVE_DIRECTIONS if direction in allowed_directions]
            if not directions or (getattr(current, "ID", None) == effect and
                                  getattr(current, "direction", None) not in {d for d, _ in directions}):
                directions.append((None, label))
        for direction, caption in directions:
            value = str(effect) if direction is None else f"{effect}:{direction}"
            options.append(dict(value=value, label=caption, effect=int(effect), direction=direction,
                                color="color" in fields or "color1" in fields, speed=speed))
    return options


def rgb_speed_percent(value, spec):
    fraction = (max(spec["min"], min(spec["max"], value)) - spec["min"]) / (spec["max"] - spec["min"])
    return round(100 * (1 - fraction if spec["field"] == "period" else fraction))


def zone(setting, value, settings, keyboard):
    options = rgb_options(setting, value, keyboard)
    control_key = ("headset_led_control" if setting.name == HEADSET_RGB_EFFECT
                   else "rgb_control" if setting.name.startswith("rgb_") else "led_control")
    control = settings.get(control_key)
    if control:
        control._value = live_read(control)
    software = not control or bool(control._value)
    effect, direction = getattr(value, "ID", None), getattr(value, "direction", None)
    option = next((option for option in options if option["effect"] == effect
                   and (option["direction"] is None or option["direction"] == direction)), None)
    color = getattr(value, "color", getattr(value, "color1", None)) if option and option["color"] else None
    speed = None
    if option and option["speed"]:
        spec = option["speed"]
        speed = rgb_speed_percent(getattr(value, spec["field"], spec["default"]), spec)
    return dict(type="rgb", options=options, value=option["value"] if option else None,
                color=None if color is None else f"#{int(color):06X}", speed=speed, software=software,
                cached=not getattr(setting._validator, "readable", True))


def extended_dpi_controls(setting, value):
    key = setting.name
    axes = {}
    for axis in (0, 1):
        values = setting._validator.choices.get(axis)
        if values:
            spec = dict(key=f"{key}:{axis}", label=f"DPI {'XY'[axis]}", type="choice",
                        value=(value or {}).get(axis, (value or {}).get(str(axis))),
                        options=choices(values))
            axes[axis] = spec
            yield spec
    if 0 in axes and 1 in axes:
        y_values = {option["value"] for option in axes[1]["options"]}
        common = [option for option in axes[0]["options"] if option["value"] in y_values]
        if common:
            x, y = axes[0]["value"], axes[1]["value"]
            yield dict(key=f"{key}:both", label="DPI (X and Y)", type="choice",
                       value=x if x == y else None, options=common,
                       linkValue=x if x in y_values else None)


def input_controls(device):
    settings = {s.name: s for s in device.settings}
    kind = input_kind(device)
    if kind == "microphone":
        try:
            gain = yeti_gx_gain(device)
        except Exception:
            gain = None
        if gain is not None:
            yield dict(key="microphone_gain", label="Microphone gain", type="gain", min=0, max=100, value=gain)
    for key, setting in settings.items():
        hits = HITS_KEY.fullmatch(key) if kind == "mouse" else None
        if not hits and key not in INPUT_LABELS and not key.startswith(ZONE_PREFIXES):
            continue
        if kind in ("headset", "microphone") and not (
                key in (HEADSET_RGB_EFFECT, "headset_led_control", "rgb_control", "led_control")
                or key.startswith(ZONE_PREFIXES)):
            continue
        label = INPUT_LABELS.get(key, str(setting.label)[:100])
        try:
            value = live_read(setting)
            if hits:
                bounds = setting.range
                if bounds and value is not None and bounds[0] <= value <= bounds[1]:
                    yield dict(key=key, type="hits", value=value, min=bounds[0], max=bounds[1],
                               field=hits[1], button=int(hits[2]))
            elif key == HEADSET_RGB_EFFECT or key.startswith(ZONE_PREFIXES):
                lighting = zone(setting, value, settings, kind == "keyboard")
                if lighting["options"]:
                    yield dict(key=key, label=label, **lighting)
            elif key.endswith("_control"):
                yield dict(key=key, label=label, type="choice", value=None if value is None else int(value),
                           options=[dict(value=0, label="Device"), dict(value=1, label="Software")])
            elif key == "dpi_extended":
                yield from extended_dpi_controls(setting, value)
            elif setting.choices:
                yield dict(key=key, label=label, type="choice", value=value,
                           options=choices(setting.choices, key.startswith("report_rate")))
        except Exception as error:
            if not hits:
                yield dict(key=key, label=label,
                           type="unavailable", error=str(error)[:180])


def dpi_field(control):
    if control["key"] not in DPI_KEYS or control["type"] != "choice":
        return control
    values = [option["value"] for option in control["options"]]
    field = {key: value for key, value in control.items() if key != "options"}
    return dict(field, type="dpi", min=min(values), max=max(values))


def kernel_hid_serial(hid_path):
    try:
        for line in (hid_path / "uevent").read_text().splitlines():
            if line.startswith("HID_UNIQ="):
                serial = line.partition("=")[2]
                # HID++ uses four dash-separated device-serial bytes.
                if re.fullmatch(r"[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){3}", serial):
                    serial = serial.replace("-", "").upper()
                    if serial != "00000000":
                        return serial
                break
    except OSError:
        pass
    return None


def link_preference_keys(identity, device):
    serial = json.loads(identity)[2].strip().upper()
    if not serial.strip("0-:"):
        serial = ""
    legacy = f"{device.name.casefold()}:{serial}" if serial else None
    # Prefer the same hardware identifier Linux sees on both transports.
    kernel_serial = kernel_hid_serial(Path("/sys/class/hidraw") / Path(device.path).name / "device") if device.path else None
    stable = kernel_serial or serial.replace("-", "")
    return hardware_id("hidpp", stable), legacy


def link_preferences_path():
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state_home / "omarchy-logitech-device-manager" / "dpi-links.json"


def read_preferences(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_preference(path, preference, value, legacy=None):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        links = read_preferences(path)
        if legacy:
            links.pop(legacy, None)
        if value is None:
            links.pop(preference, None)
        else:
            links[preference] = value
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as staged:
            staged_path = Path(staged.name)
            try:
                json.dump(links, staged)
                staged.flush()
                os.fsync(staged.fileno())
                staged_path.replace(path)
            finally:
                staged_path.unlink(missing_ok=True)


def input_devices():
    from logitech_receiver.common import BatteryLevelApproximation

    devices = []
    links = read_preferences(link_preferences_path())
    for identity, device in peripherals():
        kind = input_kind(device).capitalize()
        status = None
        try:
            battery = device.battery()
            if battery:
                state = battery.status.name if battery.status is not None else "UNKNOWN"
                state = {"RECHARGING": "Charging", "SLOW_RECHARGE": "Charging",
                         "ALMOST_FULL": "Charging, almost full"}.get(state, state.replace("_", " ").capitalize())
                level = battery.level if isinstance(battery.level, int) and 0 <= battery.level <= 100 else None
                status = dict(level=level, state=state, directLevel=level is not None,
                              approximate=isinstance(level, BatteryLevelApproximation))
        except Exception:
            status = dict(level=None, state="Unavailable")
        try:
            controls = [dpi_field(control) for control in input_controls(device)]
        except Exception as error:
            controls = [dict(type="unavailable", key="settings", label="Device settings", error=str(error)[:180])]
        # Solaar also supports productivity devices. Keep gaming-model names and
        # devices exposing gaming features even if the receiver uses a generic name.
        name = str(device.name)[:120]
        if kind == "Headset":
            if any(control["type"] == "rgb" for control in controls):
                kind = "Headset lighting"
            elif (status or {}).get("directLevel"):
                controls = []
            else:
                continue
        elif kind == "Microphone":
            if not any(control["type"] in ("rgb", "gain") for control in controls):
                continue
        elif not GAMING_MODEL.search(name):
            if not any(c["type"] == "hits" or c["key"].startswith(("report_rate", "onboard_profiles", *ZONE_PREFIXES))
                       for c in controls):
                continue
        hid_path = (Path("/sys/class/hidraw") / Path(device.path).name / "device").resolve() if device.path else None
        usb_path = None
        if kind.startswith("Headset"):
            route = hid_path
            if not route and device.receiver and device.receiver.path:
                route = (Path("/sys/class/hidraw") / Path(device.receiver.path).name / "device").resolve()
            if route:
                usb_path = next((str(parent) for parent in route.parents if (parent / "idVendor").is_file()), None)
        preference, legacy = link_preference_keys(identity, device)
        mic_path = usb_parent(hid_path) if hid_path and kind == "Microphone" else None
        devices.append(dict(id=f"input:{identity}", headsetUsbPath=usb_path,
                            microphoneUsbPath=str(mic_path) if mic_path else None,
                            name=name, kind=kind, battery=status,
                            controls=controls, hid_path=str(hid_path) if hid_path else None,
                            hardwareId=preference, canSaveDpiLink=preference is not None,
                            dpiLinked=links.get(preference, links.get(legacy, False))))
    return devices


def kernel_batteries(root=Path("/sys/class/power_supply")):
    for supply in sorted(root.glob("hidpp_battery_*")):
        try:
            properties = dict(line.split("=", 1) for line in (supply / "uevent").read_text().splitlines())
            if properties.get("POWER_SUPPLY_MANUFACTURER", "").casefold() != "logitech":
                continue
            if properties.get("POWER_SUPPLY_ONLINE") == "0":
                continue
            name = properties.get("POWER_SUPPLY_MODEL_NAME", "")[:120]
            if not name:
                continue
            state = properties.get("POWER_SUPPLY_STATUS", "Unknown")
            level = properties.get("POWER_SUPPLY_CAPACITY", "")
            level = int(level) if level.isdecimal() else None
            hid_path = (supply / "device").resolve(strict=True)
        except (OSError, ValueError):
            continue
        serial = kernel_hid_serial(hid_path)
        identity = hardware_id("hidpp", serial)
        yield dict(id=f"battery:{supply.name}", name=name, kind="Logitech peripheral",
                   hardwareId=identity, batteryIdentity=identity, kernelBattery=True,
                   battery=dict(level=level if level is not None and 0 <= level <= 100 else None,
                                state=state), hid_path=str(hid_path),
                   controls=[dict(key="battery_only", type="unavailable", label="Device controls",
                                  error="Linux can read the battery over this connection. Hardware controls are unavailable.")])


def merge_kernel_batteries(devices, batteries):
    by_path = {device["hid_path"]: device for device in devices if device.get("hid_path")}
    for fallback in batteries:
        match = by_path.get(fallback["hid_path"])
        if match:
            match["batteryIdentity"] = fallback["batteryIdentity"]
            match["hardwareId"] = fallback["hardwareId"] or match.get("hardwareId")
            match["kernelBattery"] = True
            battery = match.get("battery") or {}
            kernel = fallback["battery"]
            if not battery:
                match["battery"] = kernel
                continue
            if battery.get("level") is None and kernel["level"] is not None:
                battery.update(level=kernel["level"], approximate=False, directLevel=False)
            if battery.get("state") in ("Unknown", "Unavailable", "Offline") or kernel["state"] in ("Charging", "Full"):
                battery["state"] = kernel["state"]
        elif GAMING_MODEL.search(fallback["name"]):
            devices.append(fallback)
    for device in devices:
        if path := device.pop("hid_path", None):
            device["batteryKey"] = path


def merge_headset_batteries(devices):
    solaar = [device for device in devices if device["id"].startswith("input:") and device["kind"].startswith("Headset")]
    headsets = [device for device in devices if device["id"].startswith("headset:")]
    for headset in headsets:
        path = headset.get("headsetUsbPath")
        if not path or sum(card.get("headsetUsbPath") == path for card in headsets) != 1:
            continue
        matches = [device for device in solaar if device.get("headsetUsbPath") == path]
        if len(matches) != 1:
            continue
        match = matches[0]
        direct = match.get("battery") or {}
        battery = headset.get("battery") or {}
        if direct.get("directLevel") and not battery.get("directLevel"):
            headset["battery"] = dict(direct, state=battery.get("state", direct["state"]))
        if not match["controls"]:
            devices.remove(match)
    for device in devices:
        device.pop("headsetUsbPath", None)


def merge_microphone_controls(devices):
    microphones = [device for device in devices if device["kind"] == "Microphone"]
    for hardware in microphones:
        if not hardware["id"].startswith("input:") or not hardware.get("microphoneUsbPath"):
            continue
        matches = [device for device in microphones
                   if device["microphoneUsbPath"] == hardware["microphoneUsbPath"]]
        if len(matches) != 2:
            continue
        source = next((device for device in matches if device["id"].startswith("source:")), None)
        if source:
            source["controls"].extend(dict(control, route=hardware["id"]) for control in hardware["controls"])
            devices.remove(hardware)
    for device in devices:
        device.pop("microphoneUsbPath", None)


CAMERA_CONTROL = re.compile(r"^\s*([a-z0-9_]+)\s+0x[0-9a-fA-F]+\s+\(([a-z0-9]+)\)\s*:\s*(.*)$")
CAMERA_MENU_ENTRY = re.compile(r"^\s*(-?\d+):\s*(.+)$")


def camera_controls(text):
    controls = []
    menu = None
    for line in text.splitlines():
        if match := CAMERA_CONTROL.match(line):
            key, control_type, data = match.groups()
            menu = None
            fields = {name: int(value) for name, value in re.findall(r"\b(min|max|step|value)=(-?\d+)\b", data)}
            flags = set(re.split(r"[,\s]+", data.partition("flags=")[2]))
            if flags & {"disabled", "read-only", "write-only"} and control_type != "button":
                continue
            spec = dict(key=key, label=key.replace("_", " ").capitalize(),
                        writable=not bool(flags & {"inactive", "grabbed", "disabled", "read-only"}))
            if control_type in ("menu", "intmenu") and "value" in fields:
                menu = dict(spec, type="choice", value=fields["value"], options=[])
                controls.append(menu)
            elif control_type == "bool" and "value" in fields:
                controls.append(dict(spec, type="choice", value=fields["value"],
                                     options=[dict(value=0, label="Off"), dict(value=1, label="On")]))
            elif control_type in ("int", "int64") and all(key in fields for key in ("min", "max", "step", "value")):
                if fields["step"] > 0 and -(2**53 - 1) <= fields["min"] <= fields["value"] <= fields["max"] < 2**53:
                    controls.append(dict(spec, type="number", **fields))
            elif control_type == "button" and key in ("pan_reset", "tilt_reset"):
                controls.append(dict(spec, type="camera_action"))
        elif menu is not None and (entry := CAMERA_MENU_ENTRY.match(line)):
            menu["options"].append(dict(value=int(entry[1]), label=entry[2][:100]))
        elif line.strip():
            menu = None
    return [control for control in controls if control.get("options") != []]


def webcam_nodes(root=Path("/sys/class/video4linux")):
    for entry in sorted(root.glob("video*"), key=lambda path: (len(path.name), path.name)):
        if not re.fullmatch(r"video\d+", entry.name):
            continue
        usb = usb_parent(entry.resolve())
        if not usb or read_sysfs(usb / "idVendor").casefold() != "046d":
            continue
        path = f"/dev/{entry.name}"
        serial = read_sysfs(usb / "serial")
        identity = json.dumps([path, str(usb), serial], separators=(",", ":"))
        yield identity, path, usb, serial, read_sysfs(usb / "product") or read_sysfs(entry / "name")


def webcam_has_capture(info):
    # Device Caps describes this node. The aggregate Capabilities may also
    # list capture on a different node belonging to the same USB camera.
    _, marker, caps = info.partition("Device Caps")
    if not marker:
        caps = info.partition("Capabilities")[2]
    return any(line.strip() in ("Video Capture", "Video Capture Multiplanar", "Video Capture Multiplane")
               for line in caps.splitlines())


def webcams():
    cameras = {}
    for identity, path, usb, serial, name in webcam_nodes():
        if usb in cameras and cameras[usb]["controls"]:
            continue
        try:
            if not webcam_has_capture(run("v4l2-ctl", "--device", path, "--info")):
                continue
            controls = camera_controls(run("v4l2-ctl", "--device", path, "--list-ctrls-menus"))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
        cameras[usb] = dict(id=f"webcam:{identity}", name=(name or "Logitech webcam")[:120], kind="Webcam",
                            hardwareId=hardware_id("usb:046d", serial), controls=controls)
    return list(cameras.values())


def set_webcam(identity, key, value):
    path = next((node for candidate, node, *_ in webcam_nodes() if candidate == identity), None)
    if path is None or not webcam_has_capture(run("v4l2-ctl", "--device", path, "--info")):
        raise ValueError("The Logitech webcam is no longer available.")
    controls = camera_controls(run("v4l2-ctl", "--device", path, "--list-ctrls-menus"))
    control = next((control for control in controls if control["key"] == key), None)
    if not control or not control["writable"]:
        raise ValueError("This camera setting is unavailable. Check whether an automatic mode or another app controls it.")
    if control["type"] == "camera_action":
        if value is not True:
            raise ValueError("The camera action must be explicitly requested.")
        run("v4l2-ctl", "--device", path, f"--set-ctrl={key}=1")
        return {}
    if control["type"] == "choice":
        if type(value) is not int or not any(option["value"] == value for option in control["options"]):
            raise ValueError("Choose a mode reported by the webcam.")
    else:
        number(value, control["min"], control["max"])
        if (value - control["min"]) % control["step"]:
            raise ValueError("The value does not match the webcam's supported increments.")
    run("v4l2-ctl", "--device", path, f"--set-ctrl={key}={value}")
    readback = run("v4l2-ctl", "--device", path, f"--get-ctrl={key}")
    match = re.search(rf"^{re.escape(key)}:\s*(-?\d+)\s*$", readback, re.MULTILINE)
    return dict(value=int(match[1])) if match else {}


def snapshot():
    sources = [(scan_inputs, "Solaar"), (audio, "PipeWire audio")]
    notices = []
    if shutil.which("v4l2-ctl"):
        sources.append((webcams, "Webcams"))
    elif any(webcam_nodes()):
        notices.append("Install v4l-utils to manage Logitech webcams.")
    if shutil.which("headsetcontrol"):
        sources.append((headsets, "HeadsetControl"))
    else:
        notices.append("Install HeadsetControl for supported headset battery, EQ and auto power-off.")
    devices = []
    solaar_handoff = ""
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as pool:
        for future, name in [(pool.submit(fn), name) for fn, name in sources]:
            try:
                devices.extend(future.result())
            except (Exception, SystemExit) as error:
                notice = f"{name}: {str(error)[:200]}"
                if name == "Solaar" and is_hid_handoff(error):
                    solaar_handoff = notice
                else:
                    notices.append(notice)
    merge_kernel_batteries(devices, kernel_batteries())
    merge_headset_batteries(devices)
    merge_microphone_controls(devices)
    return dict(devices=devices[:32], notices=notices, solaarHandoff=solaar_handoff)


def number(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Enter a whole number between {low} and {high}.")
    return value


def set_input(identity, key, value):
    matches = [device for candidate, device in peripherals() if candidate == identity]
    if len(matches) != 1:
        raise ValueError("Device disconnected or ambiguous.")
    device = matches[0]
    from logitech_receiver.exceptions import FeatureCallError
    from logitech_receiver.hidpp20_constants import ErrorCode

    try:
        if key == "microphone_gain":
            if input_kind(device) != "microphone" or yeti_gx_gain(device) is None:
                raise ValueError("The microphone does not expose hardware gain.")
            level = number(value, 0, 100)
            reply = device.feature_request(YETI_GX_AUDIO_FEATURE, 0x30, 0, 1, level)
            if not reply or reply[:3] != bytes([0, 1, level]):
                raise ValueError("The microphone did not acknowledge the gain change.")
            return dict(value=level)
        if key == "dpi_link":
            return set_dpi_link(identity, device, value)
        control = next((control for control in input_controls(device) if control["key"] == key), None)
        if not control or control["type"] not in ("rgb", "choice", "hits"):
            raise ValueError("The device no longer exposes this control.")
        return set_input_control(device, control, value)
    except FeatureCallError as error:
        if key not in DPI_KEYS and key != "dpi_link":
            raise
        if error.error == ErrorCode.LOGITECH_ERROR and active_onboard_profile(device):
            return dict(error="The active onboard profile blocked the DPI change.", errorCode="dpiOnboardProfile")
        raise ValueError(f"The mouse rejected the DPI change (HID++ error {int(error.error)}).") from error


def active_onboard_profile(device):
    try:
        profile = next((setting for setting in device.settings if setting.name == "onboard_profiles"), None)
        return profile is not None and bool(live_read(profile))
    except Exception:
        return False


def set_dpi_link(identity, device, linked):
    from logitech_receiver.settings_templates import check_feature_setting

    preference, legacy = link_preference_keys(identity, device)
    if type(linked) is not bool or not preference:
        raise ValueError("The device does not support a saved DPI link preference.")

    confirmed = dict(value=linked)
    if linked:
        setting = check_feature_setting(device, "dpi_extended")
        if setting is None:
            raise ValueError("The device no longer exposes separate DPI axes.")
        current = live_read(setting)
        if not isinstance(current, dict) or 0 not in current or 1 not in current:
            raise ValueError("Could not read the current DPI axes.")
        controls = list(extended_dpi_controls(setting, current))
        combined = next((control for control in controls if control["key"] == "dpi_extended:both"), None)
        if not combined:
            raise ValueError("The X and Y axes report no shared DPI values.")
        if combined["value"] is None and combined["linkValue"] is not None:
            shared = combined["linkValue"]
            if setting.write({**current, 0: shared, 1: shared}, save=False) is None:
                raise ValueError("The device did not acknowledge the setting.")
            for control in controls:
                control["value"] = shared
        confirmed["dpiValues"] = {control["key"]: control["value"] for control in controls}
    save_preference(link_preferences_path(), preference, True if linked else None, legacy)
    return confirmed


def set_input_control(device, control, value):
    key = control["key"]
    base, _, axis = key.partition(":")
    setting = next(s for s in device.settings if s.name == base)
    if control["type"] == "hits":
        level = number(value, control["min"], control["max"])
        result = setting.write(level, save=False)
        if result is None:
            raise ValueError("The mouse did not acknowledge the trigger setting.")
        return dict(value=int(result))
    selected = value.get("effect") if control["type"] == "rgb" and isinstance(value, dict) else value
    option = next((o for o in control["options"] if o["value"] == selected), None)
    if option is None:
        if key in DPI_KEYS:
            raise ValueError("The mouse does not support that exact DPI.")
        raise ValueError("The selected value is unsupported.")
    if control["type"] == "rgb":
        if not control["software"]:
            raise ValueError("Enable software lighting control first.")
        effect = option["effect"]
        old = live_read(setting)
        _, fields, *effect_metadata = setting.fields_map[effect]
        defaults = dict(color=0xFFFFFF, color1=0xFFFFFF, color2=0x0000FF,
                        period=5000, intensity=100, speed=100, saturation=255)
        if effect_metadata:
            defaults.update(effect_metadata[0])
        if getattr(old, "ID", None) == effect:
            defaults.update(vars(old))
        params = {field: defaults.get(field, 0) for field in fields}
        if option["direction"] is not None:
            params["direction"] = option["direction"]
        color = None
        if option["color"]:
            color = value.get("color", "")
            if not isinstance(color, str) or not re.fullmatch(r"#[\da-fA-F]{6}", color):
                raise ValueError("Use a six-digit RGB color such as #00AAFF.")
            params["color1" if "color1" in fields else "color"] = int(color[1:], 16)
        speed = None
        if spec := option["speed"]:
            raw = max(spec["min"], min(spec["max"], params[spec["field"]]))
            if value.get("speed") is not None:
                percent = number(value["speed"], 0, 100)
                fraction = (100 - percent if spec["field"] == "period" else percent) / 100
                raw = round(spec["min"] + fraction * (spec["max"] - spec["min"]))
            params[spec["field"]] = raw
            speed = rgb_speed_percent(raw, spec)
        result = setting.write(setting._validator.data_class(ID=effect, **params), save=False)
        confirmed = dict(value=selected, color=color, speed=speed)
    elif axis == "both":
        old = live_read(setting)
        if not isinstance(old, dict) or 0 not in old or 1 not in old:
            raise ValueError("Could not read the current DPI axes.")
        # Extended DPI writes a whole sensor configuration in one HID++ request.
        # Preserve lift-off distance and send the two axes together.
        updated = {**old, 0: selected, 1: selected}
        result = setting.write(updated, save=False)
        confirmed = dict(value=selected)
    elif axis:
        old = live_read(setting)
        if not isinstance(old, dict):
            raise ValueError("Could not read the current DPI axes.")
        setting._value = old
        axis_key = int(axis) if int(axis) in old else axis
        result = setting.write_key_value(axis_key, selected, save=False)
        confirmed = dict(value=selected)
    else:
        result = setting.write(bool(selected) if base.endswith("_control") else selected, save=False)
        confirmed = dict(value=selected)
    if result is None:
        raise ValueError("The device did not acknowledge the setting.")
    return confirmed


def write(device_id, key, value):
    kind, identity = device_id.split(":", 1)
    if kind == "webcam":
        return set_webcam(identity, key, value)
    if kind in VOLUME_TYPES.values() and key in ("volume", "mute"):
        if not any(direction == kind and node["name"] == identity for direction, node, _, _ in audio_nodes()):
            raise ValueError("The Logitech audio endpoint is no longer available.")
        if key == "mute":
            if type(value) is not bool:
                raise ValueError("Expected a mute state.")
            run("pactl", f"set-{kind}-mute", identity, "1" if value else "0")
            return dict(muted=value)
        level = number(value, 0, 100)
        run("pactl", f"set-{kind}-volume", identity, f"{level}%")
        return dict(value=level)
    if kind == "input":
        return set_input(identity, key, value)
    if kind == "headset" and (key in HEADSET_SETTINGS or key == "equalizer") and re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", identity):
        matches = [node for address, _, node in headset_nodes() if address == identity]
        if len(matches) != 1:
            raise ValueError("The headset is disconnected or ambiguous.")
        node = matches[0]
        if key == "equalizer":
            spec = equalizer_spec(node)
            if spec is None:
                raise ValueError("The headset does not support editable EQ bands.")
            profile, bands = eq_curve(spec, value)
            wire_values = ",".join(f"{gain + spec['baseline']:.6f}".rstrip("0").rstrip(".") for gain in bands)
            result = json.loads(run("headsetcontrol", "-d", identity, "-e", wire_values, "-o", "json"))
            replies = result.get("devices", [])
            if len(replies) != 1 or replies[0].get("status") != "success":
                raise ValueError("The headset did not accept the equalizer profile.")
            if profile == "custom":
                hardware = usb_headset_identity(identity) or identity
                save_preference(link_preferences_path().with_name("headset-eq.json"), hardware, bands)
            return dict(value=profile)
        cap, flag, _, maximum = HEADSET_SETTINGS[key]
        level = number(value, 0, maximum)
        if cap not in node.get("capabilities", []):
            raise ValueError("The headset no longer exposes this control.")
        if key == "preset" and not any(option["value"] == level for option in equalizer_profiles(matches[0])):
            raise ValueError("The headset no longer reports that EQ profile.")
        run("headsetcontrol", "-d", identity, flag, str(level))
        return dict(value=level)
    raise ValueError("Unsupported device or control.")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["scan"]:
            result = snapshot()
        elif sys.argv[1:] == ["battery"]:
            batteries = list(kernel_batteries())
            by_identity = {}
            for item in batteries:
                if identity := item["batteryIdentity"]:
                    # If two transports report online together, use the exact
                    # path until there is one unambiguous replacement.
                    by_identity[identity] = None if identity in by_identity else item["battery"]
            result = dict(batteries={item["hid_path"]: item["battery"] for item in batteries},
                          batteriesByIdentity=by_identity, devices=[])
            merge_kernel_batteries(result["devices"], batteries)
            for device in result["devices"]:
                device["controls"] = []
        elif len(sys.argv) == 5 and sys.argv[1] == "set":
            result = write(sys.argv[2], sys.argv[3], json.loads(sys.argv[4]))
        else:
            raise ValueError("Expected scan, battery, or set.")
    except (Exception, SystemExit) as error:
        result = dict(error=str(error)[:240])
    print(json.dumps(result, ensure_ascii=True))
