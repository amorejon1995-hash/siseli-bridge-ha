import base64
import binascii
import ipaddress
import json
import logging
import os
import signal
import subprocess
import threading
import time
import warnings
from typing import Optional

from scapy.all import ARP, Ether, IP, Raw, TCP, UDP, AsyncSniffer, getmacbyip, sendp, send  # type: ignore

from .config import *
from . import config as _config_module
from .loggers import log, log_kv, json_log, log_payload_preview, printable_text_preview, hex_preview
from .sensors import SENSORS, get_grouped_sensor_keys
from . import state as _state
# Ensure required state attributes exist in case state.py is empty or outdated on HA
if not hasattr(_state, "LAST_STATE"):
    _state.LAST_STATE = {}
if not hasattr(_state, "DISCOVERY_PUBLISHED"):
    _state.DISCOVERY_PUBLISHED = False
if not hasattr(_state, "PUBLISHED_SENSOR_KEYS"):
    _state.PUBLISHED_SENSOR_KEYS = set()

from .mqtt import client, start_mqtt, publish_discovery, availability_topic_for_group
from .parsers import SolarParser, extract_publish_payload, append_stream_data, mqtt_type_name, SEEN_MQTT_TOPICS
from .proxy import proxy_server
from .http_proxy import http_proxy_server
from . import modem_reboot as _modem_reboot

_LOCAL_IP: Optional[str] = None

warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)

def norm_mac(mac: Optional[str]) -> Optional[str]:
    if not mac:
        return None
    return mac.strip().lower().replace("-", ":")

def send_layer2(frame, iface: Optional[str] = None) -> None:
    if iface:
        sendp(frame, verbose=False, iface=iface)
    else:
        sendp(frame, verbose=False)

INV_MAC: Optional[str] = None
RTR_MAC: Optional[str] = None
sniffer: Optional[AsyncSniffer] = None

from .config import STATE_CACHE_FILE

try:
    if os.path.exists(STATE_CACHE_FILE):
        with open(STATE_CACHE_FILE, "r") as f:
            _cached = json.load(f)
        # Only restore keys this version still knows about. Older versions
        # persisted many fields that no longer exist (unmapped nulls and
        # removed calculated sensors); without this filter they would be
        # reloaded into LAST_STATE forever and republished to HA.
        _kept = {k: v for k, v in _cached.items() if k in SENSORS}
        _dropped = len(_cached) - len(_kept)
        _state.LAST_STATE.update(_kept)
        if _dropped:
            log(f"[CACHE] Dropped {_dropped} stale key(s) from {STATE_CACHE_FILE}")
except Exception as e:
    log(f"[CACHE] Error loading state: {e}", level="error")

KNOWN_INVERTER_MACS = set()
KNOWN_ROUTER_MACS = set()
LAST_PACKET_TS = 0.0

class ArpSpoofer:
    def resolve_macs(self) -> None:
        global INV_MAC, RTR_MAC

        INV_MAC = norm_mac(INVERTER_MAC_CFG) or INV_MAC
        RTR_MAC = norm_mac(ROUTER_MAC_CFG) or RTR_MAC

        while _state.RUNNING_EVENT.is_set() and (not INV_MAC or not RTR_MAC):
            if not INV_MAC:
                INV_MAC = norm_mac(getmacbyip(INVERTER_IP))
            if not RTR_MAC:
                RTR_MAC = norm_mac(getmacbyip(ROUTER_IP))

            if not INV_MAC or not RTR_MAC:
                log("[ARP] Waiting for MAC addresses...", level="info")
                time.sleep(2)

        if _state.RUNNING_EVENT.is_set():
            log(f"[ARP] Inverter MAC: {INV_MAC}", level="info")
            log(f"[ARP] Router MAC:   {RTR_MAC}", level="info")

    def run(self) -> None:
        self.resolve_macs()
        if not _state.RUNNING_EVENT.is_set():
            return

        log(f"[ARP] Interception ACTIVE: {INVERTER_IP} <-> {ROUTER_IP}", level="info")

        while _state.RUNNING_EVENT.is_set():
            try:
                # Always spoof the inverter (tell it the router is at HA MAC)
                send_layer2(Ether(dst=INV_MAC) / ARP(op=2, pdst=INVERTER_IP, psrc=ROUTER_IP, hwdst=INV_MAC), SNIFF_IFACE)
                
                # Only spoof the router (claiming the inverter's IP) if we are NOT isolating the cloud
                if LAST_PACKET_TS > 0 and not ISOLATE_CLOUD:
                    send_layer2(Ether(dst=RTR_MAC) / ARP(op=2, pdst=ROUTER_IP, psrc=INVERTER_IP, hwdst=RTR_MAC), SNIFF_IFACE)
                
                # Send standard ARP query from HA to inverter to keep HA's own ARP table correct and healthy
                send(ARP(op=1, pdst=INVERTER_IP), verbose=False)
            except Exception as exc:
                log(f"[ARP ERROR] {exc}", level="error")

            time.sleep(2)


