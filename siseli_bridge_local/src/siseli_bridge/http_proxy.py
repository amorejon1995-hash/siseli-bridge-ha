"""
Local HTTP server that impersonates the Siseli cloud's HTTP API
(dtu.access.solar.siseli.com), for use when ISOLATE_CLOUD=true.

Why this exists: the MQTT channel to the dongle only carries a device
handshake/registration payload (ip/mac/model/firmware) -- the *actual*
telemetry block is delivered separately, via HTTP POST to
/dtu/devices/findSingle, in the "cmdOut" field of the request body (base64,
same block format as the MQTT "OgBU" telemetry block). Before this module
existed, the DNS hijack in core.py deliberately skipped this hostname
(it contains "access"), so in isolated mode the dongle got no answer on
this channel and no telemetry ever reached the bridge.

Endpoints implemented, matching what real captured traffic showed:
  POST /dtu/checkin              -- device announces itself; ack only
  POST /dtu/devices/findSingle   -- telemetry poll; cmdOut carries the block
  GET  /dtu/servers/mqtt         -- tells the dongle which MQTT broker to use
"""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from typing import Optional

from .config import HTTP_LISTEN_PORT, ENABLE_HTTP_REPORT_PERIOD, HTTP_REPORT_PERIOD_SEC
from .loggers import log
from .parsers import SolarParser
from . import config as _config_module


def _ok_json(handler: "SiseliHTTPHandler", payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json;charset=UTF-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass
    handler._responded = True  # type: ignore[attr-defined]


def _build_checkin_data() -> dict:
    """Assemble the /dtu/checkin response the dongle expects.

    Mirrors the real cloud's captured shape (timeInText / timeInMills /
    utcOffset / outbound / gatherProtocol) and injects performanceReportPeriod
    from HTTP_REPORT_PERIOD_SEC. NOTE: firmware v1.44.5 ignores this override
    (dev_event_post stays ~60s) and only requests /dtu/checkin on a cold boot,
    so this rarely even reaches the dongle -- see config.py. Kept for a
    possible future firmware. Field omitted when disabled or period is 0.
    """
    now = datetime.now(timezone.utc).astimezone()
    offset = now.strftime("%z")
    offset_colon = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
    data = {
        "timeInText": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}" + offset_colon,
        "timeInMills": str(int(time.time() * 1000)),
        "utcOffset": offset_colon,
        "outbound": 1,
        "firmware": None,
        "gatherProtocol": {
            "autoAdaptive": 0,
            "verCode": 4,
            "verText": "1.0.0",
            "verState": 10,
            "protocolNo": "MH7139274",
            "forceUpdate": 0,
        },
    }
    if ENABLE_HTTP_REPORT_PERIOD and HTTP_REPORT_PERIOD_SEC > 0:
        data["performanceReportPeriod"] = HTTP_REPORT_PERIOD_SEC * 1000
        log(
            f"[HTTP PROXY] checkin performanceReportPeriod="
            f"{HTTP_REPORT_PERIOD_SEC * 1000}ms ({HTTP_REPORT_PERIOD_SEC}s)",
            level="info",
        )
    return {"code": 0, "msg": "Success", "data": data}


def _auth_username(handler: "SiseliHTTPHandler") -> str:
    """Extract the device serial from the dongle's HTTP Basic Auth header.

    The real cloud never enforces this (any value works), but the dongle
    always sends it, and it's a more reliable source for the device serial
    than waiting on the separate MQTT CONNECT (which may not have happened
    yet -- checkin/findSingle can fire before the MQTT session starts).
    """
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="ignore")
            return decoded.split(":", 1)[0]
        except Exception:
            return ""
    return ""


