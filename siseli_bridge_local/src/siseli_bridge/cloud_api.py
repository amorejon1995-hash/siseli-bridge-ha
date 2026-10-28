"""Cloud-API parameter writes for non-isolated operation.

When ``ISOLATE_CLOUD`` is false the DTU talks to the real Siseli cloud, so
a Home Assistant parameter command cannot be injected into a local socket.
This module instead reproduces the portal's own control request, verified
live against ``https://solar.siseli.com/apis`` on 2026-10-23:

* ``POST /login/account`` with ``{account, password: md5(password),
  type: "account"}`` returns an access token (the password itself is the
  add-on's ``SISELI_PORTAL_PASSWORD`` option, sent only as its MD5 hash,
  exactly like the portal's own login form);
* ``POST /remote/device/configs/cache/get?deviceId=<id>`` with body ``{}``
  returns the full attribute objects for the device;
* ``POST /remote/device/config/write?deviceId=<id>`` with one attribute
  object (its ``value``/``valueDisplay`` set to the target) performs the
  send; the cloud then delivers the usual CSPA command to the DTU.

Every request is signed exactly like the portal's own web client (the
login endpoint rejects unsigned calls with code 36 "IOT-Open-AppID
missing"; verified live): an ``IOT-Open-Sign`` header computed as
MD5(HMAC-SHA256(base64(sorted params))) where the params are the query
pairs plus the SHA-256 body hash, the AppID and a random nonce, and the
HMAC key is derived from the app secret published in the portal's own
JavaScript bundle (AES-128-CBC decryption keyed by the AppID's MD5).
Tokens are cached in ``/data`` so restarts do not force a fresh login, a
lock serializes login/send so near-simultaneous commands cannot start
competing sessions, and an authentication failure is retried once after a
fresh login.  Credentials and tokens are never logged.
"""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterator, List, Optional, Tuple

from . import config
from .loggers import log

API_BASE = "https://solar.siseli.com/apis"
# Portal device id of the 'Inverter' in Alfredo's 'Inverter Plant'
# (distinct from the DTU serial 58186761002157416389).
PORTAL_DEVICE_ID = "484921707297996801"
# Portal id of the station ('Inverter Plant') that owns the devices; used
# only as the first filter tried by the /device/list discovery below.
PORTAL_STATION_ID = "484921707272830977"
# Portal code for "illegal argument".  The restart endpoint answers it
# when the dtuId is not one it recognizes (observed live on 2026-10-24
# with the DTU serial).  It is not an authentication failure, so a fresh
# login cannot fix it -- only the real id can.
RESTART_ILLEGAL_ARGUMENT_CODE = 20101
# Keys a /device/list record may carry the device serial under, in probe
# order, and descriptive fields scanned when preferring one candidate
# record over another (a DTU/gateway/collector mention wins).
_DTU_SERIAL_KEYS = ("sn", "serial", "serialNo", "dtuSn", "deviceSn", "code")
_DTU_TYPE_KEYS = (
    "deviceType", "type", "model", "deviceModel", "modelName",
    "name", "deviceName", "productName", "category", "categoryName",
)
_DTU_TYPE_WORDS = ("dtu", "gateway", "collector")
TOKEN_FILE = "/data/siseli_cloud_token.json"
# Portal access tokens live about two hours; renew well before that.
TOKEN_MAX_AGE_SEC = 100 * 60
HTTP_TIMEOUT_SEC = 30

# Catalog key -> portal attribute key (category 3 "Control" attributes).
PORTAL_KEYS: Dict[str, str] = {
    "mains_charging_current": "tMainsChargingCurrent",
    "solar_charging_current": "solarCharging",
    "strong_charging_voltage": "tStrongChargingVoltage",
    "floating_voltage": "tFloatingVoltage",
    "locking_voltage": "tLockingVoltage",
    "return_mains_voltage": "returnToMainsChargingVoltagePoint",
    "return_inverter_voltage": "returnInverterVoltage",
    "output_voltage_220": "tOutputVoltage",
    "power_on_battery_voltage": "powerOnBatteryVoltage",
    "low_power_restores_voltage": "lowPowerStartupRestoresVoltage",
    "low_electric_lock_soc": "lowElectricLockMachineSOC",
    "return_mains_soc": "returnToMainsChargingSOC",
    "return_inverter_soc": "returnToInverterSOC",
    "low_power_recovery_soc": "lowPowerStartupRecoverySOC",
    "low_voltage_alarm": "lowVoltageAlarmVoltage",
    "backlight_mode": "backlightMode",
    "input_range": "inputRange",
    "input_frequency_range": "inputFrequencyRange",
    "work_mode": "chargingPrioritySelection",
    "machine_charging_mode": "machineChargingMode",
    "mains_sensitivity": "mainsSensitivity",
    "output_frequency": "tOutputFrequency",
    "fault_restart": "faultRestart",
    "buzzer_mode": "buzzerMode",
    "battery_capacity_limit": "batteryCapacityLimit",
    "load_limiting_function": "loadLimitingFunction",
    "load_offline_prompt": "loadOfflinePrompt",
    "inverter_output_mode": "inverterOutputMode",
    "battery_type": "tBatteryType",
    "bms_communication_switch": "bmsCommunicationSwitch",
    "bms_communication_protocol": "bmsCommunicationProtocol",
}

