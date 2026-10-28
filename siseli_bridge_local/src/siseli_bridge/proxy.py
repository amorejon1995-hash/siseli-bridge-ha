import base64
import binascii
import json
import secrets
import socket
import string
import struct
import threading
import time

from .config import LISTEN_PORT, UPDATE_INTERVAL_SEC, MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, RPC_TEMPLATE_FILE, CI_RPC_TEMPLATE_FILE, ENABLE_RPC_POLLING, ENABLE_FIRST_POLL_DELAY, RPC_FIRST_POLL_DELAY_SEC, ISOLATE_CLOUD, ENABLE_APP_ACK, APP_PAYLOAD_NUL_PREFIX, MAX_MQTT_PACKET, ENABLE_LIVE_POLLING, LIVE_POLL_INTERVAL_SEC, ENABLE_PARAM_CONTROLS
from .loggers import log
from .parsers import SolarParser, extract_publish_payload


def _encode_remaining_length(rem_len: int) -> bytes:
    rem_len_bytes = bytearray()
    while True:
        digit = rem_len % 128
        rem_len //= 128
        if rem_len > 0:
            digit |= 0x80
        rem_len_bytes.append(digit)
        if rem_len == 0:
            break
    return bytes(rem_len_bytes)


def make_mqtt_publish_packet(topic: str, payload: bytes, packet_id: int = 0) -> bytes:
    """Build a raw MQTT PUBLISH packet (QoS 1 if packet_id > 0, else QoS 0)."""
    topic_bytes = topic.encode('utf-8')
    topic_len = len(topic_bytes)
    if packet_id > 0:
        variable_header = struct.pack("!H", topic_len) + topic_bytes + struct.pack("!H", packet_id)
        body = variable_header + payload
        return b"\x32" + _encode_remaining_length(len(body)) + body
    else:
        variable_header = struct.pack("!H", topic_len) + topic_bytes
        body = variable_header + payload
        return b"\x30" + _encode_remaining_length(len(body)) + body


def _encode_app_payload(obj: dict) -> bytes:
    """Serialize an application payload the way the real cloud does."""
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return (b"\x00" + raw) if APP_PAYLOAD_NUL_PREFIX else raw


def _load_learned_rpc_template() -> dict | None:
    """Return the last genuine cloud RPC captured in learning mode, if any."""
    if not RPC_TEMPLATE_FILE:
        return None
    try:
        with open(RPC_TEMPLATE_FILE, "r", encoding="utf-8") as handle:
            template = json.load(handle)
        if not isinstance(template, dict) or not isinstance(template.get("b"), dict):
            return None
        return template
    except (OSError, ValueError, TypeError):
        return None


def _send_rpc_request(sock: socket.socket, device_id: str, counter: int) -> None:
    """Send a MQTT PUBLISH with a dev_rpc Batch Read command to the inverter socket.

    Mirrors the genuine cloud packet: empty body, i=501, QoS 0.  Requesting
    specific block names via "ct"/"cn" is NOT honored by this firmware --
    captured traffic shows it replying e:104 to a by-name request.  Block
    names are fixed per publishing topic (dev_event_post -> "Adlg",
    dev_prop_post -> "OgBU"), i.e. response-side labels, not addressable
    registers.
    """
    alphabet = string.ascii_letters + string.digits
    t_val = "".join(secrets.choice(alphabet) for _ in range(8))
    s_val = "".join(secrets.choice(alphabet) for _ in range(9))

    topic = f"dtu/{device_id}/sub/service/dev_rpc"
    payload_obj = {
        "c": 5,
        "t": t_val,
        "s": s_val,
        "i": 501,
        "b": {},
    }
    payload = _encode_app_payload(payload_obj)

    # Always QoS 0 -- matches the real cloud's genuine RPC polls (captured
    # traffic showed fixed header 0x30, QoS bits 00).
    pkt = make_mqtt_publish_packet(topic, payload, packet_id=0)
    sock.sendall(pkt)
    log(
        f"[PROXY] Sent RPC poll #{counter} to topic={topic} "
        f"(empty-body batch read, i=501, QoS 0, t={t_val}, s={s_val})",
        level="info",
    )

