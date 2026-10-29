import json
from typing import Dict

import time

import paho.mqtt.client as mqtt

from . import params as _params
from . import state as _state
from .config import *
from .config import ENABLE_PARAM_CONTROLS
from .loggers import log
from .sensors import SENSORS, get_group_title, get_grouped_sensor_keys, get_sensor_group

# Minimum interval (seconds) between manual RPC sends triggered from HA.
# Prevents a retained-message replay storm or a button mash from flooding
# the inverter dongle with dev_rpc requests.
_MANUAL_RPC_MIN_INTERVAL = 2.0
_last_manual_rpc_ts: float = 0.0
_PARAM_WRITE_MIN_INTERVAL = 2.0
_last_param_write_ts: float = 0.0
# A DTU restart drops the dongle for a minute or two, so the restart
# button gets a longer throttle than a parameter write: it exists to stop
# an accidental double-tap, not to rate-limit real use.
_RESTART_MIN_INTERVAL = 30.0
_last_restart_ts: float = 0.0

_SECTION_PREFIXES = (
    "Device Info - ",
    "Battery Status - ",
    "BMS Status - ",
    "Grid Status - ",
    "Load Status - ",
    "PV Panel Status - ",
    "Settings - ",
)


def _trim_section_prefix(name: str) -> str:
    for prefix in _SECTION_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def display_sensor_name(base_name: str) -> str:
    trimmed = _trim_section_prefix(base_name)
    return f"{ENTITY_PREFIX} {trimmed}".strip() if ENTITY_PREFIX else trimmed


def device_id_for_group(group: str) -> str:
    if group == "main":
        return DEVICE_ID
    return f"{DEVICE_ID}_{group}"


def state_topic_for_group(group: str) -> str:
    if group == "main":
        return STATE_TOPIC
    if STATE_TOPIC.endswith("/state"):
        return f"{STATE_TOPIC[:-6]}/{group}/state"
    return f"{STATE_TOPIC}/{group}"


def availability_topic_for_group(group: str) -> str:
    if group == "main":
        return AVAILABILITY_TOPIC
    if AVAILABILITY_TOPIC.endswith("/availability"):
        return f"{AVAILABILITY_TOPIC[:-13]}/{group}/availability"
    return f"{AVAILABILITY_TOPIC}/{group}"


def device_info(group: str) -> Dict[str, object]:
    if group == "main":
        return {
            "identifiers": [DEVICE_ID],
            "name": DEVICE_NAME,
            "manufacturer": MANUFACTURER,
            "model": MODEL_NAME,
        }
    group_title = get_group_title(group)
    group_device_id = device_id_for_group(group)
    return {
        "identifiers": [group_device_id],
        "name": f"{DEVICE_NAME} {group_title}".strip(),
        "manufacturer": MANUFACTURER,
        "model": MODEL_NAME,
        "via_device": DEVICE_ID,
    }


def create_mqtt_client() -> mqtt.Client:
    try:
        c = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
            client_id=f"{DEVICE_ID}_bridge",
            protocol=mqtt.MQTTv311,
        )
    except Exception:
        c = mqtt.Client(client_id=f"{DEVICE_ID}_bridge", protocol=mqtt.MQTTv311)

    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    c.reconnect_delay_set(min_delay=5, max_delay=30)
    c.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
    return c


client = create_mqtt_client()