_lock = threading.RLock()
_token: Dict[str, object] = {"access": None, "saved_at": 0.0}

# App credentials of the portal's own web client, taken from its public
# JavaScript bundle (the same constants the browser sends on every call).
APP_ID = "rBrTRfAPXz"
APP_SECRET_B64 = "I4D0KRr2339z3pQ/at91V9BpFAOe54DaTafwSm6suIQ="


# --- Minimal AES-128 decryption (stdlib has none) -----------------------
# Only needed once, to derive the request-signing key from APP_SECRET_B64
# exactly as the web bundle does.  The S-box is generated programmatically
# (GF(2^8) inverse + affine transform) and the implementation is covered
# by the FIPS-197 test vector in the unit tests.

def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gf_mul(a: int, b: int) -> int:
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _build_sboxes():
    exp = [0] * 512
    log_table = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log_table[x] = i
        x ^= _xtime(x)  # multiply by generator 3
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    sbox = [0] * 256
    inv_sbox = [0] * 256
    for value in range(256):
        inv = 0 if value == 0 else exp[255 - log_table[value]]
        s = inv
        for shift in (1, 2, 3, 4):
            s ^= ((inv << shift) | (inv >> (8 - shift))) & 0xFF
        s = (s ^ 0x63) & 0xFF
        sbox[value] = s
        inv_sbox[s] = value
    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_sboxes()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _expand_key(key: bytes):
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [[b for word in words[r * 4:(r + 1) * 4] for b in word] for r in range(11)]


def _aes128_decrypt_block(round_keys, block: bytes) -> bytes:
    state = [block[i] ^ round_keys[10][i] for i in range(16)]
    for rnd in range(9, 0, -1):
        # InvShiftRows: row r shifts right by r (state is column-major).
        shifted = [0] * 16
        for col in range(4):
            for row in range(4):
                shifted[col * 4 + row] = state[((col - row) % 4) * 4 + row]
        state = [_INV_SBOX[b] for b in shifted]
        state = [state[i] ^ round_keys[rnd][i] for i in range(16)]
        # InvMixColumns with matrix 14 11 13 9.
        mixed = [0] * 16
        for col in range(4):
            a = state[col * 4:(col + 1) * 4]
            mixed[col * 4 + 0] = _gf_mul(a[0], 14) ^ _gf_mul(a[1], 11) ^ _gf_mul(a[2], 13) ^ _gf_mul(a[3], 9)
            mixed[col * 4 + 1] = _gf_mul(a[0], 9) ^ _gf_mul(a[1], 14) ^ _gf_mul(a[2], 11) ^ _gf_mul(a[3], 13)
            mixed[col * 4 + 2] = _gf_mul(a[0], 13) ^ _gf_mul(a[1], 9) ^ _gf_mul(a[2], 14) ^ _gf_mul(a[3], 11)
            mixed[col * 4 + 3] = _gf_mul(a[0], 11) ^ _gf_mul(a[1], 13) ^ _gf_mul(a[2], 9) ^ _gf_mul(a[3], 14)
        state = mixed
    shifted = [0] * 16
    for col in range(4):
        for row in range(4):
            shifted[col * 4 + row] = state[((col - row) % 4) * 4 + row]
    state = [_INV_SBOX[b] for b in shifted]
    return bytes(state[i] ^ round_keys[0][i] for i in range(16))


