"""Home Assistant controls for inverter configuration parameters.

The parameters in this catalog occupy the settings tail of the 86-byte
``VPHBQ`` telemetry frame.  The frame byte is NOT uniformly ``addr + 50``:
two non-parameter bytes are interleaved in the tail -- byte 71 (a live
value, observed 0-9, possibly battery discharge current) sits between the
baud rate (byte 70) and Inverter Output Mode (byte 72), and byte 76
(constant 3) sits between Battery Type (byte 75) and the BMS switch
(byte 77).  ``FRAME_BYTE_BY_ADDR`` below is the verified layout from the
live parameter map; CSPA write addresses are a separate, gapless space.

Numeric parameters whose wire encoding is the displayed value (or the
displayed value times ten) are exposed, plus every portal dropdown as a
select.  The select option codes are the manufacturer's own, taken from
the portal's official attribute definitions (``enumValues`` of
``/device/query/attribute/group``) and cross-checked against the live
values in the telemetry tail.  The communication-port baud-rate parameter
is deliberately absent: changing it can break the serial link between the
DTU and the inverter, so it must never become a writable Home Assistant
control.  The portal's "restore control parameters to default" action is
likewise never exposed.
"""

import math
import threading
import time
from typing import Dict, List, Optional, Union

from . import config
from .loggers import log


# Verified frame byte for each CSPA address in the VPHBQ settings tail
# (bytes 50-83).  See the module docstring for the two interleaved
# non-parameter bytes (71 and 76).
FRAME_BYTE_BY_ADDR: Dict[int, int] = {
    0x00: 50, 0x01: 51, 0x02: 52, 0x03: 53, 0x04: 54, 0x05: 55,
    0x06: 56, 0x07: 57, 0x08: 58, 0x09: 59, 0x0A: 60, 0x0B: 61,
    0x0C: 62, 0x0D: 63, 0x0E: 64, 0x0F: 65, 0x10: 66, 0x11: 67,
    0x12: 68, 0x13: 69, 0x15: 72, 0x16: 73, 0x17: 74, 0x18: 75,
    0x19: 77, 0x1A: 78, 0x1B: 79, 0x1C: 80, 0x1D: 81, 0x1E: 82,
    0x1F: 83,
}
VERIFICATION_TIMEOUT_SEC = 150.0

ParamValue = Union[int, float, str, None]


def _number(
    key: str,
    name: str,
    addr: int,
    unit: str,
    scale: int,
    minimum: Union[int, float],
    maximum: Union[int, float],
    step: Union[int, float],
) -> Dict[str, object]:
    return {
        "key": key,
        "name": name,
        "addr": addr,
        "kind": "number",
        "scale": scale,
        "min": minimum,
        "max": maximum,
        "step": step,
        "unit": unit,
    }


def _select(
    key: str,
    name: str,
    addr: int,
    options: Dict[str, int],
) -> Dict[str, object]:
    return {
        "key": key,
        "name": name,
        "addr": addr,
        "kind": "select",
        "options": options,
    }


