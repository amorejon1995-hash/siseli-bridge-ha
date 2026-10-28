# Changelog

## [2.9.5] - 2026-10-24

### Changed

- `SISELI_DTU_ID` default is now the portal's real internal DTU id (410736032810500114), discovered and verified live on 2.9.4 — the Restart button calls the portal directly without the discovery round-trip. The discovery remains as a fallback if the configured id is ever rejected.

## [2.9.4] - 2026-10-24

### Fixed

- **DTU id discovery, final mapping**: the live `/device/list` record (v2.9.3 log) is the inverter's device record with the DTU data embedded — the serial sits in `dtuDtuid`/`dtuName` and the restart identifier is the record's **`dtuId`** field (410736032810500114), not its `id`. The discovery walker now matches the serial under `dtuDtuid` (or `dtuName` with a `dtuId` present) and extracts `dtuId` with priority over `id`.

## [2.9.3] - 2026-10-24

### Fixed

- **DTU id discovery, third pass**: the portal's `/device/list` contract is now pinned by its own validation errors — `page` is an Integer and `count` (page size) is required (`pageSize` is ignored). The discovery ladder is exactly those two shapes: with and without `stationId`, both as `{"page": 1, "count": 50}`.

## [2.9.2] - 2026-10-24

### Fixed

- **DTU id discovery, second pass**: `/device/list` requires a `page` argument (validation error seen live on 2.9.1). The discovery now walks a ladder of request shapes (`page`/`pageSize` flat, then nested `page` objects) and stops at the first response containing the DTU record; list-failure diagnostics now keep up to 1500 characters so the portal's full validation errors are visible in the log.

## [2.9.1] - 2026-10-24

### Fixed

- **DTU restart id discovery**: the portal rejects the DTU serial as `dtuId` (code 20101, verified on the first live press). On that specific rejection the cloud client now discovers the portal's internal DTU id inside its own session (`POST /device/list`, matching the record by the DTU serial the bridge already knows), logs the discovered id (`[RESTART] discovered dtuId=... via /device/list`), and retries the restart once with it. The happy path is unchanged; the discovery adds no calls when the configured id works.

## [2.9.0] - 2026-10-24

### Added

- **"Restart DTU" button** on the Siseli device in Home Assistant. The command was captured live from the manufacturer's cloud (2026-10-24 09:15:11 EDT portal press): a body-less envelope `{"c":26,...,"i":2603}` on the `dtu/<id>/sub/service/dtu_rpc` topic, immediately followed by the dongle's MQTT DISCONNECT and reboot. In normal mode the button calls the portal's own signed endpoint `POST /remote/dtu/restart?dtuId=...` through cloud_api (same IOT-Open machinery as parameter writes); in isolated mode it injects the captured c=26 envelope into the local session through the same send path as parameter writes. The press is gated by ENABLE_PARAM_CONTROLS and throttled to one press per 30 s. The portal DTU id rides in the new `SISELI_DTU_ID` option (default: the DTU serial, the platform's visible DtuID) and is verified at first use.


## [2.8.0] - 2026-10-24

### Added