def _aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    round_keys = _expand_key(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        decrypted = _aes128_decrypt_block(round_keys, block)
        out.extend(decrypted[i] ^ previous[i] for i in range(16))
        previous = block
    return bytes(out)


_signing_key_cache: Optional[bytes] = None


def _signing_key() -> bytes:
    """Derive the HMAC key from the app secret, as the web bundle does."""

    global _signing_key_cache
    if _signing_key_cache is None:
        digest = hashlib.md5(APP_ID.encode("utf-8")).hexdigest()
        key = digest[:16].encode("utf-8")
        iv = digest[16:32].encode("utf-8")
        plain = _aes128_cbc_decrypt(key, iv, base64.b64decode(APP_SECRET_B64))
        _signing_key_cache = plain.rstrip(b"\x00").strip()
    return _signing_key_cache


_NONCE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _nonce() -> str:
    return "".join(_NONCE_ALPHABET[b % len(_NONCE_ALPHABET)] for b in os.urandom(32))


def _sign_headers(method: str, path: str, body_str: Optional[str]) -> Dict[str, str]:
    _, _, query = path.partition("?")
    params = dict(urllib.parse.parse_qsl(query)) if query else {}
    if method.upper() == "GET":
        params["IOT-Open-Body-Hash"] = ""
    else:
        params["IOT-Open-Body-Hash"] = hashlib.sha256((body_str or "").encode("utf-8")).hexdigest()
    nonce = _nonce()
    params["IOT-Open-AppID"] = APP_ID
    params["IOT-Open-Nonce"] = nonce
    canonical = "&".join(f"{name}={params[name]}" for name in sorted(params))
    message = base64.b64encode(canonical.encode("utf-8"))
    digest = hmac.new(_signing_key(), message, hashlib.sha256).digest()
    return {
        "IOT-Open-AppID": APP_ID,
        "IOT-Open-Nonce": nonce,
        "IOT-Open-Sign": hashlib.md5(digest).hexdigest(),
    }


def _request_json(
    method: str,
    path: str,
    body: Optional[dict],
    token: Optional[str],
) -> Tuple[Optional[int], Optional[dict]]:
    """One signed JSON request against the portal API.

    Returns ``(http_status, parsed_json)``; ``parsed_json`` is ``None`` on
    transport or parse failure.  Never logs the body or headers.
    """

    url = API_BASE + path
    body_str = json.dumps(body) if body is not None else None
    data = body_str.encode("utf-8") if body_str is not None else None
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "IOT-Time-Zone": "America/New_York",
    }
    headers.update(_sign_headers(method, path, body_str))
    if token:
        headers["IOT-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.getcode()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        status = exc.code
    except Exception as exc:
        log(f"[PARAM] Cloud API transport error: {exc}", level="warning")
        return None, None
    try:
        return status, json.loads(raw)
    except (TypeError, ValueError):
        return status, None


def _save_token_locked() -> None:
    try:
        payload = {
            "access": _token["access"],
            "saved_at": _token["saved_at"],
        }
        tmp_path = TOKEN_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, TOKEN_FILE)
    except Exception:
        # A token that cannot be persisted only costs a future re-login.
        pass


def _load_token_locked() -> None:
    if _token["access"]:
        return
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        access = payload.get("access")
        saved_at = float(payload.get("saved_at") or 0)
        if access and (time.time() - saved_at) < TOKEN_MAX_AGE_SEC:
            _token["access"] = access
            _token["saved_at"] = saved_at
    except Exception:
        pass


def _login_locked() -> bool:
    """Log in with the configured portal credentials.  Caller holds lock."""

    user = (config.SISELI_PORTAL_USER or "").strip()
    password = config.SISELI_PORTAL_PASSWORD or ""
    if not user or not password:
        log(
            "[PARAM] Cloud send unavailable: portal credentials are not "
            "configured (SISELI_PORTAL_USER / SISELI_PORTAL_PASSWORD)",
            level="warning",
        )
        return False
    md5_password = hashlib.md5(password.encode("utf-8")).hexdigest()
    status, payload = _request_json(
        "POST",
        "/login/account",
        {"account": user, "password": md5_password, "type": "account"},
        None,
    )
    data = (payload or {}).get("data") or {}
    access = data.get("accessToken")
    if status != 200 or not access:
        code = (payload or {}).get("code")
        log(
            f"[PARAM] Cloud login failed (http={status}, code={code})",
            level="warning",
        )
        return False
    _token["access"] = access
    _token["saved_at"] = time.time()
    _save_token_locked()
    log("[PARAM] Cloud login OK; token cached", level="info")
    return True