CATALOG: Dict[str, Dict[str, object]] = {
    "input_range": _select(
        "input_range", "Rango de entrada", 0x00, {"UPS": 1, "APL": 2}
    ),
    "input_frequency_range": _select(
        "input_frequency_range",
        "Rango de frecuencia de entrada",
        0x01,
        {"LO": 1, "HI": 2},
    ),
    "work_mode": _select(
        "work_mode", "Modo de trabajo", 0x02, {"UTI": 1, "SBU": 2, "SOL": 3}
    ),
    "machine_charging_mode": _select(
        "machine_charging_mode",
        "Modo de carga",
        0x03,
        {"CUT": 1, "CSO": 2, "OSO": 3},
    ),
    "mains_charging_current": _number(
        "mains_charging_current",
        "Corriente de carga de red",
        0x04,
        "%",
        1,
        0,
        100,
        1,
    ),
    "solar_charging_current": _number(
        "solar_charging_current",
        "Corriente de carga solar",
        0x05,
        "%",
        1,
        0,
        100,
        1,
    ),
    "strong_charging_voltage": _number(
        "strong_charging_voltage",
        "Voltaje de carga fuerte (por batería)",
        0x06,
        "V",
        10,
        10.0,
        16.0,
        0.1,
    ),
    "floating_voltage": _number(
        "floating_voltage",
        "Voltaje de flotación (por batería)",
        0x07,
        "V",
        10,
        10.0,
        16.0,
        0.1,
    ),
    "locking_voltage": _number(
        "locking_voltage",
        "Voltaje de bloqueo (por batería)",
        0x08,
        "V",
        10,
        9.0,
        14.0,
        0.1,
    ),
    "return_mains_voltage": _number(
        "return_mains_voltage",
        "Voltaje de retorno a red (por batería)",
        0x09,
        "V",
        10,
        10.0,
        15.0,
        0.1,
    ),
    "return_inverter_voltage": _number(
        "return_inverter_voltage",
        "Voltaje de retorno a inversor (por batería)",
        0x0A,
        "V",
        10,
        11.0,
        16.0,
        0.1,
    ),
    "output_voltage_220": _number(
        "output_voltage_220",
        "Voltaje de salida 220V",
        0x0B,
        "V",
        1,
        200,
        260,
        1,
    ),
    "power_on_battery_voltage": _number(
        "power_on_battery_voltage",
        "Voltaje de encendido por batería",
        0x16,
        "V",
        10,
        10.0,
        14.0,
        0.1,
    ),
    "low_power_restores_voltage": _number(
        "low_power_restores_voltage",
        "Voltaje de restauración por baja potencia",
        0x17,
        "V",
        10,
        11.0,
        16.0,
        0.1,
    ),
    "low_electric_lock_soc": _number(
        "low_electric_lock_soc",
        "SOC de bloqueo por baja carga",
        0x1A,
        "%",
        1,
        0,
        100,
        1,
    ),
    "return_mains_soc": _number(
        "return_mains_soc",
        "SOC de retorno a red",
        0x1B,
        "%",
        1,
        0,
        100,
        1,
    ),
    "return_inverter_soc": _number(
        "return_inverter_soc",
        "SOC de retorno a inversor",
        0x1C,
        "%",
        1,
        0,
        100,
        1,
    ),
    "low_power_recovery_soc": _number(
        "low_power_recovery_soc",
        "SOC de recuperación por baja potencia",
        0x1D,
        "%",
        1,
        0,
        100,
        1,
    ),
    "low_voltage_alarm": _number(
        "low_voltage_alarm",
        "Voltaje de alarma por bajo voltaje",
        0x1E,
        "V",
        10,
        10.0,
        14.0,
        0.1,
    ),
    "mains_sensitivity": _select(
        "mains_sensitivity",
        "Sensibilidad de red",
        0x0C,
        {"HI": 1, "IDE": 2, "LO": 3},
    ),
    "output_frequency": _select(
        "output_frequency", "Frecuencia de salida", 0x0D, {"50": 1, "60": 2}
    ),
    "fault_restart": _select(
        "fault_restart",
        "Reinicio ante fallas",
        0x0E,
        {"Allow": 1, "Not Allow": 2},
    ),
    "backlight_mode": {
        "key": "backlight_mode",
        "name": "Modo de luz de fondo",
        "addr": 0x0F,
        "kind": "select",
        # LOF=2 was confirmed in the portal's official enum definitions
        # (2026-10-23); LON/LOD were additionally verified live.
        "options": {"LON": 1, "LOF": 2, "LOD": 3},
    },
    "buzzer_mode": _select(
        "buzzer_mode", "Modo de zumbador", 0x10, {"OFF": 2, "ON": 1}
    ),
    "battery_capacity_limit": _select(
        "battery_capacity_limit",
        "Límite de capacidad de batería",
        0x11,
        {"OFF": 1, "ON": 2},
    ),
    "load_limiting_function": _select(
        "load_limiting_function",
        "Función de limitación de carga",
        0x12,
        {"OFF": 1, "ON": 2},
    ),
    "load_offline_prompt": _select(
        "load_offline_prompt",
        "Aviso de carga desconectada",
        0x13,
        {"OFF": 1, "ON": 2},
    ),
    "inverter_output_mode": _select(
        "inverter_output_mode",
        "Modo de salida del inversor",
        0x15,
        {"220V": 0, "110V": 1},
    ),
    "battery_type": _select(
        "battery_type",
        "Tipo de batería",
        0x18,
        {"SEL": 1, "GEL": 2, "FLD": 3, "USE": 4, "TER": 5, "LIF": 6},
    ),
    "bms_communication_switch": _select(
        "bms_communication_switch",
        "Interruptor de comunicación BMS",
        0x19,
        {"OFF": 1, "ON": 2},
    ),
    "bms_communication_protocol": _select(
        "bms_communication_protocol",
        "Protocolo de comunicación BMS",
        0x1F,
        {"Auto": 0, "VOL": 1, "PYLON": 2, "GROWATT": 3},
    ),
}