def publish_sensor_discovery(key: str) -> None:
    if key not in SENSORS:
        return

    meta = SENSORS[key]
    group = get_sensor_group(key)
    group_device_id = device_id_for_group(group)
    topic = f"{MQTT_DISCOVERY_PREFIX}/sensor/{group_device_id}/{key}/config"
    payload = {
        "name": display_sensor_name(str(meta["name"])),
        "unique_id": f"{group_device_id}_{key}",
        "state_topic": state_topic_for_group(group),
        "value_template": f"{{{{ value_json.{key} }}}}",
        "availability_topic": availability_topic_for_group(group),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_info(group),
    }

    # Only send an icon when one is defined. Sending "icon": null -- or a
    # static icon -- would override Home Assistant's dynamic device_class
    # icon (e.g. the battery level icon that changes with the state).
    if meta.get("icon"):
        payload["icon"] = meta["icon"]
    if meta.get("unit"):
        payload["unit_of_measurement"] = meta["unit"]
    if meta.get("device_class"):
        payload["device_class"] = meta["device_class"]
    if meta.get("state_class"):
        payload["state_class"] = meta["state_class"]
    if meta.get("entity_category"):
        payload["entity_category"] = meta["entity_category"]
    if "enabled_by_default" in meta:
        payload["enabled_by_default"] = bool(meta["enabled_by_default"])

    client.publish(topic, json.dumps(payload), retain=True)
    _state.PUBLISHED_SENSOR_KEYS.add(key)


# Every key in SENSORS is published. Keeping a second hand-maintained list
# here caused silent drift: keys the parser produced were dropped because
# nobody remembered to add them, leaving entities stuck on "unknown".
ACTIVE_KEYS = set(SENSORS)


MANUAL_RPC_COMMAND_TOPIC = f"{DEVICE_ID}/cmd/manual_rpc"
MANUAL_RPC_PRESS_PAYLOAD = "PRESS"
MANUAL_PAYLOAD_COMMAND_TOPIC = f"{DEVICE_ID}/cmd/manual_payload"
RESTART_DTU_COMMAND_TOPIC = f"{DEVICE_ID}/cmd/restart_dtu"
RESTART_DTU_PRESS_PAYLOAD = "PRESS"
PARAM_STATE_TOPIC = f"{DEVICE_ID}/params/state"
PARAM_COMMAND_PREFIX = f"{DEVICE_ID}/cmd/param/"


def param_command_topic(key: str) -> str:
    return f"{PARAM_COMMAND_PREFIX}{key}"


def publish_manual_rpc_button_discovery() -> None:
    """Register a HA button that sends one dev_rpc request using the known
    working template -- the quick/default option."""
    group_device_id = device_id_for_group("main")
    topic = f"{MQTT_DISCOVERY_PREFIX}/button/{group_device_id}/manual_rpc/config"
    payload = {
        "name": display_sensor_name("RPC Poll (plantilla)"),
        "unique_id": f"{group_device_id}_manual_rpc",
        "command_topic": MANUAL_RPC_COMMAND_TOPIC,
        "payload_press": MANUAL_RPC_PRESS_PAYLOAD,
        "availability_topic": availability_topic_for_group("main"),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_info("main"),
        "icon": "mdi:send",
    }
    client.publish(topic, json.dumps(payload), retain=True)


def publish_restart_dtu_button_discovery() -> None:
    """Register a HA button that reboots the DTU (dongle), reproducing the
    portal's own Restart button.  Gated behind ENABLE_PARAM_CONTROLS like
    the other cloud-backed controls: in normal mode a press is served by
    the portal API (cloud_api.restart_dtu), which needs the configured
    portal credentials; in isolated mode the local proxy replays the
    captured restart envelope instead."""
    if not ENABLE_PARAM_CONTROLS:
        return
    group_device_id = device_id_for_group("main")
    topic = f"{MQTT_DISCOVERY_PREFIX}/button/{group_device_id}/restart_dtu/config"
    payload = {
        "name": display_sensor_name("Restart DTU"),
        "unique_id": f"{group_device_id}_restart_dtu",
        "command_topic": RESTART_DTU_COMMAND_TOPIC,
        "payload_press": RESTART_DTU_PRESS_PAYLOAD,
        "availability_topic": availability_topic_for_group("main"),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_info("main"),
        "icon": "mdi:restart",
    }
    client.publish(topic, json.dumps(payload), retain=True)