def _ensure_token_locked(force: bool = False) -> Optional[str]:
    if not force:
        _load_token_locked()
        access = _token["access"]
        saved_at = float(_token["saved_at"] or 0)
        if access and (time.time() - saved_at) < TOKEN_MAX_AGE_SEC:
            return str(access)
    if _login_locked():
        return str(_token["access"])
    return None


def _fetch_attributes(token: str) -> Optional[Dict[str, dict]]:
    status, payload = _request_json(
        "POST",
        f"/remote/device/configs/cache/get?deviceId={PORTAL_DEVICE_ID}",
        {},
        token,
    )
    if status != 200 or not payload or payload.get("code") != 0:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _format_number(value: object) -> Tuple[object, str]:
    """Normalize a numeric display value the way the portal stores it."""

    number = float(value)  # type: ignore[arg-type]
    if number == int(number):
        number = int(number)
    return number, ("%g" % float(number))


def send_param(param: Dict[str, object], display_value: object) -> bool:
    """Send one parameter through the real cloud.

    ``param`` is a copy of a :mod:`siseli_bridge.params` catalog entry and
    ``display_value`` is the user-facing value (for example ``14.2`` or
    ``"LOD"``), not the encoded CSPA byte.  Returns True only when the
    portal confirms the write (``code == 0``); Home Assistant state still
    changes only when telemetry confirms the value, exactly as with the
    isolated route.
    """

    key = param.get("key", "?") if isinstance(param, dict) else "?"
    portal_key = PORTAL_KEYS.get(str(key))
    if portal_key is None:
        log(f"[PARAM] No portal attribute mapped for parameter {key!r}", level="warning")
        return False

    with _lock:
        token = _ensure_token_locked()
        if token is None:
            return False
        for attempt in (1, 2):
            attributes = _fetch_attributes(token)
            if attributes is None:
                if attempt == 1:
                    token = _ensure_token_locked(force=True)
                    if token is None:
                        return False
                    continue
                log("[PARAM] Cloud attribute fetch failed", level="warning")
                return False
            attribute = attributes.get(portal_key)
            if not isinstance(attribute, dict):
                log(
                    f"[PARAM] Portal attribute {portal_key!r} not found for "
                    f"parameter {key!r}",
                    level="warning",
                )
                return False
            body = dict(attribute)
            if param.get("kind") == "select":
                options = param.get("options") or {}
                label = str(display_value)
                if label not in options:
                    log(
                        f"[PARAM] Invalid select label {label!r} for {key!r}",
                        level="warning",
                    )
                    return False
                body["value"] = str(options[label])
                body["valueDisplay"] = label
            else:
                number, display = _format_number(display_value)
                body["value"] = number
                body["valueDisplay"] = display
            status, payload = _request_json(
                "POST",
                f"/remote/device/config/write?deviceId={PORTAL_DEVICE_ID}",
                body,
                token,
            )
            if status == 200 and payload and payload.get("code") == 0:
                log(
                    f"[PARAM] Cloud send OK for {key} "
                    f"(portal key={portal_key}, value={body['valueDisplay']})",
                    level="info",
                )
                return True
            code = (payload or {}).get("code")
            message = (payload or {}).get("message")
            if attempt == 1:
                log(
                    f"[PARAM] Cloud send for {key} failed "
                    f"(http={status}, code={code}, message={message!r}); "
                    "renewing session and retrying once",
                    level="warning",
                )
                token = _ensure_token_locked(force=True)
                if token is None:
                    return False
                continue
            log(
                f"[PARAM] Cloud send for {key} failed after retry "
                f"(http={status}, code={code}, message={message!r})",
                level="warning",
            )
            return False
    return False


def _post_restart(token: str, dtu_id: str) -> Tuple[Optional[int], Optional[dict]]:
    path = f"/remote/dtu/restart?dtuId={urllib.parse.quote(str(dtu_id), safe='')}"
    return _request_json("POST", path, None, token)


def _walk_records(node: object) -> Iterator[dict]:
    """Yield every dict in a decoded JSON tree, depth-first."""

    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_records(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_records(item)


def _record_serial(record: dict) -> Optional[str]:
    for key in _DTU_SERIAL_KEYS:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _numeric_field(record: dict, key: str) -> Optional[str]:
    value = record.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str) and value.strip().isdigit():
        return value.strip()
    return None