arp_spoofer = ArpSpoofer()


def handle_inverter_tcp_packet(pkt) -> None:
    if Raw not in pkt:
        return

    payload = bytes(pkt[Raw].load)
    if not payload:
        return

    flow_key = (pkt[IP].src, int(pkt[TCP].sport), pkt[IP].dst, int(pkt[TCP].dport))
    seq = int(pkt[TCP].seq)

    packets = append_stream_data(flow_key, seq, payload)

    if not packets:
        return

    for packet in packets:
        if LOG_VERBOSE:
            ptype = mqtt_type_name(packet[0])
            log(
                f"[MQTT PACKET] {pkt[IP].src}:{int(pkt[TCP].sport)} -> "
                f"{pkt[IP].dst}:{int(pkt[TCP].dport)} type={ptype} len={len(packet)} "
                f"first16={packet[:16].hex()}"
            )

        if ((packet[0] >> 4) & 0x0F) == 3:
            topic, publish_payload = extract_publish_payload(packet)
            if topic is not None:
                count = SEEN_MQTT_TOPICS.get(topic, 0) + 1
                SEEN_MQTT_TOPICS[topic] = count
                if LOG_MQTT_TOPICS:
                    log_kv("[MQTT TOPIC]", topic=topic, seen_count=count, payload_len=len(publish_payload or b""))
            if LOG_VERBOSE and topic is not None:
                log(f"[MQTT PUBLISH] topic={topic} payload_len={len(publish_payload or b'')}")
            if publish_payload and LOG_MQTT_PAYLOAD_PREVIEW:
                log_payload_preview("[MQTT PAYLOAD]", publish_payload, topic=topic)
            if publish_payload:
                parsed_ok = SolarParser.parse_payload(publish_payload, source_topic=topic)
                if not parsed_ok and LOG_UNPARSED_PUBLISH:
                    log_payload_preview("[MQTT PAYLOAD NOT PARSED]", publish_payload, topic=topic)


def handle_cloud_to_inverter_packet(pkt) -> None:
    if Raw not in pkt:
        return

    payload = bytes(pkt[Raw].load)
    if not payload:
        return

    flow_key = (pkt[IP].src, int(pkt[TCP].sport), pkt[IP].dst, int(pkt[TCP].dport))
    seq = int(pkt[TCP].seq)

    packets = append_stream_data(flow_key, seq, payload)

    if not packets:
        return

    for packet in packets:
        ptype = mqtt_type_name(packet[0])
        log(
            f"[DOWNLINK MQTT PACKET] {pkt[IP].src}:{int(pkt[TCP].sport)} -> "
            f"{pkt[IP].dst}:{int(pkt[TCP].dport)} type={ptype} len={len(packet)} "
            f"first16={packet[:16].hex()}"
        )

        if ((packet[0] >> 4) & 0x0F) == 3:
            topic, publish_payload = extract_publish_payload(packet)
            if topic is not None:
                log_kv("[DOWNLINK MQTT TOPIC]", topic=topic, payload_len=len(publish_payload or b""))
            if publish_payload:
                log_payload_preview("[DOWNLINK MQTT PAYLOAD]", publish_payload, topic=topic)
                _save_rpc_template(topic, publish_payload)