def publish_manual_payload_text_discovery() -> None:
    """Register a HA text field where you can type an arbitrary payload
    (JSON or raw text) and have it sent straight to the inverter's dev_rpc
    topic -- for probing what commands it does/doesn't respond to. Missing
    't'/'s' transaction fields are auto-filled if you send a JSON object,
    so you only need to type the part you're testing, e.g.:
      {"c":5,"b":{"ct":[{"cn":"OgBU"}]}}
    Non-JSON text is sent verbatim as raw bytes."""
    group_device_id = device_id_for_group("main")
    topic = f"{MQTT_DISCOVERY_PREFIX}/text/{group_device_id}/manual_payload/config"
    payload = {
        "name": display_sensor_name("RPC Manual (JSON o texto libre)"),
        "unique_id": f"{group_device_id}_manual_payload",
        "command_topic": MANUAL_PAYLOAD_COMMAND_TOPIC,
        "mode": "text",
        "max": 255,
        "availability_topic": availability_topic_for_group("main"),
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_info("main"),
        "icon": "mdi:code-json",
    }
    client.publish(topic, json.dumps(payload), retain=True)


def publish_param_discovery() -> None:
    """Publish Home Assistant discovery for phase-1 parameter controls."""

    if not ENABLE_PARAM_CONTROLS:
        return

    group_device_id = device_id_for_group("main")
    for key, meta in _params.CATALOG.items():
        base_payload = {
            "name": display_sensor_name(str(meta["name"])),
            "unique_id": f"{group_device_id}_param_{key}",
            "state_topic": PARAM_STATE_TOPIC,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "command_topic": param_command_topic(key),
            "availability_topic": availability_topic_for_group("main"),
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device_info("main"),
        }

        if meta["kind"] == "number":
            topic = f"{MQTT_DISCOVERY_PREFIX}/number/{group_device_id}/{key}/config"
            payload = dict(base_payload)
            payload.update(
                {
                    "min": meta["min"],
                    "max": meta["max"],
                    "step": meta["step"],
                    "unit_of_measurement": meta["unit"],
                    "mode": "box",
                }
            )
        elif meta["kind"] == "select":
            topic = f"{MQTT_DISCOVERY_PREFIX}/select/{group_device_id}/{key}/config"
            payload = dict(base_payload)
            payload["options"] = list(meta["options"].keys())  # type: ignore[attr-defined]
        else:
            log(f"[PARAM] Skipping discovery for unsupported kind {meta['kind']!r} ({key})", level="warning")
            continue

        client.publish(topic, json.dumps(payload), retain=True)


def publish_param_state() -> None:
    """Publish the latest telemetry-confirmed parameter values, retained."""

    if not ENABLE_PARAM_CONTROLS:
        return
    values = _params.current_display_values()
    result = client.publish(PARAM_STATE_TOPIC, json.dumps(values), retain=True)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        log(
            f"[PARAM] State publish was not queued (rc={result.rc})",
            level="warning",
        )


def publish_discovery() -> None:
    for key in sorted(SENSORS.keys()):
        publish_sensor_discovery(key)

    publish_manual_rpc_button_discovery()
    publish_manual_payload_text_discovery()
    publish_restart_dtu_button_discovery()
    publish_param_discovery()

    for group in get_grouped_sensor_keys():
        client.publish(availability_topic_for_group(group), "online", retain=True)
    _state.DISCOVERY_PUBLISHED = True
    log(f"[HA MQTT] Discovery published for {len(SENSORS)} sensors", level="info")


def publish_grouped_state(state_payload: Dict[str, object]) -> None:
    grouped_state: Dict[str, Dict[str, object]] = {}
    for key, value in state_payload.items():
        # Never publish keys without a matching discovery config -- they would
        # show up in HA as orphaned/unknown entities.
        if key not in ACTIVE_KEYS:
            continue
        group = get_sensor_group(key)
        grouped_state.setdefault(group, {})[key] = value

    for group, payload in grouped_state.items():
        result = client.publish(state_topic_for_group(group), json.dumps(payload), retain=MQTT_RETAIN)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            log(
                f"[HA MQTT ERROR] State publish for group={group} was not queued (rc={result.rc})",
                level="warning",
            )