def _record_numeric_id(record: dict) -> Optional[str]:
    return _numeric_field(record, "id")


def _record_matches_serial(record: dict, serial: str) -> bool:
    """Whether a /device/list record is the DTU we are looking for.

    Three ways, widest first: the serial under any of the known serial
    keys; the serial in the record's ``dtuDtuid`` field -- the shape
    observed live on 2026-10-24 (v2.9.3), where the list returns
    DEVICE records and the inverter's record carries the DTU's serial
    there (and in ``dtuName``); or, as a fallback, ``dtuName`` equal
    to the serial when the record also carries a ``dtuId``.
    """

    if _record_serial(record) == serial:
        return True
    dtu_dtuid = record.get("dtuDtuid")
    if dtu_dtuid is not None and str(dtu_dtuid).strip() == serial:
        return True
    dtu_name = record.get("dtuName")
    if (
        dtu_name is not None
        and str(dtu_name).strip() == serial
        and _numeric_field(record, "dtuId") is not None
    ):
        return True
    return False


def _record_mentions_dtu(record: dict) -> bool:
    for key in _DTU_TYPE_KEYS:
        value = record.get(key)
        if isinstance(value, str):
            lowered = value.lower()
            if any(word in lowered for word in _DTU_TYPE_WORDS):
                return True
    return False


def _find_dtu_id(data: object, serial: str) -> Optional[str]:
    """Find the portal's internal DTU id in a /device/list payload.

    A candidate is any record matching the DTU serial the bridge
    already knows (see :func:`_record_matches_serial`).  The id to use
    is that record's own ``dtuId`` field when it carries one -- the
    live v2.9.3 record held ``dtuId`` 410736032810500114 while its
    ``id`` was the *inverter's* id, so ``dtuId`` takes priority; the
    numeric ``id`` is the fallback, as before.  A value equal to the
    serial itself is skipped -- that is exactly what the portal
    rejected.  When several records match, one whose type/model
    mentions a DTU, gateway or collector wins.
    """

    candidates: List[str] = []
    preferred: List[str] = []
    for record in _walk_records(data):
        if not _record_matches_serial(record, serial):
            continue
        candidate_id = _numeric_field(record, "dtuId")
        if candidate_id is None:
            candidate_id = _record_numeric_id(record)
        if candidate_id is None or candidate_id == serial:
            continue
        candidates.append(candidate_id)
        if _record_mentions_dtu(record):
            preferred.append(candidate_id)
    if preferred:
        return preferred[0]
    return candidates[0] if candidates else None


def _truncate_payload(payload: Optional[dict], limit: int = 400) -> str:
    try:
        text = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    return text if len(text) <= limit else text[:limit] + "..."


def _discover_dtu_id_locked(token: str) -> Optional[str]:
    """Discover the portal's internal DTU id via /device/list.

    The list endpoint's body schema is settled by the live v2.9.2
    answers from the portal's Java backend: ``page`` as an object
    failed with "Cannot deserialize instance of java.lang.Integer out
    of START_OBJECT token" (so ``page`` is a plain Integer), and
    ``{"page": 1, "pageSize": 50}`` failed with argument ``count``
    "must not be null" -- ``count`` is the page-size field, and both
    ``page`` and ``count`` are required.  Exactly two bodies are
    tried, stopping at the first that yields a candidate:

    a. ``{"stationId": ..., "page": 1, "count": 50}`` (station-filtered)
    b. ``{"page": 1, "count": 50}`` (unfiltered)

    The serial matched against is :data:`config.SISELI_DTU_SERIAL` --
    independent of the (possibly wrong) configured dtuId.  Caller holds
    the lock.  Returns None when no record matches; failed responses
    are logged truncated to 1500 chars so the full ``errors`` array of
    the validation verdict survives.
    """

    serial = (config.SISELI_DTU_SERIAL or "").strip()
    if not serial:
        log(
            "[RESTART] DTU serial unknown; cannot run /device/list discovery",
            level="warning",
        )
        return None
    ladder = (
        {"stationId": PORTAL_STATION_ID, "page": 1, "count": 50},
        {"page": 1, "count": 50},
    )
    for body in ladder:
        status, payload = _request_json("POST", "/device/list", body, token)
        if status == 200 and payload and payload.get("code") == 0:
            found = _find_dtu_id(payload.get("data"), serial)
            if found:
                return found
            log(
                f"[RESTART] /device/list (body={body}) has no record with "
                f"serial {serial}; response (truncated): "
                f"{_truncate_payload(payload)}",
                level="warning",
            )
        else:
            code = (payload or {}).get("code")
            log(
                f"[RESTART] /device/list (body={body}) failed "
                f"(http={status}, code={code}); response (truncated): "
                f"{_truncate_payload(payload, 1500)}",
                level="warning",
            )
    return None