_lock = threading.RLock()
_current_raw: Dict[int, int] = {}
_current_display: Dict[str, object] = {}
_pending: Dict[int, Dict[str, object]] = {}
_verification_results: Dict[int, str] = {}


def _resolve_param(param: Union[str, Dict[str, object]]) -> Dict[str, object]:
    if isinstance(param, str):
        try:
            return CATALOG[param]
        except KeyError as exc:
            raise ValueError(f"unknown parameter: {param!r}") from exc
    if isinstance(param, dict) and "kind" in param and "addr" in param:
        return param
    raise ValueError("param must be a catalog key or parameter dict")


def _validate_byte(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer byte")
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{field} must be between 0 and 255")
    return value


def _coerce_display_number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("numeric parameter value must not be boolean")
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict").strip()
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError("numeric parameter value is empty")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric parameter value: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError("numeric parameter value must be finite")
    return number


def build_cspa_ci(addr: int, raw: int) -> bytes:
    """Build the verified 15-byte CSPA/CSIPAM write payload.

    Layout: ``CSPA`` + length byte 0x0A + ``CSIPAM`` + two-byte address
    (catalog addresses are one byte, so the second byte is 0x00) + one-byte
    value + checksum.  The checksum is the low byte of the sum of the three
    bytes immediately before it (address low, address high, value).
    """

    addr = _validate_byte(addr, "addr")
    raw = _validate_byte(raw, "raw")
    payload_tail = bytes((addr, 0x00, raw))
    checksum = sum(payload_tail) & 0xFF
    return b"CSPA" + bytes((0x0A,)) + b"CSIPAM" + payload_tail + bytes((checksum,))


def encode_value(param: Union[str, Dict[str, object]], display: object) -> int:
    """Encode a Home Assistant display value to its one-byte wire value."""

    meta = _resolve_param(param)
    kind = meta["kind"]

    if kind == "select":
        options = meta["options"]  # type: ignore[assignment]
        if isinstance(display, str):
            label = display.strip()
            if label in options:  # type: ignore[operator]
                return int(options[label])  # type: ignore[index]
        if isinstance(display, int) and not isinstance(display, bool):
            if display in options.values():  # type: ignore[attr-defined]
                return display
        raise ValueError(
            f"invalid option for {meta['key']}: {display!r}; "
            f"expected one of {sorted(options)}"  # type: ignore[arg-type]
        )

    if kind != "number":
        raise ValueError(f"unsupported parameter kind: {kind!r}")

    value = _coerce_display_number(display)
    minimum = float(meta["min"])  # type: ignore[arg-type]
    maximum = float(meta["max"])  # type: ignore[arg-type]
    if value < minimum or value > maximum:
        raise ValueError(
            f"value {value} for {meta['key']} is outside the allowed "
            f"range {minimum}..{maximum}"
        )
    scale = int(meta["scale"])  # type: ignore[arg-type]
    return _validate_byte(int(round(value * scale)), "raw")


def decode_value(param: Union[str, Dict[str, object]], raw: int) -> ParamValue:
    """Decode a one-byte wire value to its Home Assistant display value.

    An unrecognized select code returns ``None`` rather than inventing a
    label; callers omit that parameter from published state.
    """

    meta = _resolve_param(param)
    raw = _validate_byte(raw, "raw")
    if meta["kind"] == "select":
        for label, code in meta["options"].items():  # type: ignore[attr-defined]
            if code == raw:
                return label
        return None
    if meta["kind"] != "number":
        raise ValueError(f"unsupported parameter kind: {meta['kind']!r}")
    scale = int(meta["scale"])  # type: ignore[arg-type]
    if scale == 1:
        return raw
    return round(raw / scale, 1)


def expect(addr: int, raw: int) -> None:
    """Register that a future frame should show ``raw`` at ``addr``."""

    addr = _validate_byte(addr, "addr")
    raw = _validate_byte(raw, "raw")
    with _lock:
        _pending[addr] = {
            "raw": raw,
            "deadline": time.monotonic() + VERIFICATION_TIMEOUT_SEC,
        }
        _verification_results.pop(addr, None)


def cancel_expectation(addr: int) -> None:
    with _lock:
        _pending.pop(addr, None)


def _resolve_pending_locked(now: Optional[float] = None) -> None:
    if now is None:
        now = time.monotonic()
    for addr, pending in list(_pending.items()):
        observed = _current_raw.get(addr)
        if observed == pending["raw"]:
            _verification_results[addr] = "confirmed"
            del _pending[addr]
            log(f"[PARAM] Verification confirmed for addr=0x{addr:02x}", level="info")
        elif now >= float(pending["deadline"]):
            _verification_results[addr] = "failed"
            del _pending[addr]
            log(
                f"[PARAM] Verification failed for addr=0x{addr:02x}: "
                f"expected raw={pending['raw']}, observed raw={observed}",
                level="warning",
            )


def verification_status(addr: int) -> Optional[str]:
    """Return pending/confirmed/failed for an address, if known."""

    addr = _validate_byte(addr, "addr")
    with _lock:
        _resolve_pending_locked()
        if addr in _pending:
            return "pending"
        return _verification_results.get(addr)


# A descriptive alias for callers that prefer the registry terminology.
pending_status = verification_status


def note_frame(frame: bytes) -> List[str]:
    """Update the parameter store from a VPHBQ telemetry frame.

    Returns the catalog keys whose raw values changed in this frame.  The
    function is deliberately conservative: malformed or short frames are
    ignored, and unknown select codes remove that key from display state
    instead of producing an invented label.
    """

    try:
        frame_bytes = bytes(frame)
    except Exception:
        return []
    if len(frame_bytes) < 84 or not frame_bytes.startswith(b"VPHBQ"):
        return []

    changed: List[str] = []
    with _lock:
        for key, meta in CATALOG.items():
            addr = int(meta["addr"])  # type: ignore[arg-type]
            raw = frame_bytes[FRAME_BYTE_BY_ADDR[addr]]
            if _current_raw.get(addr) != raw:
                changed.append(key)
                _current_raw[addr] = raw
                decoded = decode_value(meta, raw)
                if decoded is None:
                    _current_display.pop(key, None)
                else:
                    _current_display[key] = decoded
        _resolve_pending_locked()
    return changed


def current_display_values() -> Dict[str, object]:
    """Return a copy of the currently known display values."""

    with _lock:
        _resolve_pending_locked()
        return dict(_current_display)


def current_raw_values() -> Dict[int, int]:
    """Return a copy of the raw values by CSPA address (diagnostic aid)."""

    with _lock:
        return dict(_current_raw)


def clear_state() -> None:
    """Clear all observed and pending parameter state (used by tests)."""

    with _lock:
        _current_raw.clear()
        _current_display.clear()
        _pending.clear()
        _verification_results.clear()


def handle_command(key: str, payload_text: object) -> bool:
    """Handle one Home Assistant parameter command.

    The command is validated and its expected raw value is registered
    before routing.  No optimistic state is published here: Home Assistant
    state changes only when a later telemetry frame confirms the value via
    :func:`note_frame`.
    """

    if not config.ENABLE_PARAM_CONTROLS:
        log("[PARAM] Parameter controls are disabled", level="warning")
        return False

    meta = CATALOG.get(key) if isinstance(key, str) else None
    if meta is None:
        log(f"[PARAM] Unknown parameter command key: {key!r}", level="warning")
        return False

    try:
        raw = encode_value(meta, payload_text)
        display_value = decode_value(meta, raw)
    except (TypeError, ValueError) as exc:
        log(f"[PARAM] Rejected command for {key}: {exc}", level="warning")
        return False

    addr = int(meta["addr"])  # type: ignore[arg-type]
    expect(addr, raw)

    try:
        if config.ISOLATE_CLOUD:
            # Lazy import: proxy owns the live dongle socket and importing it
            # at module load would create an import cycle.
            from .proxy import proxy_server

            sent = proxy_server.send_param_write(addr, raw)
            route = "local isolated-cloud socket"
        else:
            # Lazy import keeps the cloud path optional and cycle-free.
            from . import cloud_api

            sent = cloud_api.send_param(dict(meta), display_value)
            route = "cloud API"
    except Exception as exc:
        sent = False
        route = "local isolated-cloud socket" if config.ISOLATE_CLOUD else "cloud API"
        log(f"[PARAM] Send for {key} via {route} raised: {exc}", level="error")

    if not sent:
        cancel_expectation(addr)
        log(
            f"[PARAM] Command for {key} was not sent via {route} "
            f"(addr=0x{addr:02x}, raw={raw})",
            level="warning",
        )
        return False

    log(
        f"[PARAM] Command sent for {key} via {route} "
        f"(addr=0x{addr:02x}, raw={raw}); awaiting telemetry verification",
        level="info",
    )
    return True