def _load_learned_ci_template() -> dict | None:
    """Return the last genuine cloud 'ci' (CMSG/INV-HB heartbeat) RPC, if any.

    Only the CMSG heartbeat variant is safe to replay unattended -- the other
    family (CSPA/CSIPAM) is a config-write command carrying a real setpoint
    value, and replaying it on a timer would silently re-apply a stale
    setting to the inverter every cycle. This check guards against a stale
    template file saved before that distinction was enforced in core.py.
    """
    if not CI_RPC_TEMPLATE_FILE:
        return None
    try:
        with open(CI_RPC_TEMPLATE_FILE, "r", encoding="utf-8") as handle:
            template = json.load(handle)
        if not isinstance(template, dict) or not isinstance(template.get("b"), dict):
            return None
        ci_value = template["b"].get("ci")
        if not isinstance(ci_value, str):
            return None
        try:
            decoded = base64.b64decode(ci_value, validate=False)
        except (ValueError, binascii.Error):
            return None
        if not decoded.startswith(b"CMSG"):
            return None
        return template
    except (OSError, ValueError, TypeError):
        return None


# Genuine read command captured from the real cloud on 2026-10-23 (portal
# "Read"/"Batch Read" on the inverter): ci = base64("CMSG" + 0x06 + "INV-HB").
# Read-only and safe to send unattended, unlike the CSPA write family. Used
# as the ci payload when no learned template is available.
_BUILTIN_INV_HB_RPC = {"c": 5, "i": 503, "b": {"ci": "Q01TRwZJTlYtSEI=", "no": 0, "rs": 0}}


# Genuine DTU-restart downlink captured from the real cloud on 2026-10-24
# (Alfredo pressed the portal's Restart button at 09:15:11 EDT): a
# body-less envelope {"c":26,"t":...,"s":...,"i":2603} published on the
# service/dtu_rpc topic (NOT dev_rpc), after which the dongle sent an MQTT
# DISCONNECT and rebooted.  i=2603 is replayed verbatim from the capture,
# the same way i=501/i=503 are fixed per command family on dev_rpc.
DTU_RPC_RESTART_CODE = 26
DTU_RPC_RESTART_I = 2603


def _send_ci_request(sock: socket.socket, device_id: str, counter: int) -> None:
    """Replay a learned cloud 'ci' capability/keepalive handshake to the inverter.

    Unlike the 'ct' telemetry poll, this body is opaque (base64 "CSPA..."
    blob) and the dongle acknowledges it generically ("MAPAACK") regardless
    of content. We only rotate the transaction fields (t/s) and resend the
    learned 'ci' value verbatim, since we don't yet know how to construct
    one from scratch.
    """
    template = _load_learned_ci_template()
    source = "learned"
    if not template:
        template = json.loads(json.dumps(_BUILTIN_INV_HB_RPC))
        source = "built-in INV-HB"

    alphabet = string.ascii_letters + string.digits
    t_val = "".join(secrets.choice(alphabet) for _ in range(8))
    s_val = "".join(secrets.choice(alphabet) for _ in range(9))

    payload_obj = json.loads(json.dumps(template))
    payload_obj["t"] = t_val
    payload_obj["s"] = s_val

    topic = f"dtu/{device_id}/sub/service/dev_rpc"
    # Route through _encode_app_payload so the NUL prefix (0x00 byte) is
    # applied when APP_PAYLOAD_NUL_PREFIX is true, matching _send_rpc_request.
    # Previously this used plain json.dumps().encode(), which omitted the
    # prefix the protocol requires -- the resulting packet was malformed.
    payload = _encode_app_payload(payload_obj)

    qos = 1 if (counter % 2 != 0) else 0
    packet_id = (counter % 65000) + 1 if qos == 1 else 0

    pkt = make_mqtt_publish_packet(topic, payload, packet_id=packet_id)
    sock.sendall(pkt)
    log(
        f"[PROXY] Sent 'ci' command #{counter} ({source}) to topic={topic} "
        f"(t={t_val}, s={s_val})",
        level="info",
    )