def on_connect(_client, _userdata, _flags, rc, _properties=None):
    code = int(rc) if rc is not None else -1
    if code == 0:
        log(f"[HA MQTT] Connected to {MQTT_HOST}:{MQTT_PORT}", level="info")
        publish_discovery()
        client.subscribe(MANUAL_RPC_COMMAND_TOPIC, qos=1)
        client.subscribe(MANUAL_PAYLOAD_COMMAND_TOPIC, qos=1)
        if ENABLE_PARAM_CONTROLS:
            client.subscribe(RESTART_DTU_COMMAND_TOPIC, qos=1)
            client.subscribe(f"{PARAM_COMMAND_PREFIX}+", qos=1)
            if _params.current_display_values():
                publish_param_state()
        # Clear any retained payload on the command topics so a reconnect
        # does not replay a stale PRESS command as a fresh RPC send.
        client.publish(MANUAL_RPC_COMMAND_TOPIC, payload=b"", retain=True)
        client.publish(MANUAL_PAYLOAD_COMMAND_TOPIC, payload=b"", retain=True)
        if ENABLE_PARAM_CONTROLS:
            client.publish(RESTART_DTU_COMMAND_TOPIC, payload=b"", retain=True)
        if _state.has_any_state():
            publish_grouped_state(_state.get_state_snapshot())
    else:
        log(f"[HA MQTT ERROR] Connection failed with rc={code}", level="error")


def on_disconnect(_client, _userdata, rc, _properties=None):
    code = int(rc) if rc is not None else -1
    if code != 0 and _state.RUNNING_EVENT.is_set():
        log(f"[HA MQTT] Disconnected (rc={code}), retrying...", level="warning")


def _handle_restart_dtu_press() -> bool:
    """Route one "Restart DTU" button press to the active cloud path.

    Normal mode asks the real cloud (cloud_api.restart_dtu -- the portal's
    own /remote/dtu/restart call); isolated mode replays the captured
    c=26 dtu_rpc envelope on the local session
    (proxy_server.send_dtu_restart).  Either way the dongle then reboots
    and telemetry pauses for a minute or two; that is the intended effect,
    not an error.  Like cloud_api.send_param, the cloud call runs
    synchronously here.
    """

    # Lazy imports: proxy.py doesn't import this module, so this is safe at
    # call time (same pattern as on_message and params.handle_command).
    from .proxy import proxy_server

    if ISOLATE_CLOUD:
        sent = proxy_server.send_dtu_restart()
        route = "local isolated-cloud socket"
    else:
        from . import cloud_api

        sent, detail = cloud_api.restart_dtu()
        route = f"cloud API ({detail})"
    if sent:
        log(f"[RESTART] DTU restart sent via {route}; dongle will reboot", level="info")
    else:
        log(f"[RESTART] DTU restart was not sent via {route}", level="warning")
    return sent