- **Operation mode label + raw code**: frame byte 32 code 2 is now decoded as "Mains Mode" (cross-checked live against the portal's Data Overview, which showed "Mains Mode" while Home Assistant displayed "Unknown (2)"), and a new diagnostic sensor exposes the raw `mode_code` so future codes can be correlated with the portal before their labels are added. A full audit of the portal's Data Overview against the parser confirmed the remaining unparsed portal fields (system run time, controller connection/charging status texts, battery-full flag and the four alarm flags) do not travel in the inverter's VPHBQ frame — the cloud derives or computes them — so no offsets were invented for them.


## [2.7.1] - 2026-10-24

### Changed

- **ENABLE_RPC_POLLING frozen OFF** at the owner's order: removed from the add-on panel (options and schema) and pinned as a literal in run.sh, same treatment as the 26 options frozen in 2.5.43. Verified unnecessary in production: with it off, telemetry stays alive on live polling (genuine INV-HB every 2 s) plus the always-on session keepalive, which no longer depends on this flag since the July fix.


## [2.7.0] - 2026-10-23

### Added

- **Phase-2 parameter controls**: every remaining portal dropdown is now a Home Assistant select on the Siseli device (Input Range, Input Frequency Range, Work Mode, Machine Charging Mode, Mains Sensitivity, Output Frequency, Fault Restart, Buzzer Mode, Battery Capacity Limit, Load Limiting Function, Load Offline Prompt, Inverter Output Mode, Battery Type, BMS Communication Switch, BMS Communication Protocol), and Backlight Mode gains its LOF option. Option codes are the manufacturer's own from the portal's official attribute definitions (`enumValues`), cross-checked against live values; the cloud-API key map covers all of them. The communication-port baud rate stays deliberately unexposed, as does the portal's "restore control parameters to default" action.

### Fixed

- **Settings-tail frame layout**: the VPHBQ tail is not uniformly `addr + 50`. Two non-parameter bytes are interleaved (byte 71, a live value, and byte 76, constant 3), so parameters from Inverter Output Mode (0x15) onward sit one or two bytes later than the Phase-1 map assumed. Seven Phase-1 entities (power-on/restore voltages, the four SOC thresholds and the low-voltage alarm) therefore displayed the neighbouring byte's value. `params.py` now carries the verified per-address frame-byte map and the test fixture uses the real tail layout.


## [2.6.2] - 2026-10-23

### Fixed

- **Cloud-API requests are now signed** like the portal's own web client: the login endpoint rejects unsigned calls with code 36 ("IOT-Open-AppID missing"), which the v2.6.1 unsigned client hit on its first live send. `cloud_api.py` now computes the `IOT-Open-Sign` header (MD5 of HMAC-SHA256 over the base64 canonical parameter string) and derives the HMAC key from the app secret published in the portal bundle, using a small self-contained AES-128 implementation (stdlib has none; covered by the FIPS-197 test vector). Verified live end-to-end before release: login and a same-value Backlight send succeed through the Python module itself.


## [2.6.1] - 2026-10-23

### Added

- **Cloud-API parameter writes for non-isolated mode** (`siseli_bridge/cloud_api.py`): when `ISOLATE_CLOUD` is off, Home Assistant parameter commands now go through the official Siseli cloud API, reproducing the portal's own control request (verified live 2026-10-23): login at `POST /apis/login/account` (password sent only as its MD5 hash, using the `SISELI_PORTAL_USER` / `SISELI_PORTAL_PASSWORD` add-on options), attribute objects fetched from `POST /remote/device/configs/cache/get`, and the send performed by `POST /remote/device/config/write` with the target `value`/`valueDisplay`. The access token is cached in `/data/siseli_cloud_token.json` (mode 600), logins are single-flight, and a failed send is retried once after a fresh login. State confirmation still comes only from telemetry, as in isolated mode. Fail-closed when credentials are not configured.


## [2.6.0] - 2026-10-23

### Added

- **Phase-1 Home Assistant parameter controls** (`ENABLE_PARAM_CONTROLS`, default on): MQTT discovery now exposes 15 numeric inverter settings and Backlight Mode on the existing Siseli device. Values shown in Home Assistant come only from the settings tail of confirmed `VPHBQ` telemetry frames; changing a control sends the verified CSPA/CSIPAM command in isolated-cloud mode and waits for a later frame to confirm the new value instead of publishing an optimistic state. The non-isolated cloud-API send path is present behind the same interface but is not wired yet and fails closed. The communication-port baud rate is deliberately **not** exposed because changing it can break the DTU-to-inverter serial link.

## [2.5.43] - 2026-10-23

### Changed

- **Froze 26 secondary options** (owner's decision): identity/discovery names, topics, MAC overrides, inverter/battery counts, the HTTP report-period pair, first-poll delay, payload prefix, MQTT retain and publish-on-every-telemetry are no longer editable in the add-on panel. Their current values are pinned as literal exports in `run.sh` (marked FROZEN) and removed from the config schema; the Python code is untouched and still reads them as environment variables. The 18 operational options (isolation switch, modem ritual, live/RPC polling, app ack, endpoints, IPs, log level) remain configurable. Nothing was removed functionally.

## [2.5.42] - 2026-10-23

### Added

- **Live polling in isolated mode** (`ENABLE_LIVE_POLLING`, `LIVE_POLL_INTERVAL_SEC`, default 3s, minimum 2s): the poller now asks the inverter for a fresh frame with the genuine `CMSG`/`INV-HB` command (dev_rpc i=503) captured from the real cloud, instead of only replaying the cached batch read (i=501). The DTU answers in ~1s with the full 86-byte frame. When no learned ci template exists, a built-in copy of the captured read-only INV-HB command is used; config-write (CSPA) templates are still never replayed.

### Fixed

- **RPC template learning never saved anything** (`core.py`): genuine payloads carry a leading `0x00` byte that made `json.loads` fail silently in `_save_rpc_template`, so the ci/RPC templates were never persisted. The prefix is now stripped before parsing.
- **RPC replies were invisible to the parser** (`parsers.py`): i=504 replies carry the frame in a bare `co` field (`{sa, co, no}`) with no block name, so `_walk_for_blocks` found no blocks. A nameless `co` that decodes to a VPHB frame is now treated as the standard telemetry block.

All notable changes to this project will be documented in this file.

## [2.5.41] - 2026-10-23

### Added

- **Automatic modem reboot when Isolated Cloud is activated** (`modem_reboot.py`): the field ritual for landing the dongle on the local fake cloud requires rebooting the LAN modem within seconds of the bridge restart that enables isolation. The bridge now records the previous run's `ISOLATE_CLOUD` value in `/data/modem_reboot_marker.json`; on an off -> on transition, with the new `MODEM_REBOOT_ON_ISOLATE` option enabled and `MODEM_PASSWORD` set, it logs into the modem web UI (TP-Link TD-W8961N flow, MD5-hashed credentials plus the form's hidden fields) and issues the reboot `MODEM_REBOOT_DELAY_SEC` seconds (default 2) after the fake cloud servers are up. Failures are logged and never block the bridge.

## [2.5.38] - 2026-07-25

### Changed

- **`send_manual_payload` now respects `APP_PAYLOAD_NUL_PREFIX`** (`proxy.py`): the JSON branch previously serialized with a bare `json.dumps().encode()`, so a manually-entered payload never carried the leading `0x00` byte regardless of the toggle -- a probe would look rejected when it was really malformed. The JSON branch now goes through `_encode_app_payload()`; the raw non-JSON branch is still sent verbatim on purpose (for testing malformed payloads).


- Manual RPC throttle now uses `time.monotonic()` instead of `time.time()`, so it can't be wedged open or shut by wall-clock adjustments (NTP correction, manual time change) on hosts with intermittent time sync.
- State read-diff-update-publish now runs under a single lock hold via a new `state.apply_and_snapshot()` accessor, so the logged `[STATE CHANGE]` diff and the state published always reflect the same merge (no interleaving writer between the read and the update). The snapshot is reused for the cache write, unresolved-target check, snapshot log, and publish.

## [2.5.37] - 2026-07-25

### Security

- **HTTP proxy accepts requests from any LAN host** (`http_proxy.py`): On a `host_network: true` container binding `0.0.0.0:80`, any device on the LAN could POST to `/dtu/devices/findSingle` and inject telemetry. Added `_check_source_ip()` that rejects requests from IPs other than the configured `INVERTER_IP`, returning HTTP 403 with `Connection: close` to prevent connection desynchronization.
- **Subprocess injection via `shell=True`** (`core.py`): The IP-change notification path used `shell=True` with string interpolation. Replaced with `subprocess.run(["nft", ...])` using an argv list, and validated the IP with `ipaddress.ip_address()` before use — rejects non-private, multicast, and loopback addresses.

### Fixed — Critical

- **Parser off-by-two crash on truncated telemetry blocks** (`parsers.py:723`): The binary `OgBU` block decoder used a single `len > 30` guard for all compact fields, but `status_code` (offset 31) and `mode` (offset 32) require `len > 31` and `len > 32` respectively. A truncated 31–32 byte block caused a silent `IndexError` that dropped `status_code` and `mode` without logging. Replaced with staged guards: `> 29` (load_pct, load_w, discharge), `> 30` (inverter_temperature_c), `> 31` (status_code), `> 32` (mode). Added 3 regression tests for truncated blocks at 29, 31, and 32 bytes.

### Fixed — High

- **Manual RPC flood — 132 sends in 2 minutes** (`mqtt.py`): The `on_message` handler for the manual RPC command topic had no rate limit. Retained command messages were redelivered on every broker reconnection, and Home Assistant's UI could fire dozens of commands per second. Added a 2-second minimum interval (`_MANUAL_RPC_MIN_INTERVAL`) between manual RPC sends, a `msg.retain` guard that drops and clears retained command payloads, and proactive retained-message clearing on connect (publishes empty payload with `retain=True` to both command topics after subscribing).

- **No network teardown on shutdown** (`core.py`, `run.sh`): When the container received SIGTERM, the ARP spoof and nftables/iptables rules were never cleaned up, leaving the inverter orphaned and traffic redirected to a dead proxy. Added ARP restoration in `shutdown()` — 3 rounds of gratuitous ARP with correct `hwsrc`/`src` MAC fields to both the inverter and the router. Added nftables/iptables/loopback cleanup with individual `try` blocks per command (one missing binary doesn't skip the rest). Changed `run.sh` from `exec python3` to a child/wait pattern with `trap forward_term TERM INT` and `trap cleanup_nat EXIT` so shell traps actually fire on container stop.

- **MQTT remaining-length decoder can block forever** (`proxy.py:383`): The remaining-length multiplier had no cap. A misaligned stream with continuation bits set on every byte would loop indefinitely. Capped at 4 continuation bytes per MQTT 3.1.1 §2.2.3 — exceeding this raises `ConnectionError` and closes the connection. Added `MAX_MQTT_PACKET` enforcement comparing total packet size (`pos + rem_len`) against the configured limit.

- **CONNECT parsing crashes on truncated packets** (`proxy.py:411`): `struct.unpack("!H", packet_bytes[pos + 10:pos + 12])` was called without verifying the buffer was long enough. A truncated CONNECT packet would raise `struct.error` inside the socket handler thread. Added bounds-checking before `struct.unpack` — verifies `len(packet_bytes) >= pos + 12` before unpacking, and `len(packet_bytes) >= id_end` before slicing the client ID. On failure, raises `ConnectionError` to close the connection.

- **`_send_ci_request` omits NUL prefix** (`proxy.py:149`): The ci keepalive used `json.dumps().encode()` directly, bypassing `_encode_app_payload()`. When `APP_PAYLOAD_NUL_PREFIX` is `true` (confirmed required on firmware v1.44.5), the resulting packet was missing the leading `0x00` byte and the inverter could not recognize it as a valid application payload. Routed through `_encode_app_payload()` to match `_send_rpc_request`.

- **`RUNNING` flag passed by value, not shared** (`state.py`, `mqtt.py`, `core.py`): The `RUNNING = True` boolean in `mqtt.py` was imported by value into `core.py`. When `shutdown()` set `RUNNING = False` in `core.py`'s namespace, `mqtt.py`'s copy stayed `True`, so `on_disconnect` retried indefinitely. Replaced with `threading.Event()` (`RUNNING_EVENT`) in `state.py`, shared across all modules via `_state.RUNNING_EVENT.is_set()` / `.clear()`.

- **`LAST_STATE` dict mutated without locking** (`state.py`, `parsers.py`): The shared `LAST_STATE` dictionary was read and written from multiple threads (parser thread, MQTT publisher thread, HA discovery thread) without synchronization, risking torn reads and `RuntimeError: dictionary changed size during iteration`. Added `threading.RLock()` (`STATE_LOCK`) with accessor functions: `get_state_snapshot()`, `update_state()`, `get_state_value()`, `has_any_state()`. All `LAST_STATE` accesses in `parsers.py` now use these lock-protected accessors.

- **HTTP proxy double-send on error** (`http_proxy.py`): If `_ok_json()` sent a response and then an exception was caught, the `except` block would call `_ok_json()` again, sending a second HTTP response on the same keep-alive connection. Added a `_responded` flag — `_ok_json()` sets it after writing, and `except` blocks check `if not getattr(self, "_responded", False)` before attempting an error response.

- **HTTP proxy socket not released on stop** (`http_proxy.py`): `stop()` called only `shutdown()` without `server_close()`, leaving the listening socket in `TIME_WAIT` and causing `EADDRINUSE` on fast restarts. Added `server_close()` after `shutdown()`.

- **nftables cleanup by expression fails silently** (`core.py`): The shutdown path used `nft delete rule` with a match expression, which fails silently if the rule doesn't exactly match (including handle numbers). Replaced with `nft flush chain` followed by `nft add rule` — flushes the entire chain and re-adds the redirect rule, which is idempotent and doesn't depend on matching existing rule handles.

### Fixed — Medium

- **State cache write amplification** (`parsers.py`): The state cache file was written on every telemetry parse (every few seconds), causing excessive SD-card writes. Added throttling — writes at most once every 10 seconds via `LAST_CACHE_WRITE_TS`. The timestamp only updates after a successful `os.replace()`, so a failed write retries immediately on the next parse. The cache is only read once at boot, so a slightly stale copy is acceptable.

- **State cache not atomic** (`parsers.py`): The cache file was written directly with `open()` + `json.dump()`. A SIGTERM mid-write could leave a truncated or partial JSON file, corrupting the cache. Changed to atomic write via temp file + `os.replace()`. Added `traceback.format_exc()` to the parser error handler for better debugging.

- **Unresolved debug fields generate perpetual log noise** (`parsers.py`): `IMPORTANT_DEBUG_KEYS` included `bms_avg_temp_c` and `mains_current_flow_direction`, which are not present in `SENSORS` and therefore unpublishable by construction. This generated 97+ "UNRESOLVED TARGETS" warnings in the log with no actionable fix. `IMPORTANT_DEBUG_KEYS` is now filtered at import time to only include keys that exist in `SENSORS`.

### Changed

- **Version single-source** (`run.sh`, `core.py`): The version string was hardcoded independently in `run.sh` (2.5.30), `core.py` (2.5.29), and `config.yaml` (2.5.36), causing drift. `run.sh` now reads the version from `bashio::addon.version` (injected by Home Assistant from `config.yaml`), with a fallback to parsing `config.yaml` directly, then a hardcoded default. `core.py` reads it from the `SISELI_BRIDGE_VERSION` environment variable exported by `run.sh`. The version now flows through a single path: `config.yaml` → bashio → `run.sh` → env var → `core.py`.

- **ARP teardown uses correct MAC fields** (`core.py`): The initial ARP restoration used placeholder values for `hwsrc` and `src`. Corrected to use the actual interface MAC and the inverter/router IP addresses, so gratuitous ARP packets are properly formed and the network devices update their ARP caches.

### Added

- **`state.py` module** (`src/siseli_bridge/state.py`): New module centralizing shared state with thread-safe accessors. Contains `STATE_LOCK` (`threading.RLock`), `RUNNING_EVENT` (`threading.Event`), `LAST_STATE` (dict), and accessor functions `get_state_snapshot()`, `update_state()`, `get_state_value()`, `has_any_state()`.

- **3 regression tests** (`tests/test_parsers.py`): `test_binary_ogbu_truncated_29_bytes_yields_no_compact_fields`, `test_binary_ogbu_truncated_31_bytes_does_not_crash`, `test_binary_ogbu_truncated_32_bytes_does_not_crash` — verify that truncated binary blocks at boundary lengths don't crash and yield the expected subset of fields.

## [2.5.36] - 2026-07-25

### Added

- `ENABLE_FIRST_POLL_DELAY` (default `true`) and `RPC_FIRST_POLL_DELAY_SEC` (default `30`): the first RPC telemetry poll is anchored to the inverter's first PUBLISH plus the configured delay, then repeats every `UPDATE_INTERVAL_SEC`.
- `ENABLE_HTTP_REPORT_PERIOD` (default `true`) and `HTTP_REPORT_PERIOD_SEC` (default `60`): the local `/dtu/checkin` returns a `performanceReportPeriod` derived from this value, and the full cloud-shaped checkin response (`timeInText`, `timeInMills`, `utcOffset`, `outbound`, `gatherProtocol`). A startup log line reports the armed value.

### Changed

- The poller loop now ticks every 1s and schedules RPC/`ci` sends by monotonic timestamps, so the anchored first poll fires on time. The `ci` keepalive cadence is unchanged and unaffected by the first-poll delay.

### Notes

- Testing on firmware **v1.44.5** confirmed the dongle **ignores** `performanceReportPeriod` (its autonomous `dev_event_post` stays at ~60s) and only requests `/dtu/checkin` on a **cold boot**, not on MQTT reconnect -- so the override often never even reaches it. The HTTP options are retained for a possible future firmware. The effective telemetry-frequency lever is `UPDATE_INTERVAL_SEC` (the RPC poll), confirmed answering every ~10s with a full `OgBU` block.

## [2.5.33] - 2026-07-24

### Removed

- `POLL_BLOCKS` option, along with its schema entry, `run.sh` export and the now-orphaned import in `proxy.py`. The setting had become dead code once the RPC poll switched to an empty body, and requesting a block by name never worked anyway: the firmware answers `e:104`. Block names are fixed per publishing topic (`dev_event_post` → `Adlg`, `dev_prop_post` / `dev_rpc_reply` → `OgBU`), not addressable registers.

> **Upgrade note**: remove `POLL_BLOCKS` from the add-on's saved options YAML before restarting. Supervisor rejects saved options that are no longer in the schema, and the add-on will fail to start with a validation error.

## [2.5.32] - 2026-07-24

### Added

- `ENABLE_APP_ACK` option to toggle the application-level ack sent after each `pub/event/*` publish.
- `APP_PAYLOAD_NUL_PREFIX` option to toggle the leading `0x00` byte on outgoing application payloads.
- Startup log line reporting both flags, so the active configuration is visible in the log.

Both settings were exposed because their necessity was unproven at the time. A/B testing on this version settled it: the `0x00` prefix is **required** — with it disabled the inverter silently ignores the RPC request and never replies — while the application-level ack turned out to be **unnecessary** and can be left off.

## [2.5.31] - 2026-07-24

### Fixed

- **RPC poll never answered.** `_send_rpc_request` sent `i:502` with a `"ct":[{"cn":<block>}]` block-name request. The genuine cloud uses `i:501` with an empty body (`"b":{}`), and `502` is the *response* id. The poll now mirrors the real cloud packet, and the inverter answers every request with a full `OgBU` block — roughly halving telemetry latency when combined with the passive heartbeat.
- **Application-level ack always sent `i:0`.** Inverter payloads carry a leading `0x00` byte before the JSON, so `json.loads()` on the raw bytes always raised and the exception was swallowed, defaulting the id to zero. The envelope is now parsed from the first `{`.
- **Ack used the wrong reply id.** The cloud answers with `request_i + 1` (201→202, 101→102), never the same id. It also echoes the request's `t` token verbatim rather than minting a new one, and its `c` code simply mirrors the request's own `c`, which makes the hardcoded per-event lookup table a fallback rather than the primary source.
- No ack is sent at all when the request id cannot be read, instead of emitting an unmatched `i:0`.

### Fixed (previously unreleased)

- `ENABLE_RPC_POLLING` toggle in the add-on UI had no effect. `run.sh` never exported it to the container environment, so `config.py` always fell back to its `true` default regardless of the setting. The automatic RPC poller now actually stops when the toggle is turned off.

## [2.5.29] - 2026-07-19

- Explicitly copy the complete runtime source package into the add-on image.

## [2.5.28] - 2026-07-19

- Confirm MQTT subscriptions with the QoS actually requested by the inverter.

## [2.5.27] - 2026-07-19

- Explicitly copy the required Home Assistant sensor definitions into the image.

## [2.5.26] - 2026-07-19

- Derive PV/solar power as battery voltage multiplied by controller current.

## [2.5.25] - 2026-07-19

- Derive AC output power from live load percentage and configured rated output.
- Derive battery discharge current during an off-grid, non-solar load state.
- Publish live controller temperature through the legacy PV-temperature entity.

## [2.5.24] - 2026-03-30

### Fixed

- **Logging Level Enforcement**: Implemented level-aware logging so `LOG_LEVEL=error` suppresses routine informational/debug logs while preserving error output.
- **Debug Block Noise**: Converted parser debug block output (`[DEBUG BLOCK]`) to debug-level logging so it no longer appears at warning/error levels.
- **Severity Alignment**: Tagged runtime and MQTT lifecycle logs with explicit severities for consistent filtering behavior.

### Added

- **Logging Regression Tests**: Added `tests/test_logging.py` coverage for level filtering and always-on critical error logging behavior.

## [2.5.23] - 2026-03-30

### Fixed

- **App Parity (BMS Temp)**: Added guarded parsing for `bms_avg_temp_c` from extended `Yavb` payloads to reduce `null` values when the inverter provides the field.
- **App Parity (Charging Light)**: Updated preset fallback for `charging_light_status` from `Light` to `Flicker` to match observed app behavior.
- **App Parity (Output Set Frequency)**: Normalized `output_set_frequency` display to app-style integer setpoint representation (example: `49.9` -> `50`).
- **App Parity (Software Version)**: Normalized `software_version` display format (example: `0010.11` -> `10.11`) while preserving raw firmware token in `firmware_version`.

### Changed

- **README Refresh**: Expanded README with a "What is New" section and clearer add-on usage notes.
- **Add-on Info Documentation Link**: Added add-on `url` metadata so the Home Assistant add-on page points directly to the project README.

## [2.5.22] - 2026-03-30

### Changed

- **Battery Power Calculation**: `c_battery_charge_power_w` and `c_battery_discharge_power_w` are no longer scaled by `INVERTER_COUNT`; they reflect the per-inverter BMS reading directly (`bat_v × current_a`).

## [2.5.21] - 2026-03-30

### Changed

- **Calculated Sensor Device Routing**: All calculated sensors (`c_*`) are now grouped under the Main logical device.