def _restart_with_discovered_id_locked(
    token: str, rejected_detail: str
) -> Tuple[bool, str]:
    """One discovery phase plus exactly one restart retry.  Lock held."""

    discovered = _discover_dtu_id_locked(token)
    if discovered is None:
        detail = (
            f"configured dtuId rejected ({rejected_detail}); /device/list "
            "discovery found no device with the DTU serial"
        )
        log(f"[RESTART] Cloud DTU restart failed: {detail}", level="warning")
        return False, detail
    log(f"[RESTART] discovered dtuId={discovered} via /device/list", level="info")
    status, payload = _post_restart(token, discovered)
    if status == 200 and payload and payload.get("code") == 0:
        log(
            f"[RESTART] Cloud DTU restart accepted for dtuId={discovered}",
            level="info",
        )
        return True, (
            "portal accepted the restart (code=0) with discovered "
            f"dtuId={discovered}; set SISELI_DTU_ID={discovered} to skip "
            "discovery on future presses"
        )
    code = (payload or {}).get("code")
    message = (payload or {}).get("message")
    detail = (
        f"discovered dtuId={discovered} also rejected "
        f"(http={status}, code={code}, message={message!r})"
    )
    log(f"[RESTART] Cloud DTU restart failed: {detail}", level="warning")
    return False, detail


def restart_dtu() -> Tuple[bool, str]:
    """Ask the real cloud to reboot the DTU (dongle).

    Reproduces the portal's own Restart button, confirmed from its web
    bundle: ``POST /remote/dtu/restart?dtuId=<id>`` with the session token
    and no body.  The cloud then delivers a body-less ``dtu_rpc`` envelope
    (``c=26``) to the dongle, which drops its MQTT session and reboots --
    captured live on 2026-10-24.  Returns ``(ok, detail)``; ``ok`` means
    the portal accepted the request (``code == 0``), not that the reboot
    itself was observed.  The DTU id comes from
    :data:`config.SISELI_DTU_ID`.

    Failure handling is bounded per press, with no retry loops: an
    authentication-style failure is retried once after a fresh login,
    exactly like :func:`send_param`; a ``20101`` ("illegal argument")
    rejection means the id itself is wrong -- observed live on
    2026-10-24 with the DTU serial as the id -- so instead of re-login it
    triggers one discovery phase (:func:`_discover_dtu_id_locked`) and,
    when a candidate id is found, exactly one more restart attempt with
    it.  Anything else fails closed with the portal's verdict in the
    detail string.
    """

    dtu_id = (config.SISELI_DTU_ID or "").strip()
    if not dtu_id:
        detail = "SISELI_DTU_ID is not configured"
        log(f"[RESTART] Cloud DTU restart unavailable: {detail}", level="warning")
        return False, detail

    with _lock:
        token = _ensure_token_locked()
        if token is None:
            return False, "portal login failed (no token)"
        detail = ""
        for attempt in (1, 2):
            status, payload = _post_restart(token, dtu_id)
            if status == 200 and payload and payload.get("code") == 0:
                log(
                    f"[RESTART] Cloud DTU restart accepted for dtuId={dtu_id}",
                    level="info",
                )
                return True, "portal accepted the restart (code=0)"
            code = (payload or {}).get("code")
            message = (payload or {}).get("message")
            detail = f"http={status}, code={code}, message={message!r}"
            if code == RESTART_ILLEGAL_ARGUMENT_CODE:
                # Not an auth problem: re-login cannot fix a bad id.
                break
            if attempt == 1:
                log(
                    f"[RESTART] Cloud DTU restart failed ({detail}); "
                    "renewing session and retrying once",
                    level="warning",
                )
                token = _ensure_token_locked(force=True)
                if token is None:
                    return False, "portal re-login failed after the first failure"
                continue
            log(
                f"[RESTART] Cloud DTU restart failed after retry ({detail})",
                level="warning",
            )
            return False, detail
        return _restart_with_discovered_id_locked(token, detail)
