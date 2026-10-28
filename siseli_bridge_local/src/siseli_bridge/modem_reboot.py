"""Reboot the LAN modem (router) right after Isolated Cloud is activated.

Field procedure (verified 2026-10-23): after the dongle has hooked the real
cloud and full telemetry flows, Isolated Cloud is switched on and the bridge
restarts; the modem must then be rebooted within seconds so the dongle lands
on the local fake cloud. This module automates exactly that last step from
inside the add-on:

- On every start, the bridge records the current ISOLATE_CLOUD value in a
  small marker file under /data (next to the state cache).
- If the previous run had ISOLATE_CLOUD off and this run has it on -- i.e.
  isolation was just activated -- and MODEM_REBOOT_ON_ISOLATE is enabled and
  a modem password is configured, a daemon thread reboots the modem
  MODEM_REBOOT_DELAY_SEC seconds after the fake cloud servers are up.

The modem (TP-Link TD-W8961N) web flow mirrors the manually verified one:
GET /login.htm, POST /login.cgi with MD5-hashed credentials plus the hidden
``submit.htm?login.htm=Send`` field, then POST /form2Reboot.cgi with
``save=Reboot`` plus the hidden ``submit.htm?reboot.htm=Send`` field (without
that hidden field the firmware silently ignores the reboot). The ``reboot``
form field (factory reset) is never sent. Every failure is logged and
swallowed: a failed modem reboot must never take the bridge down.
"""

import hashlib
import http.cookiejar
import json
import os
import threading
import time
import urllib.request

from .config import (
    ISOLATE_CLOUD,
    MODEM_PASSWORD,
    MODEM_REBOOT_DELAY_SEC,
    MODEM_REBOOT_ON_ISOLATE,
    MODEM_USER,
    ROUTER_IP,
)
from .loggers import log

MARKER_FILE = "/data/modem_reboot_marker.json"


def register_isolation_state() -> bool:
    """Record this run's ISOLATE_CLOUD value; return True if the modem
    reboot should fire (isolation was just activated: off -> on)."""
    prev = None
    try:
        with open(MARKER_FILE, "r") as f:
            prev = json.load(f).get("isolate_cloud")
    except Exception:
        prev = None
    try:
        os.makedirs(os.path.dirname(MARKER_FILE), exist_ok=True)
        with open(MARKER_FILE, "w") as f:
            json.dump({"isolate_cloud": ISOLATE_CLOUD}, f)
    except Exception as exc:
        log(f"[MODEM REBOOT] could not write state marker: {exc}", level="warning")
    fire = bool(
        MODEM_REBOOT_ON_ISOLATE
        and MODEM_PASSWORD
        and ISOLATE_CLOUD
        and prev is False
    )
    if MODEM_REBOOT_ON_ISOLATE and ISOLATE_CLOUD:
        log(f"[MODEM REBOOT] isolation transition check: prev={prev} -> fire={fire}")
    return fire


def _do_reboot() -> None:
    try:
        time.sleep(MODEM_REBOOT_DELAY_SEC)
        base = f"http://{ROUTER_IP}"
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )

        def req(path: str, data: bytes | None = None) -> bytes:
            request = urllib.request.Request(
                base + path,
                data=data,
                method="POST" if data is not None else "GET",
            )
            with opener.open(request, timeout=15) as resp:
                return resp.read()

        req("/login.htm")
        # Field names are sent verbatim, exactly as the modem's own form posts
        # them (the hidden field name itself contains '?' and '.').
        login_body = (
            f"usernameEncrypt={hashlib.md5(MODEM_USER.encode()).hexdigest()}"
            f"&passwordEncrypt={hashlib.md5(MODEM_PASSWORD.encode()).hexdigest()}"
            "&loginBtn=Login&submit.htm?login.htm=Send"
        ).encode()
        req("/login.cgi", login_body)
        reboot_body = b"save=Reboot&submit.htm?reboot.htm=Send"
        resp = req("/form2Reboot.cgi", reboot_body)
        accepted = b"rebooting" in resp.lower()
        log(
            f"[MODEM REBOOT] reboot command sent to {ROUTER_IP}: "
            f"accepted={accepted} bytes={len(resp)}"
        )
    except Exception as exc:
        log(f"[MODEM REBOOT] failed: {exc}", level="warning")


def start_reboot_thread() -> None:
    log(
        f"[MODEM REBOOT] Isolated Cloud just activated; "
        f"modem reboot scheduled in {MODEM_REBOOT_DELAY_SEC}s"
    )
    threading.Thread(target=_do_reboot, daemon=True).start()
