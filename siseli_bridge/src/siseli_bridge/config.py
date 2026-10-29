import ipaddress
import re
import os

STATE_CACHE_FILE = "/data/state.json"

INVERTER_IP = os.getenv("INVERTER_IP", "192.168.1.139")
ROUTER_IP = os.getenv("ROUTER_IP", "192.168.1.1")

TARGET_HOST = os.getenv("TARGET_HOST", "8.212.18.157")
TARGET_PORT = int(os.getenv("TARGET_PORT", "1883"))
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "18899"))

# The dongle also talks HTTP to a second cloud host (observed as
# dtu.access.solar.siseli.com / 8.212.16.60:80) -- this is where the real
# telemetry block (cmdOut on POST /dtu/devices/findSingle) is delivered, not
# just config-write commands as originally assumed. Must be 80 to match what
# the dongle expects to connect to unless the container can bind privileged
# ports another way.
HTTP_LISTEN_PORT = int(os.getenv("HTTP_LISTEN_PORT", "80"))

# Controls the "performanceReportPeriod" (in ms) returned in the /dtu/checkin
# response. This is the cadence the real cloud uses to tell the dongle how
# often to emit its autonomous dev_event_post telemetry (captured value:
# 180000 ms = 3 min). NOTE: confirmed on firmware v1.44.5 the dongle IGNORES
# this override -- dev_event_post stays at ~60s -- AND /dtu/checkin only fires
# on a cold boot of the dongle, not on MQTT reconnect, so the value often never
# even gets sent. Kept configurable in case a future firmware honors it. The
# real frequency lever for telemetry is UPDATE_INTERVAL_SEC (the RPC poll).
# Set HTTP_REPORT_PERIOD_SEC in SECONDS; it is converted to ms. When
# ENABLE_HTTP_REPORT_PERIOD is false (or the value is 0), the field is omitted.
ENABLE_HTTP_REPORT_PERIOD = os.getenv("ENABLE_HTTP_REPORT_PERIOD", "true").strip().lower() in {"1", "true", "yes", "on"}
HTTP_REPORT_PERIOD_SEC = int(os.getenv("HTTP_REPORT_PERIOD_SEC", "60"))

AUTO_INTERCEPT = os.getenv("AUTO_INTERCEPT", "true").strip().lower() in {"1", "true", "yes", "on"}
INVERTER_MAC_CFG = os.getenv("INVERTER_MAC", "").strip().lower() or None
ROUTER_MAC_CFG = os.getenv("ROUTER_MAC", "").strip().lower() or None
ISOLATE_CLOUD = os.getenv("ISOLATE_CLOUD", "true").strip().lower() in {"1", "true", "yes", "on"}

# When Isolated Cloud is newly activated (previous run had it off), the LAN
# modem must be rebooted within seconds so the dongle lands on the local fake
# cloud -- see modem_reboot.py. Disabled unless explicitly enabled and a
# modem password is configured.
MODEM_REBOOT_ON_ISOLATE = os.getenv("MODEM_REBOOT_ON_ISOLATE", "false").strip().lower() in {"1", "true", "yes", "on"}
MODEM_USER = os.getenv("MODEM_USER", "admin").strip()
MODEM_PASSWORD = os.getenv("MODEM_PASSWORD", "")
MODEM_REBOOT_DELAY_SEC = int(os.getenv("MODEM_REBOOT_DELAY_SEC", "2"))