def on_message(_client, _userdata, msg):
    # Lazy import: proxy.py doesn't import this module, so this is safe at
    # call time; keeping it out of the top-level import avoids any risk of
    # import-order issues between the two singletons (client / proxy_server).
    from .proxy import proxy_server

    # Drop retained command payloads: the broker redelivers them on every
    # reconnect, which can trigger a stale RPC send.  We clear the retained
    # payload and ignore the message.
    if getattr(msg, "retain", False):
        client.publish(msg.topic, payload=b"", retain=True)
        log(f"[HA MQTT] Dropped retained command payload on {msg.topic}", level="warning")
        return

    if ENABLE_PARAM_CONTROLS and msg.topic.startswith(PARAM_COMMAND_PREFIX):
        key = msg.topic[len(PARAM_COMMAND_PREFIX):]
        if not key or "/" in key or key not in _params.CATALOG:
            log(f"[PARAM] Ignored command for unknown parameter topic: {msg.topic}", level="warning")
            return

        # Parameter writes get their own throttle, independent of the
        # manual RPC button.  As there, a monotonic clock prevents NTP or
        # wall-clock changes from wedging the throttle.
        global _last_param_write_ts
        now = time.monotonic()
        if now - _last_param_write_ts < _PARAM_WRITE_MIN_INTERVAL:
            log(
                f"[PARAM] Command for {key} dropped: only "
                f"{now - _last_param_write_ts:.1f}s since last parameter "
                f"write (minimum {_PARAM_WRITE_MIN_INTERVAL}s)",
                level="warning",
            )
            return
        _last_param_write_ts = now

        payload_text = msg.payload.decode("utf-8", errors="ignore").strip()
        if not payload_text:
            log(f"[PARAM] Ignored empty command for {key}", level="warning")
            return
        # params.handle_command validates/encodes, registers the expected
        # telemetry value, and routes the write.  HA state is deliberately
        # not updated here; only a later confirmed frame may change it.
        _params.handle_command(key, payload_text)
        return

    if ENABLE_PARAM_CONTROLS and msg.topic == RESTART_DTU_COMMAND_TOPIC:
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        if payload != RESTART_DTU_PRESS_PAYLOAD:
            return
        # Own throttle, longer than the other buttons': a restart knocks
        # the dongle offline for a minute or two, so this guards against
        # an accidental double-tap restarting it twice in a row.
        global _last_restart_ts
        now = time.monotonic()
        if now - _last_restart_ts < _RESTART_MIN_INTERVAL:
            log(
                f"[RESTART] DTU restart dropped: only {now - _last_restart_ts:.1f}s "
                f"since last restart (minimum {_RESTART_MIN_INTERVAL}s)",
                level="warning",
            )
            return
        _last_restart_ts = now
        _handle_restart_dtu_press()
        return

    if msg.topic == MANUAL_RPC_COMMAND_TOPIC:
        payload = msg.payload.decode("utf-8", errors="ignore").strip()
        if payload != MANUAL_RPC_PRESS_PAYLOAD:
            return
        # Throttle: drop manual RPC sends that arrive faster than the minimum
        # interval.  This prevents a retained-message replay storm (the broker
        # redelivers retained messages on every reconnect) and protects the
        # embedded dongle from being flooded.
        global _last_manual_rpc_ts
        # Monotonic clock: immune to wall-clock adjustments (NTP correction,
        # manual time change) that could otherwise wedge the throttle open or
        # shut on a host with intermittent time sync.
        now = time.monotonic()
        if now - _last_manual_rpc_ts < _MANUAL_RPC_MIN_INTERVAL:
            log(
                f"[HA MQTT] Manual RPC dropped: only {now - _last_manual_rpc_ts:.1f}s "
                f"since last send (minimum {_MANUAL_RPC_MIN_INTERVAL}s)",
                level="warning",
            )
            return
        _last_manual_rpc_ts = now
        sent = proxy_server.send_manual_rpc()
        if sent:
            log("[HA MQTT] Manual RPC button pressed — request sent to inverter", level="info")
        else:
            log("[HA MQTT] Manual RPC button pressed but inverter is not connected", level="warning")

    elif msg.topic == MANUAL_PAYLOAD_COMMAND_TOPIC:
        raw_text = msg.payload.decode("utf-8", errors="ignore")
        if not raw_text.strip():
            return
        sent = proxy_server.send_manual_payload(raw_text)
        if not sent:
            log("[HA MQTT] Manual payload entered but inverter is not connected", level="warning")


client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message


def start_mqtt() -> None:
    try:
        client.connect_async(MQTT_HOST, MQTT_PORT, 60)
        client.loop_start()
    except Exception as exc:
        log(f"[HA MQTT ERROR] {exc}", level="error")