class SiseliHTTPHandler(BaseHTTPRequestHandler):
    server_version = "nginx/1.20.1"  # matches the real cloud's Server header
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence default stderr access log
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _check_source_ip(self) -> bool:
        """Reject requests from IPs other than the configured inverter.

        On a host_network:true container binding 0.0.0.0:80, any LAN host
        could POST to /dtu/devices/findSingle and inject telemetry.  This
        check ensures only the inverter's source IP is accepted.
        """
        client_ip = self.client_address[0] if self.client_address else ""
        inverter_ip = _config_module.INVERTER_IP
        if client_ip != inverter_ip:
            log(
                f"[HTTP PROXY] Rejected request from {client_ip} "
                f"(expected inverter {inverter_ip})",
                level="warning",
            )
            try:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                self._responded = True
            except Exception:
                pass
            return False
        return True

    def do_POST(self):
        self._responded = False
        try:
            if not self._check_source_ip():
                return
            if self.path == "/dtu/checkin":
                self._read_body()
                dev_sn = _auth_username(self) or "unknown"
                log(f"[HTTP PROXY] /dtu/checkin from devSN={dev_sn}", level="info")
                _ok_json(self, _build_checkin_data())
                return

            if self.path == "/dtu/devices/findSingle":
                body = self._read_body()
                try:
                    parsed = json.loads(body.decode("utf-8", errors="ignore"))
                except Exception:
                    parsed = {}

                cmd_no = parsed.get("cmdNo", "")
                cmd_out = parsed.get("cmdOut", "")
                dev_sn = _auth_username(self) or "unknown"
                log(
                    f"[HTTP PROXY] findSingle devSN={dev_sn} cmdNo={cmd_no} cmdOut_len={len(cmd_out)}",
                    level="info",
                )

                if cmd_out:
                    # Reuse the existing block-walking parser (SolarParser._walk_for_blocks)
                    # by wrapping the HTTP field names (cmdNo/cmdOut) into the same
                    # {"cn": ..., "co": ...} shape already used on the MQTT side --
                    # see proxy.py's _send_rpc_request, which builds "ct": [{"cn": ...}].
                    synthetic = json.dumps(
                        {"b": {"ct": [{"cn": cmd_no, "co": cmd_out}]}}
                    ).encode("utf-8")
                    ok = SolarParser.parse_payload(
                        synthetic, source_topic="http:/dtu/devices/findSingle"
                    )
                    if not ok:
                        log(
                            "[HTTP PROXY] findSingle payload did not parse into any state "
                            f"(cmdNo={cmd_no})",
                            level="warning",
                        )

                _ok_json(self, {"code": 0, "msg": "Success", "data": {"devSN": dev_sn}})
                return

            self._read_body()
            log(f"[HTTP PROXY] Unhandled POST path: {self.path}", level="warning")
            _ok_json(self, {"code": 0, "msg": "Success", "data": {}})
        except Exception as exc:
            log(f"[HTTP PROXY] POST error: {exc}", level="error")
            if not getattr(self, "_responded", False):
                _ok_json(self, {"code": -1, "msg": "Error", "data": {}})

    def do_GET(self):
        self._responded = False
        try:
            if not self._check_source_ip():
                return
            if self.path == "/dtu/servers/mqtt":
                # Point the dongle back at the (already DNS-hijacked) MQTT broker
                # hostname so it reconnects there too, same shape as the real
                # cloud's response captured in [HTTP CAPTURE] logs.
                mqtt_entry = {
                    "host": "hongkong.broker.mqtt.solar.siseli.com",
                    "port": 1883,
                    "ssl": 0,
                    "sslCertificate": None,
                }
                _ok_json(
                    self,
                    {
                        "code": 0,
                        "msg": "Success",
                        "data": {"main": mqtt_entry, "mains": [mqtt_entry]},
                    },
                )
                return

            log(f"[HTTP PROXY] Unhandled GET path: {self.path}", level="warning")
            _ok_json(self, {"code": 0, "msg": "Success", "data": {}})
        except Exception as exc:
            log(f"[HTTP PROXY] GET error: {exc}", level="error")
            if not getattr(self, "_responded", False):
                _ok_json(self, {"code": -1, "msg": "Error", "data": {}})


class HTTPProxyServer:
    def __init__(self):
        self._httpd: Optional[ThreadingHTTPServer] = None

    def start(self) -> None:
        try:
            self._httpd = ThreadingHTTPServer(("0.0.0.0", HTTP_LISTEN_PORT), SiseliHTTPHandler)
            threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
            log(f"[HTTP PROXY] Dummy Siseli HTTP API started on port {HTTP_LISTEN_PORT}", level="info")
            if ENABLE_HTTP_REPORT_PERIOD and HTTP_REPORT_PERIOD_SEC > 0:
                log(
                    f"[HTTP PROXY] performanceReportPeriod armed at "
                    f"{HTTP_REPORT_PERIOD_SEC}s -- will be sent on the next "
                    f"/dtu/checkin (dongle only checks in on a COLD BOOT, not "
                    f"on MQTT reconnect; firmware v1.44.5 is known to ignore it)",
                    level="info",
                )
        except Exception as exc:
            log(
                f"[HTTP PROXY] Failed to start HTTP server on port {HTTP_LISTEN_PORT}: {exc} "
                "(port 80 needs root or CAP_NET_BIND_SERVICE -- check the add-on's container privileges)",
                level="error",
            )

    def stop(self) -> None:
        if self._httpd:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass


http_proxy_server = HTTPProxyServer()