MQTT_HOST = os.getenv("MQTT_HOST", "core-mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "").strip()
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

MQTT_DISCOVERY_PREFIX = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
DEVICE_ID = os.getenv("DEVICE_ID", "siseli_inverter_1")
DEVICE_NAME = os.getenv("DEVICE_NAME", "Siseli Inverter 1")
MODEL_NAME = os.getenv("MODEL_NAME", DEVICE_NAME)
MANUFACTURER = os.getenv("MANUFACTURER", "Siseli Compatible")
ENTITY_PREFIX = os.getenv("ENTITY_PREFIX", "").strip()
INVERTER_COUNT = int(os.getenv("INVERTER_COUNT", "1"))
BATTERY_COUNT = int(os.getenv("BATTERY_COUNT", "1"))
BATTERY_CAPACITY_PER_BATTERY_AH = float(os.getenv("BATTERY_CAPACITY_PER_BATTERY_AH", "0.0"))
RATED_OUTPUT_POWER_W = int(os.getenv("RATED_OUTPUT_POWER_W", "5000"))

STATE_TOPIC = os.getenv("STATE_TOPIC", f"siseli/{DEVICE_ID}/state")
AVAILABILITY_TOPIC = os.getenv("AVAILABILITY_TOPIC", f"siseli/{DEVICE_ID}/availability")

SNIFF_IFACE = os.getenv("SNIFF_IFACE", "").strip() or None

UPDATE_INTERVAL_SEC = int(os.getenv("UPDATE_INTERVAL_SEC", "10"))
# When false, the background poller thread never starts -- useful to observe
# whether the inverter sends dev_rpc traffic on its own, without the bridge
# nudging it. Manual RPC sends (via the "Enviar RPC Manual" HA button) still
# work regardless of this flag, since they go through send_manual_rpc()
# directly rather than the poller loop.
ENABLE_RPC_POLLING = os.getenv("ENABLE_RPC_POLLING", "true").strip().lower() in {"1", "true", "yes", "on"}
# Live polling (v2.5.42): in isolated mode, ask the inverter for a fresh
# frame every LIVE_POLL_INTERVAL_SEC via the genuine CMSG/INV-HB command
# (i=503) instead of relying on the DTU's cached batch-read replies. The
# command is a read-only heartbeat request, captured from the real cloud
# on 2026-10-23; the DTU answers it in ~1s with the full 86-byte frame.
ENABLE_LIVE_POLLING = os.getenv("ENABLE_LIVE_POLLING", "false").strip().lower() in {"1", "true", "yes", "on"}
LIVE_POLL_INTERVAL_SEC = max(2, int(os.getenv("LIVE_POLL_INTERVAL_SEC", "3")))
# When true, the FIRST RPC telemetry poll is deferred until
# RPC_FIRST_POLL_DELAY_SEC seconds after the inverter's first PUBLISH on this
# connection. After that, subsequent polls follow UPDATE_INTERVAL_SEC. When
# false, first poll is one UPDATE_INTERVAL_SEC after the poller starts. The
# 'ci' keepalive is not affected by this delay either way.
ENABLE_FIRST_POLL_DELAY = os.getenv("ENABLE_FIRST_POLL_DELAY", "true").strip().lower() in {"1", "true", "yes", "on"}
RPC_FIRST_POLL_DELAY_SEC = int(os.getenv("RPC_FIRST_POLL_DELAY_SEC", "30"))
# Application-level ack the real cloud sends after every pub/event/* publish
# ({"c":<same as request>,"t":<echoed>,"s":<fresh>,"i":<request i + 1>,"e":0}).
# Kept configurable because its necessity is unproven: telemetry and the RPC
# poll both worked before it existed and while it was malformed (i=0).  Turn
# off to test whether the device still answers dev_rpc without it.
ENABLE_APP_ACK = os.getenv("ENABLE_APP_ACK", "true").strip().lower() in {"1", "true", "yes", "on"}
# Phase-1 Home Assistant parameter controls (v2.6.0).  The portal settings
# are credentials for the future non-isolated cloud-API write path; the
# isolated path uses the local CSPA socket and does not need them.
ENABLE_PARAM_CONTROLS = os.getenv("ENABLE_PARAM_CONTROLS", "true").strip().lower() in {"1", "true", "yes", "on"}
SISELI_PORTAL_USER = os.getenv("SISELI_PORTAL_USER", "").strip()
SISELI_PORTAL_PASSWORD = os.getenv("SISELI_PORTAL_PASSWORD", "")
# Serial printed on the DTU (dongle) -- also its MQTT device_id, i.e. the
# <id> in every dtu/<id>/... topic the bridge sees.  cloud_api matches
# /device/list records against it when discovering the portal dtuId.
SISELI_DTU_SERIAL = os.getenv("SISELI_DTU_SERIAL", "")
SISELI_PORTAL_DEVICE_ID = os.getenv("SISELI_PORTAL_DEVICE_ID", "")
SISELI_PORTAL_STATION_ID = os.getenv("SISELI_PORTAL_STATION_ID", "")
# Portal identifier of the DTU (dongle) itself, sent as the ``dtuId`` query
# parameter of POST /remote/dtu/restart -- the cloud route of the Home
# Assistant "Restart DTU" button (see cloud_api.restart_dtu).
# TODO(verify at deploy): the portal's internal numeric dtuId is not
# recorded in any saved file, so the default is SISELI_DTU_SERIAL, which
# is the platform's user-facing "DtuID" (the portal's swap-DTU screen
# asks for exactly that value typed in).  If the portal rejects it (code
# 20101, observed live 2026-10-24), cloud_api discovers the real id from
# /device/list by matching SISELI_DTU_SERIAL and logs it as "[RESTART]
# discovered dtuId=..." -- set SISELI_DTU_ID to that value to skip the
# discovery; no code change needed.
SISELI_DTU_ID = os.getenv("SISELI_DTU_ID", SISELI_DTU_SERIAL).strip()
# Captured traffic shows every application payload -- in BOTH directions --
# prefixed with a single 0x00 byte before the opening brace.  Configurable in
# case that turns out to be a capture artifact rather than protocol.
APP_PAYLOAD_NUL_PREFIX = os.getenv("APP_PAYLOAD_NUL_PREFIX", "true").strip().lower() in {"1", "true", "yes", "on"}
MQTT_RETAIN = os.getenv("MQTT_RETAIN", "true").strip().lower() in {"1", "true", "yes", "on"}
PUBLISH_ON_EVERY_TELEMETRY = os.getenv("PUBLISH_ON_EVERY_TELEMETRY", "true").strip().lower() in {"1", "true", "yes", "on"}
RPC_TEMPLATE_FILE = os.getenv("RPC_TEMPLATE_FILE", "/data/siseli_rpc_template.json").strip()
# Separate template for the "ci"-style dev_rpc downlinks (capability/keepalive
# handshake, e.g. base64 "CSPA..." payloads), distinct from the "ct"
# telemetry batch-read template above.
CI_RPC_TEMPLATE_FILE = os.getenv("CI_RPC_TEMPLATE_FILE", "/data/siseli_ci_template.json").strip()
LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "info").strip().lower()