def _write_template_file(path: str, rpc: dict, kind: str) -> None:
    """Atomically persist a genuine cloud RPC payload to `path`."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        temp_file = f"{path}.tmp"
        with open(temp_file, "w", encoding="utf-8") as handle:
            json.dump(rpc, handle, separators=(",", ":"))
        os.replace(temp_file, path)
        log(f"[LEARN] Saved genuine cloud RPC '{kind}' template to {path}", level="info")
    except OSError as exc:
        log(f"[LEARN] Could not save RPC '{kind}' template: {exc}", level="warning")


def _save_rpc_template(topic: Optional[str], payload: bytes) -> None:
    """Persist real cloud downlinks for later isolated operation.

    Two distinct dev_rpc body shapes are captured, each to its own file:
      - "ct": the telemetry batch-read command (list of block requests)
      - "ci": a capability/keepalive handshake (base64 "CSPA..." blob),
        acknowledged by the dongle with a generic "MAPAACK" regardless of
        content, but still worth replaying to keep the session healthy.
    """
    if not topic or not topic.endswith("/sub/service/dev_rpc"):
        return
    # Genuine app payloads carry a leading 0x00 byte (APP_PAYLOAD_NUL_PREFIX);
    # without stripping it, json.loads fails and nothing is ever learned.
    if payload[:1] == b"\x00":
        payload = payload[1:]
    try:
        rpc = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return
    if not isinstance(rpc, dict) or rpc.get("c") != 5 or not isinstance(rpc.get("b"), dict):
        return

    body = rpc["b"]

    commands = body.get("ct")
    if isinstance(commands, list) and commands:
        _write_template_file(RPC_TEMPLATE_FILE, rpc, "ct")
        return

    ci_value = body.get("ci")
    if isinstance(ci_value, str) and ci_value:
        # Only learn the CMSG/INV-HB heartbeat variant for periodic replay.
        # The other family (CSPA...CSIPAM) is a config-write command carrying
        # a real setpoint value + checksum in its trailing bytes -- replaying
        # THAT one on a timer would silently re-apply a stale setting to the
        # inverter every cycle. Decode-check the base64 payload before saving.
        try:
            decoded = base64.b64decode(ci_value, validate=False)
        except (ValueError, binascii.Error):
            decoded = b""
        if decoded.startswith(b"CMSG"):
            _write_template_file(CI_RPC_TEMPLATE_FILE, rpc, "ci (CMSG heartbeat)")
        else:
            log(
                "[LEARN] Skipped saving 'ci' as keepalive template: not a CMSG "
                "heartbeat (looks like a config-write command, e.g. CSPA/CSIPAM)",
                level="info",
            )
        return


def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return None


def packet_callback(pkt) -> None:
    global INV_MAC, RTR_MAC, LAST_PACKET_TS, INVERTER_IP

    LAST_PACKET_TS = time.time()

    if IP not in pkt or Ether not in pkt:
        return

    src_mac = norm_mac(pkt[Ether].src)
    src_ip = pkt[IP].src
    dst_ip = pkt[IP].dst

    # Track dynamic inverter IP by MAC address if known
    if INV_MAC and src_mac == INV_MAC and src_ip != INVERTER_IP:
        # Validate the sniffed IP is a real, private IPv4 address before using
        # it in a subprocess call.  Rejects broadcast/multicast/loopback and
        # any malformed value that could inject shell arguments.
        try:
            parsed = ipaddress.ip_address(src_ip)
        except ValueError:
            parsed = None
        if parsed and parsed.is_private and not parsed.is_multicast and not parsed.is_loopback:
            old_ip = INVERTER_IP
            INVERTER_IP = src_ip
            # Update the config module's copy so other modules that imported
            # from config see the new IP (the `from .config import *` in this
            # module creates a by-value copy that is now stale, but other
            # modules importing config directly will pick up the mutation).
            _config_module.INVERTER_IP = src_ip
            log(f"[ARP] Inverter MAC {INV_MAC} changed IP from {old_ip} to {src_ip}", level="info")
            # Re-apply redirection rule matching the new IP dynamically.
            # Use flush + add instead of delete-by-expression (nft delete rule
            # by handle is the reliable path; delete by full expression often
            # silently fails).  Each command in its own try so a missing nft
            # binary does not skip the add.
            if ISOLATE_CLOUD:
                for cmd in [
                    ["nft", "flush", "chain", "ip", "siseli_nat", "prerouting"],
                    ["nft", "add", "rule", "ip", "siseli_nat", "prerouting",
                     "ip", "saddr", src_ip, "tcp", "dport", str(TARGET_PORT),
                     "redirect", "to", f":{LISTEN_PORT}"],
                ]:
                    try:
                        subprocess.run(cmd, capture_output=True, timeout=5)
                    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                        pass
        else:
            log(f"[ARP] Ignoring invalid/non-private IP from inverter MAC: {src_ip}", level="warning")

    if src_ip == INVERTER_IP and not INV_MAC:
        INV_MAC = src_mac
    if dst_ip == INVERTER_IP and not RTR_MAC:
        RTR_MAC = src_mac

    if src_ip == INVERTER_IP and src_mac:
        KNOWN_INVERTER_MACS.add(src_mac)
    if dst_ip == INVERTER_IP and src_mac:
        KNOWN_ROUTER_MACS.add(src_mac)

    if LOG_VERBOSE and (src_ip == INVERTER_IP or dst_ip == INVERTER_IP):
        if TCP in pkt:
            proto = f"TCP {pkt[TCP].sport} -> {pkt[TCP].dport}"
        elif UDP in pkt:
            proto = f"UDP {pkt[UDP].sport} -> {pkt[UDP].dport}"
        elif pkt.haslayer("ICMP"):
            from scapy.all import ICMP
            proto = f"ICMP type={pkt[ICMP].type} code={pkt[ICMP].code}"
        else:
            proto = "OTHER"
        log(f"[X-RAY] {src_ip} ({src_mac}) -> {dst_ip} [{proto}]")

    # The inverter also talks HTTP to a second cloud host (8.212.16.60:80)
    # that we have never decoded -- the config-write commands (Input Range,
    # Input Frequency Range, etc.) are suspected to live here rather than in
    # the MQTT dev_rpc channel. Dump any payload seen on this host so we can
    # correlate it with a command submitted from the web UI.
    HTTP_API_HOST = "8.212.16.60"
    if TCP in pkt and Raw in pkt and (src_ip == HTTP_API_HOST or dst_ip == HTTP_API_HOST):
        raw = bytes(pkt[Raw].load)
        direction = f"{src_ip}:{pkt[TCP].sport} -> {dst_ip}:{pkt[TCP].dport}"
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = repr(raw)
        log(
            f"[HTTP CAPTURE] {direction} len={len(raw)} hex={raw.hex()} text={text!r}",
            level="info",
        )

    if src_ip == INVERTER_IP:
        if INV_MAC and src_mac != INV_MAC:
            return

        if UDP in pkt and int(pkt[UDP].dport) == 53:
            from scapy.all import DNS, DNSQR, DNSRR
            if pkt.haslayer(DNSQR):
                qname = pkt[DNSQR].qname.decode('utf-8', errors='ignore')
                if ("siseli" in qname or "shineset" in qname or "powmr" in qname) and ISOLATE_CLOUD:
                    # Return OUR OWN IP so the inverter connects directly to us.
                    # NOTE: this used to exclude any hostname containing "access"
                    # (i.e. dtu.access.solar.siseli.com), on the assumption that
                    # host only carried config-write commands. It also carries the
                    # actual telemetry block (via POST /dtu/devices/findSingle's
                    # "cmdOut" field) -- excluding it left isolated mode with no
                    # telemetry source at all. See http_proxy.py for the HTTP-side
                    # responder that now backs this redirect.
                    global _LOCAL_IP
                    if not _LOCAL_IP:
                        _LOCAL_IP = get_local_ip()
                    resolved_ip = _LOCAL_IP or TARGET_HOST
                    dns_resp = IP(dst=INVERTER_IP, src=pkt[IP].dst)/UDP(dport=pkt[UDP].sport, sport=53)/DNS(
                        id=pkt[DNS].id,
                        qr=1,
                        aa=1,
                        qd=pkt[DNS].qd,
                        an=DNSRR(rrname=pkt[DNSQR].qname, type='A', rclass='IN', ttl=60, rdata=resolved_ip)
                    )
                    send_layer2(Ether(dst=INV_MAC)/dns_resp, SNIFF_IFACE)
                    log(f"[DNS HIJACK] Spoofed DNS response for {qname} -> {resolved_ip} (src={pkt[IP].dst})", level="info")
                    return

        intercepted = False
        # MQTT traffic redirected to the local proxy is parsed there.  Parsing
        # it again from the packet sniffer duplicated state writes and could
        # make Home Assistant appear to receive stalled/out-of-order updates.
        if TCP in pkt and int(pkt[TCP].dport) == TARGET_PORT:
            if not ISOLATE_CLOUD:
                try:
                    handle_inverter_tcp_packet(pkt)
                except Exception as exc:
                    log(f"[TCP PARSE ERROR] {exc}", level="error")
            elif dst_ip == TARGET_HOST:
                # DROP — do not forward to cloud
                intercepted = True

        if AUTO_INTERCEPT and RTR_MAC and not intercepted and dst_ip != get_local_ip():
            try:
                fwd_pkt = Ether(dst=RTR_MAC) / pkt[IP]
                send_layer2(fwd_pkt, SNIFF_IFACE)
            except Exception as exc:
                log(f"[FWD ERROR] inverter->router {exc}", level="error")
        return

    if dst_ip == INVERTER_IP:
        if RTR_MAC and src_mac != RTR_MAC:
            return

        is_cloud_packet = False
        if TCP in pkt and int(pkt[TCP].sport) == 1883:
            is_cloud_packet = True
            try:
                handle_cloud_to_inverter_packet(pkt)
            except Exception as exc:
                log(f"[TCP PARSE ERROR DOWNLINK] {exc}", level="error")

        if AUTO_INTERCEPT and INV_MAC and not (ISOLATE_CLOUD and is_cloud_packet):
            try:
                fwd_pkt = Ether(dst=INV_MAC) / pkt[IP]
                send_layer2(fwd_pkt, SNIFF_IFACE)
            except Exception as exc:
                log(f"[FWD ERROR] router->inverter {exc}", level="error")


def health_logger() -> None:
    while _state.RUNNING_EVENT.is_set():
        time.sleep(30)
        age = time.time() - LAST_PACKET_TS if LAST_PACKET_TS else -1
        if age < 0:
            log("[HEALTH] No packets captured yet", level="info")
        else:
            inv_list = sorted(x for x in KNOWN_INVERTER_MACS if x)
            rtr_list = sorted(x for x in KNOWN_ROUTER_MACS if x)
            log(
                f"[HEALTH] Last packet seen {int(age)}s ago; inverter_macs={inv_list}; router_macs={rtr_list}",
                level="info",
            )


def shutdown(*_args) -> None:
    global sniffer

    if not _state.RUNNING_EVENT.is_set():
        return

    _state.RUNNING_EVENT.clear()

    try:
        if sniffer is not None:
            sniffer.stop()
    except Exception:
        pass

    try:
        proxy_server.stop()
    except Exception:
        pass

    try:
        http_proxy_server.stop()
    except Exception:
        pass

    # --- Network teardown (Fix 3) -------------------------------------------
    # Restore the real ARP mappings on both the inverter and the router so
    # the inverter is not left blackholed after the add-on stops.  Send a
    # few gratuitous ARP replies with the correct MACs.
    try:
        if INV_MAC and RTR_MAC:
            for _ in range(3):
                # Tell the inverter: "ROUTER_IP is at RTR_MAC" (correct mapping)
                send_layer2(
                    Ether(dst=INV_MAC, src=RTR_MAC) / ARP(
                        op=2, psrc=ROUTER_IP, hwsrc=RTR_MAC,
                        pdst=INVERTER_IP, hwdst=INV_MAC,
                    ),
                    SNIFF_IFACE,
                )
                # Tell the router: "INVERTER_IP is at INV_MAC" (correct mapping)
                send_layer2(
                    Ether(dst=RTR_MAC, src=INV_MAC) / ARP(
                        op=2, psrc=INVERTER_IP, hwsrc=INV_MAC,
                        pdst=ROUTER_IP, hwdst=RTR_MAC,
                    ),
                    SNIFF_IFACE,
                )
                time.sleep(0.2)
            log("[ARP] Restored correct ARP mappings on shutdown", level="info")
    except Exception as exc:
        log(f"[ARP] Error restoring ARP on shutdown: {exc}", level="warning")

    # Clean up nftables/iptables rules and loopback alias added by run.sh.
    # This mirrors the startup cleanup in run.sh so a stop-then-start cycle
    # does not leave stale redirect rules.  Each command is in its own try so
    # a missing binary (e.g. nft on an iptables-only host) does not skip the
    # rest of the cleanup.
    for cmd in [
        ["nft", "delete", "table", "ip", "siseli_nat"],
        ["nft", "delete", "table", "ip", "siseli_filter"],
        ["iptables", "-t", "nat", "-D", "PREROUTING",
         "-s", INVERTER_IP, "-p", "tcp", "--dport", str(TARGET_PORT),
         "-j", "REDIRECT", "--to-ports", str(LISTEN_PORT)],
        ["iptables", "-D", "OUTPUT", "-p", "icmp",
         "--icmp-type", "redirect", "-j", "DROP"],
        ["ip", "addr", "del", f"{TARGET_HOST}/32", "dev", "lo"],
    ]:
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    log("[NET] Cleaned up nftables/iptables/loopback rules on shutdown", level="info")

    try:
        for group in get_grouped_sensor_keys():
            client.publish(availability_topic_for_group(group), "offline", retain=True)
        client.disconnect()
        client.loop_stop()
    except Exception:
        pass

    log("[Bridge] Stopped")


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

VERSION = os.getenv("SISELI_BRIDGE_VERSION", "2.5.37")  # Keep in sync with config.yaml


if __name__ == "__main__":
    from .config import validate_config
    validate_config()
    _modem_reboot_fire = _modem_reboot.register_isolation_state()
    log(f"--- Siseli Inverter Bridge {VERSION} ---")
    log(f"[Config] INVERTER_IP={INVERTER_IP} ROUTER_IP={ROUTER_IP}")
    log(f"[Config] TARGET={TARGET_HOST}:{TARGET_PORT} MQTT={MQTT_HOST}:{MQTT_PORT}")
    log(f"[Config] AUTO_INTERCEPT={AUTO_INTERCEPT} LISTEN_PORT={LISTEN_PORT}")
    log(f"[Config] INVERTER_COUNT={INVERTER_COUNT}")
    log(f"[Config] BATTERY_COUNT={BATTERY_COUNT} BATTERY_CAPACITY_PER_BATTERY_AH={BATTERY_CAPACITY_PER_BATTERY_AH}")
    log(f"[Config] DEVICE_NAME={DEVICE_NAME} MANUFACTURER={MANUFACTURER}")
    log(f"[Config] STATE_TOPIC={STATE_TOPIC}")
    log(f"[Config] SNIFF_IFACE={SNIFF_IFACE or 'auto'}")
    log(f"[Config] DEBUG_FLAGS verbose={LOG_VERBOSE} blocks={LOG_BLOCKS} state_diff={LOG_STATE_DIFF} state_snapshot={LOG_STATE_SNAPSHOT} raw_json={LOG_RAW_JSON} clean_state={LOG_CLEAN_STATE} mqtt_topics={LOG_MQTT_TOPICS} payload_preview={LOG_MQTT_PAYLOAD_PREVIEW} unparsed={LOG_UNPARSED_PUBLISH} stream_events={LOG_STREAM_EVENTS} null_targets={LOG_NULL_TARGETS}")

    for key in SENSORS.keys():
        _state.LAST_STATE.setdefault(key, None)

    start_mqtt()
    
    proxy_server.start()

    if ISOLATE_CLOUD:
        # Only needed when we're actually hijacking DNS for the HTTP domain
        # (dtu.access.solar.siseli.com) -- see the DNS HIJACK block above.
        # In non-isolated mode the dongle keeps talking to the real cloud
        # over HTTP and [HTTP CAPTURE] logging still shows that traffic.
        http_proxy_server.start()

        if _modem_reboot_fire:
            _modem_reboot.start_reboot_thread()

    if AUTO_INTERCEPT:
        threading.Thread(target=arp_spoofer.run, daemon=True).start()
        wait_start = time.time()
        while _state.RUNNING_EVENT.is_set() and time.time() - wait_start < 15 and (not INV_MAC or not RTR_MAC):
            time.sleep(1)
    else:
        INV_MAC = norm_mac(INVERTER_MAC_CFG)
        RTR_MAC = norm_mac(ROUTER_MAC_CFG)
        log("[ARP] AUTO_INTERCEPT disabled; relying on existing network redirection")

    threading.Thread(target=health_logger, daemon=True).start()

    sniff_kwargs = {
        "filter": f"ip host {INVERTER_IP}",
        "prn": packet_callback,
        "store": False,
    }
    if SNIFF_IFACE:
        sniff_kwargs["iface"] = SNIFF_IFACE

    sniffer = AsyncSniffer(**sniff_kwargs)
    sniffer.start()
    log("[Bridge] Sniffer started", level="info")

    try:
        while _state.RUNNING_EVENT.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