class DummySiseliCloud:
    def __init__(self):
        self.running = False
        self.server_socket = None
        # Tracks the inverter's current live connection, so a manual send
        # (triggered from HA, outside the poller loop) can use the same
        # socket/device_id without needing its own connection.
        self.active_socket = None
        self.active_device_id = None
        self._manual_lock = threading.Lock()
        self._manual_counter = 0

    def send_manual_rpc(self) -> bool:
        """Send a single dev_rpc request right now, independent of the
        automatic poller (works whether or not ENABLE_RPC_POLLING is on).
        Only available when ISOLATE_CLOUD=true -- when the real cloud is
        also active (ISOLATE_CLOUD=false), a manual send here would race
        with genuine cloud traffic on the same connection and is more
        likely to confuse a live capture than to test anything useful.
        Returns False if the inverter isn't currently connected, or if
        ISOLATE_CLOUD is off."""
        if not ISOLATE_CLOUD:
            log("[PROXY] Manual RPC send skipped: only available when ISOLATE_CLOUD=true", level="warning")
            return False
        with self._manual_lock:
            sock = self.active_socket
            device_id = self.active_device_id
            if not sock or not device_id:
                log("[PROXY] Manual RPC requested but inverter is not connected", level="warning")
                return False
            self._manual_counter += 1
            try:
                _send_rpc_request(sock, device_id, self._manual_counter)
                return True
            except Exception as e:
                log(f"[PROXY] Manual RPC send failed: {e}", level="error")
                return False

    def send_manual_payload(self, raw_text: str) -> bool:
        """Send an arbitrary, user-supplied payload to the inverter's
        dev_rpc topic, for probing what it does/doesn't respond to.

        If raw_text parses as a JSON object, missing 't'/'s' transaction
        fields are auto-filled with fresh random tokens (matching the
        format the firmware expects) so you only have to type the parts
        you actually want to test -- e.g. '{"c":5,"b":{"ct":[{"cn":"OgBU"}]}}'.
        If it isn't valid JSON, it's sent verbatim as raw bytes, useful for
        testing malformed/non-standard payloads.

        Only available when ISOLATE_CLOUD=true, for the same reason as
        send_manual_rpc -- avoids interfering with a genuine cloud session.
        """
        if not ISOLATE_CLOUD:
            log("[PROXY] Manual payload send skipped: only available when ISOLATE_CLOUD=true", level="warning")
            return False
        with self._manual_lock:
            sock = self.active_socket
            device_id = self.active_device_id
            if not sock or not device_id:
                log("[PROXY] Manual payload requested but inverter is not connected", level="warning")
                return False

            try:
                obj = json.loads(raw_text)
            except (ValueError, TypeError):
                obj = None

            if isinstance(obj, dict):
                alphabet = string.ascii_letters + string.digits
                if "t" not in obj:
                    obj["t"] = "".join(secrets.choice(alphabet) for _ in range(8))
                if "s" not in obj:
                    obj["s"] = "".join(secrets.choice(alphabet) for _ in range(9))
                # Encode the same way the real cloud does, incl. the leading
                # 0x00 byte (APP_PAYLOAD_NUL_PREFIX). Without it the firmware
                # silently ignores the request -- so a probe would look
                # "rejected" when it was really just malformed. Raw non-JSON
                # text below is still sent verbatim, on purpose, so you can
                # test malformed/non-standard payloads too.
                payload = _encode_app_payload(obj)
                payload_desc = json.dumps(obj, separators=(',', ':'))
            else:
                payload = raw_text.encode("utf-8", errors="ignore")
                payload_desc = raw_text + " (raw, no NUL prefix)"

            topic = f"dtu/{device_id}/sub/service/dev_rpc"
            try:
                pkt = make_mqtt_publish_packet(topic, payload, packet_id=0)
                sock.sendall(pkt)
                log(f"[PROXY] Sent MANUAL payload to topic={topic}: {payload_desc}", level="info")
                return True
            except Exception as e:
                log(f"[PROXY] Manual payload send failed: {e}", level="error")
                return False

    def send_param_write(self, addr: int, raw: int) -> bool:
        """Send one verified-format CSPA/CSIPAM parameter write.

        This is intentionally narrower than :meth:`send_manual_payload`: the
        caller supplies only a catalog address and raw byte, and the CSPA
        payload is constructed here so an arbitrary payload cannot be sent
        through the parameter-control path.

        The write is hard-gated to isolated-cloud mode.  In non-isolated
        mode the DTU's session belongs to the real cloud, and parameter
        commands must use the cloud API route instead of this socket.
        """

        if not ENABLE_PARAM_CONTROLS:
            log("[PARAM] Parameter write skipped: ENABLE_PARAM_CONTROLS=false", level="warning")
            return False
        if not ISOLATE_CLOUD:
            log("[PARAM] Parameter write skipped: only available when ISOLATE_CLOUD=true", level="warning")
            return False

        # Lazy import keeps params independent of the proxy singleton and
        # avoids any import-order cycle between the two modules.
        from .params import build_cspa_ci

        try:
            ci = build_cspa_ci(addr, raw)
        except (TypeError, ValueError) as exc:
            log(f"[PARAM] Invalid parameter write (addr={addr!r}, raw={raw!r}): {exc}", level="warning")
            return False

        with self._manual_lock:
            sock = self.active_socket
            device_id = self.active_device_id
            if not sock or not device_id:
                log("[PARAM] Parameter write requested but inverter is not connected", level="warning")
                return False

            alphabet = string.ascii_letters + string.digits
            payload_obj = {
                "c": 5,
                "t": "".join(secrets.choice(alphabet) for _ in range(8)),
                "s": "".join(secrets.choice(alphabet) for _ in range(9)),
                "i": 503,
                "b": {
                    "ci": base64.b64encode(ci).decode("ascii"),
                    "no": 0,
                    "rs": 0,
                },
            }
            topic = f"dtu/{device_id}/sub/service/dev_rpc"
            try:
                pkt = make_mqtt_publish_packet(
                    topic,
                    _encode_app_payload(payload_obj),
                    packet_id=0,
                )
                sock.sendall(pkt)
            except Exception as exc:
                log(f"[PARAM] Parameter write send failed: {exc}", level="error")
                return False

            self._manual_counter += 1
            log(
                f"[PARAM] Sent CSPA parameter write #{self._manual_counter} "
                f"to topic={topic} (addr=0x{addr:02x}, raw={raw}, ci={ci.hex()})",
                level="info",
            )
            return True

    def send_dtu_restart(self) -> bool:
        """Reboot the DTU by replaying the genuine cloud restart envelope.

        Isolated mode only, for the same reason as :meth:`send_manual_rpc`:
        in non-isolated mode the DTU's session belongs to the real cloud
        and the restart must go through the portal API instead
        (``cloud_api.restart_dtu``).  The send itself reuses the exact
        machinery of :meth:`send_param_write` -- same lock, same active
        socket, same packet encoder -- and differs only in the topic
        (``service/dtu_rpc``) and the body-less ``c=26`` envelope taken
        verbatim from the 2026-10-24 capture (see DTU_RPC_RESTART_*).
        Acceptance by the dongle on this local session has not been
        verified live yet; deployment will confirm it the first time the
        button is pressed in isolated mode.
        """

        if not ISOLATE_CLOUD:
            log("[RESTART] Local DTU restart skipped: only available when ISOLATE_CLOUD=true", level="warning")
            return False

        with self._manual_lock:
            sock = self.active_socket
            device_id = self.active_device_id
            if not sock or not device_id:
                log("[RESTART] DTU restart requested but inverter is not connected", level="warning")
                return False

            alphabet = string.ascii_letters + string.digits
            payload_obj = {
                "c": DTU_RPC_RESTART_CODE,
                "t": "".join(secrets.choice(alphabet) for _ in range(8)),
                "s": "".join(secrets.choice(alphabet) for _ in range(9)),
                "i": DTU_RPC_RESTART_I,
            }
            topic = f"dtu/{device_id}/sub/service/dtu_rpc"
            try:
                pkt = make_mqtt_publish_packet(
                    topic,
                    _encode_app_payload(payload_obj),
                    packet_id=0,
                )
                sock.sendall(pkt)
            except Exception as exc:
                log(f"[RESTART] DTU restart send failed: {exc}", level="error")
                return False

            self._manual_counter += 1
            log(
                f"[RESTART] Sent DTU restart envelope #{self._manual_counter} "
                f"to topic={topic} (c={DTU_RPC_RESTART_CODE}, i={DTU_RPC_RESTART_I})",
                level="info",
            )
            return True

    def start(self):
        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_socket.bind(('0.0.0.0', LISTEN_PORT))
            self.server_socket.listen(5)
            threading.Thread(target=self.accept_clients, daemon=True).start()
            log(f"[PROXY] Dummy Siseli Cloud TCP Server started on port {LISTEN_PORT}", level="info")
        except Exception as e:
            log(f"[PROXY] Failed to start server on port {LISTEN_PORT}: {e}", level="error")

    def stop(self):
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass

    def accept_clients(self):
        while self.running:
            try:
                client_sock, addr = self.server_socket.accept()
                log(f"[PROXY] Inverter connected from {addr[0]}:{addr[1]}", level="info")
                threading.Thread(target=self.handle_client, args=(client_sock,), daemon=True).start()
            except Exception as e:
                if self.running:
                    log(f"[PROXY] Accept error: {e}", level="error")

    def handle_client(self, client_sock):
        """
        Handle a single TCP connection from the inverter.

        Lifecycle:
          1. Receive MQTT CONNECT  → send CONNACK
          2. Receive MQTT SUBSCRIBE → send SUBACK, discover device_id, start internal poller
          3. Poller sends PUBLISH (dev_rpc) every UPDATE_INTERVAL_SEC via the same socket
          4. Receive MQTT PUBLISH  → parse payload, forward state to HA via SolarParser
          5. Send PUBACK if QoS > 0
          6. Handle PINGREQ → PINGRESP
        """
        client_sock.settimeout(120)
        buffer = b""
        device_id = None
        poller_thread = None
        poller_stop = threading.Event()
        # Monotonic timestamp of the FIRST PUBLISH from the inverter on this
        # connection; anchors the deferred first RPC poll. List-wrapped so the
        # nested poller closure can read a value set later by the packet loop.
        first_publish_mono = [None]
        # Separate MQTT client to push to HA MQTT broker
        ha_mqtt = None
        self.active_socket = client_sock

        def _start_ha_mqtt():
            nonlocal ha_mqtt
            try:
                import paho.mqtt.client as mqtt_lib
                ha_mqtt = mqtt_lib.Client()
                if MQTT_USER and MQTT_PASSWORD:
                    ha_mqtt.username_pw_set(MQTT_USER, MQTT_PASSWORD)
                ha_mqtt.connect(MQTT_HOST, int(MQTT_PORT), 60)
                ha_mqtt.loop_start()
                log(f"[PROXY] HA MQTT relay connected to {MQTT_HOST}:{MQTT_PORT}", level="info")
            except Exception as e:
                log(f"[PROXY] HA MQTT relay error: {e}", level="warning")

        def _poller_loop():
            """Send periodic requests to the inverter over the open socket.

            Two independent things happen here for different purposes:
              - The 'ci' capability/keepalive handshake (~once a minute):
                REQUIRED. This loop previously only started when
                ENABLE_RPC_POLLING=true, which meant disabling RPC polling
                also silently killed this keepalive -- confirmed in
                production (2026-07-22): with ENABLE_RPC_POLLING=false the
                device disconnected from the local proxy and never
                reconnected. Now the loop always runs.
              - The 'rpc' telemetry poll (every UPDATE_INTERVAL_SEC):
                confirmed via log audit to get ZERO replies from this
                bridge's own poll (only a genuinely cloud-authenticated
                session gets answered) -- gated behind ENABLE_RPC_POLLING
                purely to reduce log noise, no effect on the keepalive.
            """
            counter = 0
            # 'ci' keepalive cadence -- same as before: ~30s for small
            # intervals, one interval for large ones.
            if ENABLE_LIVE_POLLING:
                ci_interval = LIVE_POLL_INTERVAL_SEC
            else:
                ci_interval = UPDATE_INTERVAL_SEC * max(1, round(30 / UPDATE_INTERVAL_SEC))
            start = time.monotonic()
            next_ci = start + ci_interval
            if ENABLE_FIRST_POLL_DELAY:
                next_rpc = None  # resolved once the first PUBLISH arrives
            else:
                next_rpc = start + UPDATE_INTERVAL_SEC
            first_poll_logged = False
            # Tick every 1s so the anchored first poll fires on time instead of
            # drifting by up to a full interval.
            while not poller_stop.is_set():
                try:
                    now = time.monotonic()
                    if next_rpc is None and first_publish_mono[0] is not None:
                        next_rpc = first_publish_mono[0] + RPC_FIRST_POLL_DELAY_SEC
                        if ENABLE_RPC_POLLING and not first_poll_logged:
                            first_poll_logged = True
                            log(
                                f"[PROXY] First RPC poll scheduled "
                                f"{max(0.0, next_rpc - now):.1f}s from now "
                                f"({RPC_FIRST_POLL_DELAY_SEC}s after first PUBLISH), "
                                f"then every {UPDATE_INTERVAL_SEC}s",
                                level="info",
                            )
                    if ENABLE_RPC_POLLING and next_rpc is not None and now >= next_rpc:
                        counter += 1
                        _send_rpc_request(client_sock, device_id, counter)
                        next_rpc = now + UPDATE_INTERVAL_SEC
                    if now >= next_ci:
                        _send_ci_request(client_sock, device_id, counter)
                        next_ci = now + ci_interval
                except Exception as e:
                    log(f"[PROXY] Poller error: {e}", level="error")
                    break
                poller_stop.wait(1)

        try:
            while self.running:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk

                while len(buffer) > 1:
                    # --- Decode MQTT remaining-length ---
                    # Per MQTT 3.1.1 §2.2.3, the multiplier is capped at
                    # 128**3 (4 bytes max).  A stream that misaligns can
                    # produce a garbage length; without a cap the decoder
                    # blocks forever waiting for bytes that never come.
                    multiplier = 1
                    rem_len = 0
                    pos = 1
                    _rl_bytes = 0
                    while pos < len(buffer):
                        encoded_byte = buffer[pos]
                        rem_len += (encoded_byte & 127) * multiplier
                        multiplier *= 128
                        pos += 1
                        _rl_bytes += 1
                        if (encoded_byte & 128) == 0:
                            break
                        if _rl_bytes >= 4:
                            # Malformed: more than 4 continuation bytes.
                            log("[PROXY] Malformed MQTT remaining-length (>4 bytes), closing connection", level="error")
                            raise ConnectionError("malformed remaining-length")

                    if pos == len(buffer) and (buffer[pos - 1] & 128) != 0:
                        break  # Not enough data for length

                    # Reject packets larger than the configured maximum.
                    # Compare total packet size (fixed header + remaining-length
                    # bytes + payload) not just rem_len, since the on-wire
                    # footprint includes the header.
                    if pos + rem_len > MAX_MQTT_PACKET:
                        log(
                            f"[PROXY] MQTT packet length {rem_len} exceeds MAX_MQTT_PACKET "
                            f"({MAX_MQTT_PACKET}), closing connection",
                            level="error",
                        )
                        raise ConnectionError("packet too large")

                    if len(buffer) < pos + rem_len:
                        break  # Not enough data for payload

                    packet_bytes = buffer[:pos + rem_len]
                    buffer = buffer[pos + rem_len:]

                    packet_type = (packet_bytes[0] >> 4) & 0x0F
                    flags = packet_bytes[0] & 0x0F

                    # ---- CONNECT (1) ----
                    if packet_type == 1:
                        # Extract Client ID with bounds-checking.
                        # A truncated CONNECT can raise struct.error inside
                        # the socket handler thread; guard each access.
                        if len(packet_bytes) < pos + 12:
                            log("[PROXY] Truncated MQTT CONNECT, closing connection", level="error")
                            raise ConnectionError("truncated CONNECT")
                        client_id_len = struct.unpack("!H", packet_bytes[pos + 10:pos + 12])[0]
                        id_end = pos + 12 + client_id_len
                        if len(packet_bytes) < id_end:
                            log(f"[PROXY] CONNECT client_id_len={client_id_len} exceeds buffer, closing", level="error")
                            raise ConnectionError("CONNECT client_id overflow")
                        client_id = packet_bytes[pos + 12:id_end].decode('utf-8', errors='ignore')
                        log(f"[PROXY] MQTT CONNECT received for ClientId='{client_id}' → sending CONNACK", level="info")
                        client_sock.sendall(b"\x20\x02\x00\x00")

                    # ---- PUBLISH (3) ----
                    elif packet_type == 3:
                        qos = (flags >> 1) & 0x03
                        if first_publish_mono[0] is None:
                            first_publish_mono[0] = time.monotonic()
                            log("[PROXY] First PUBLISH received -- first-poll delay anchor set", level="info")
                        topic, publish_payload = extract_publish_payload(packet_bytes)
                        if topic:
                            parts = topic.split('/')
                            if len(parts) >= 2 and parts[0] == 'dtu' and not device_id:
                                device_id = parts[1]
                                self.active_device_id = device_id
                                log(f"[PROXY] Discovered device_id from PUBLISH: {device_id}", level="info")

                        if publish_payload:
                            parsed_ok = SolarParser.parse_payload(publish_payload, source_topic=topic)
                            if not parsed_ok:
                                log(f"[PROXY] Failed to parse payload for topic {topic}", level="warning")

                        if qos > 0:
                            # Extract packet-id (after topic length + topic bytes)
                            topic_len_field = struct.unpack("!H", packet_bytes[pos:pos + 2])[0]
                            pid_offset = pos + 2 + topic_len_field
                            if len(packet_bytes) >= pid_offset + 2:
                                packet_id = packet_bytes[pid_offset:pid_offset + 2]
                                client_sock.sendall(b"\x40\x02" + packet_id)

                        # Real cloud sends an application-level ack after
                        # every pub/event/* publish, e.g.
                        # {"c":2,"t":"...","s":"...","i":<same i as the
                        # inverter's own message>,"e":0} on the matching
                        # sub/event/<name>_reply topic. This is separate from
                        # the MQTT PUBACK above and the local proxy never
                        # sent it -- the device's own connection-liveness
                        # logic may depend on seeing this, independent of
                        # ci/RPC traffic. The "c" code is NOT constant --
                        # confirmed from real captures it differs per event
                        # type: dev_event_post_reply=2, dev_prop_post_reply=1,
                        # dtu_prop_post_reply=21. Falls back to 2 (most
                        # commonly observed) for any event name not yet
                        # confirmed by capture.
                        EVENT_REPLY_CODE = {
                            "dev_event_post": 2,
                            "dev_prop_post": 1,
                            "dtu_prop_post": 21,
                        }
                        if ENABLE_APP_ACK and topic and "/pub/event/" in topic:
                            try:
                                event_name = topic.rsplit("/", 1)[-1]
                                event_i = None
                                event_c = None
                                event_t = ""
                                if publish_payload:
                                    try:
                                        # The payload carries a leading 0x00 byte before
                                        # the JSON, so json.loads() on the raw bytes always
                                        # raises -- slice from the first brace like
                                        # SolarParser.parse_payload does.  Without this the
                                        # envelope never parses and the ack goes out as i=0,
                                        # which the device cannot correlate to any request.
                                        brace = publish_payload.find(b"{")
                                        if brace != -1:
                                            original = json.loads(
                                                publish_payload[brace:].decode("utf-8", errors="ignore")
                                            )
                                            event_i = original.get("i")
                                            event_c = original.get("c")
                                            event_t = original.get("t", "")
                                    except Exception as parse_exc:
                                        log(
                                            f"[PROXY] Could not parse {event_name} envelope for ack: {parse_exc}",
                                            level="warning",
                                        )

                                if event_i is None:
                                    # Better to send nothing than an ack the device can't
                                    # match to one of its own outstanding messages.
                                    log(
                                        f"[PROXY] No 'i' in {event_name} envelope -- skipping app-level ack",
                                        level="warning",
                                    )
                                else:
                                    # The reply's "c" is simply the request's own "c"
                                    # (dev_event_post publishes c=2, dev_prop_post c=1),
                                    # so echoing it self-corrects for event names not in
                                    # the table.  The table is only a fallback now.
                                    reply_c = event_c
                                    if reply_c is None:
                                        reply_c = EVENT_REPLY_CODE.get(event_name)
                                        if reply_c is None:
                                            reply_c = 2
                                            log(
                                                f"[PROXY] No confirmed reply code for event '{event_name}', "
                                                f"defaulting to c=2 -- verify against a real capture if possible",
                                                level="warning",
                                            )

                                    reply_topic = topic.replace("/pub/event/", "/sub/event/") + "_reply"
                                    alphabet = string.ascii_letters + string.digits
                                    # Real cloud replies echo "t" verbatim, mint a fresh
                                    # "s", and answer with i+1 (201->202, 101->102) -- it
                                    # never repeats the request's own "i".
                                    reply_obj = {
                                        "c": reply_c,
                                        "t": event_t,
                                        "s": "".join(secrets.choice(alphabet) for _ in range(9)),
                                        "i": event_i + 1,
                                        "e": 0,
                                    }
                                    reply_payload = _encode_app_payload(reply_obj)
                                    reply_pkt = make_mqtt_publish_packet(reply_topic, reply_payload, packet_id=0)
                                    client_sock.sendall(reply_pkt)
                                    log(
                                        f"[PROXY] Sent app-level ack for {event_name} "
                                        f"(c={reply_c}, i={reply_obj['i']}, t={event_t}) to {reply_topic}",
                                        level="info",
                                    )
                            except Exception as exc:
                                log(f"[PROXY] Failed to send event-post-reply ack: {exc}", level="error")

                    # ---- SUBSCRIBE (8) ----
                    elif packet_type == 8:
                        sub_topic = ""
                        req_qos = 0
                        if len(packet_bytes) >= pos + 4:
                            topic_len = struct.unpack("!H", packet_bytes[pos + 2:pos + 4])[0]
                            if len(packet_bytes) >= pos + 4 + topic_len:
                                sub_topic = packet_bytes[pos + 4:pos + 4 + topic_len].decode('utf-8', errors='ignore')
                                req_qos = packet_bytes[pos + 4 + topic_len] if len(packet_bytes) > pos + 4 + topic_len else 0
                                log(f"[PROXY] MQTT SUBSCRIBE to topic='{sub_topic}' with requested QoS={req_qos}", level="info")
                                parts = sub_topic.split('/')
                                if len(parts) >= 2 and parts[0] == 'dtu' and not device_id:
                                    device_id = parts[1]
                                    self.active_device_id = device_id
                                    log(f"[PROXY] Discovered device_id from SUBSCRIBE: {device_id}", level="info")

                        # MQTT requires the granted QoS to be no greater than
                        # the QoS requested by the subscriber.  This inverter
                        # asks for QoS 0; granting QoS 1 made the local broker
                        # protocol-invalid and it ignored our RPC replies.
                        if len(packet_bytes) >= pos + 2:
                            packet_id = packet_bytes[pos:pos + 2]
                            granted_qos = req_qos if req_qos in (0, 1) else 0
                            client_sock.sendall(
                                b"\x90\x03" + packet_id + bytes([granted_qos])
                            )

                        # Start poller once we know the device_id. Always
                        # runs now -- it carries the required 'ci' keepalive
                        # (see _poller_loop docstring); ENABLE_RPC_POLLING
                        # only controls whether it also sends the RPC
                        # telemetry poll (confirmed to get zero replies from
                        # this bridge's own session either way). Manual
                        # sends (send_manual_rpc/send_manual_payload) keep
                        # working independent of this either way.
                        if device_id and not ha_mqtt:
                            _start_ha_mqtt()
                        if device_id and poller_thread is None:
                            poller_thread = threading.Thread(
                                target=_poller_loop, daemon=True, name="proxy_poller"
                            )
                            poller_thread.start()
                            log(
                                f"[PROXY] Poller started for device_id={device_id} "
                                f"interval={UPDATE_INTERVAL_SEC}s ci_keepalive=always rpc_poll={ENABLE_RPC_POLLING} "
                                f"first_poll_delay={'%ds after first PUBLISH' % RPC_FIRST_POLL_DELAY_SEC if ENABLE_FIRST_POLL_DELAY else 'off'}",
                                level="info"
                            )

                    # ---- PINGREQ (12) ----
                    elif packet_type == 12:
                        client_sock.sendall(b"\xd0\x00")  # PINGRESP

                    # ---- DISCONNECT (14) ----
                    elif packet_type == 14:
                        log("[PROXY] MQTT DISCONNECT received", level="info")
                        break

        except socket.timeout:
            log("[PROXY] Connection timed out (no data from inverter)", level="warning")
        except Exception as e:
            log(f"[PROXY] Client connection error: {e}", level="error")
        finally:
            poller_stop.set()
            if self.active_socket is client_sock:
                self.active_socket = None
                self.active_device_id = None
            if ha_mqtt:
                try:
                    ha_mqtt.loop_stop()
                    ha_mqtt.disconnect()
                except Exception:
                    pass
            try:
                client_sock.close()
            except Exception:
                pass
            log("[PROXY] Connection closed", level="info")


proxy_server = DummySiseliCloud()