if LOG_LEVEL_STR == "debug":
    LOG_VERBOSE = True
    LOG_BLOCKS = True
    LOG_STATE_DIFF = True
    LOG_STATE_SNAPSHOT = True
    LOG_RAW_JSON = True
    LOG_CLEAN_STATE = True
    LOG_MQTT_TOPICS = True
    LOG_MQTT_PAYLOAD_PREVIEW = True
    LOG_UNPARSED_PUBLISH = True
    LOG_STREAM_EVENTS = True
    LOG_NULL_TARGETS = True
elif LOG_LEVEL_STR in {"warning", "error"}:
    LOG_VERBOSE = False
    LOG_BLOCKS = False
    LOG_STATE_DIFF = False
    LOG_STATE_SNAPSHOT = False
    LOG_RAW_JSON = False
    LOG_CLEAN_STATE = False
    LOG_MQTT_TOPICS = False
    LOG_MQTT_PAYLOAD_PREVIEW = False
    LOG_UNPARSED_PUBLISH = False
    LOG_STREAM_EVENTS = False
    LOG_NULL_TARGETS = False
else:
    LOG_VERBOSE = os.getenv("LOG_VERBOSE", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_BLOCKS = os.getenv("LOG_BLOCKS", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_STATE_DIFF = os.getenv("LOG_STATE_DIFF", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_STATE_SNAPSHOT = os.getenv("LOG_STATE_SNAPSHOT", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_RAW_JSON = os.getenv("LOG_RAW_JSON", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_CLEAN_STATE = os.getenv("LOG_CLEAN_STATE", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_MQTT_TOPICS = os.getenv("LOG_MQTT_TOPICS", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_MQTT_PAYLOAD_PREVIEW = os.getenv("LOG_MQTT_PAYLOAD_PREVIEW", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_UNPARSED_PUBLISH = os.getenv("LOG_UNPARSED_PUBLISH", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_STREAM_EVENTS = os.getenv("LOG_STREAM_EVENTS", "false").strip().lower() in {"1", "true", "yes", "on"}
    LOG_NULL_TARGETS = os.getenv("LOG_NULL_TARGETS", "false").strip().lower() in {"1", "true", "yes", "on"}



STRICT_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
PRINTABLE_ASCII_RE = re.compile(r"^[ -~]+$")
SLUG_RE = re.compile(r"[^a-z0-9]+")

MAX_MQTT_PACKET = 1024 * 64
STREAM_STALE_SECONDS = 30
MAX_STREAM_BUFFER = 1024 * 256


def validate_config() -> None:
    """Validate critical configuration at startup. Calls sys.exit on fatal errors."""
    import sys

    errors: list = []

    for name, val in [("INVERTER_IP", INVERTER_IP), ("ROUTER_IP", ROUTER_IP)]:
        try:
            ipaddress.ip_address(val)
        except ValueError:
            errors.append(f"{name} is not a valid IP address: {val!r}")

    for name, val in [
        ("TARGET_PORT", TARGET_PORT),
        ("MQTT_PORT", MQTT_PORT),
        ("LISTEN_PORT", LISTEN_PORT),
        ("HTTP_LISTEN_PORT", HTTP_LISTEN_PORT),
    ]:
        if not (1 <= val <= 65535):
            errors.append(f"{name} must be 1-65535, got {val}")

    if UPDATE_INTERVAL_SEC < 1:
        errors.append(f"UPDATE_INTERVAL_SEC must be >= 1, got {UPDATE_INTERVAL_SEC}")

    if not MQTT_HOST.strip():
        errors.append("MQTT_HOST must not be empty")

    if not TARGET_HOST.strip():
        errors.append("TARGET_HOST must not be empty")

    if INVERTER_COUNT < 1:
        errors.append(f"INVERTER_COUNT must be >= 1, got {INVERTER_COUNT}")
    if BATTERY_COUNT < 1:
        errors.append(f"BATTERY_COUNT must be >= 1, got {BATTERY_COUNT}")
    if BATTERY_CAPACITY_PER_BATTERY_AH < 0:
        errors.append(
            "BATTERY_CAPACITY_PER_BATTERY_AH must be >= 0, "
            f"got {BATTERY_CAPACITY_PER_BATTERY_AH}"
        )
    if RATED_OUTPUT_POWER_W < 1:
        errors.append(f"RATED_OUTPUT_POWER_W must be >= 1, got {RATED_OUTPUT_POWER_W}")

    data_dir = os.path.dirname(STATE_CACHE_FILE)
    if data_dir:
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError as exc:
            print(
                f"[CONFIG WARNING] Cannot create state cache directory {data_dir!r}: {exc}",
                flush=True,
            )

    if errors:
        for err in errors:
            print(f"[CONFIG ERROR] {err}", flush=True)
        sys.exit(f"[Config] Aborting: {len(errors)} configuration error(s) found.")
