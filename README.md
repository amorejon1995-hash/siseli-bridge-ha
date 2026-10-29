# ☀️ Siseli Inverter Bridge — Extended (split-phase, writable controls, full cloud isolation)

A Home Assistant add-on that runs a **Siseli-compatible solar inverter fully locally**:
it decodes the telemetry the inverter's WiFi dongle already sends to the vendor cloud,
publishes it to Home Assistant through MQTT auto-discovery, and — unlike a read-only
mirror — can also **write the inverter's parameters back** and even **replace the
vendor cloud entirely** with a local "fake cloud", so the plant keeps working with
no internet and no vendor account.

Developed and run in production on a **PowMr POW-RELAB 5 kW split-phase (110/220 V,
48 V battery)** inverter in Cuba, where the internet connection is unreliable enough
that depending on a cloud in another continent is not an option.

> **Lineage:** this is an independent, heavily extended continuation of
> [yuraantonov11/siseli-ha](https://github.com/yuraantonov11/siseli-ha) (the original
> bridge) via the generalized fork [fadmaz/siseli-ha](https://github.com/fadmaz/siseli-ha).
> See [`NOTICE`](NOTICE) for attribution and licensing details.

---

## What it does

- **Full local telemetry** for split-phase PowMr units (capture definition `713`):
  grid voltage/frequency and presence, L1/L2 output voltage, current and power,
  PV voltage/power, battery voltage/current/SOC from the inverter's BMS view,
  load percentage, temperatures, operating mode, alarms, and daily/cumulative
  energy figures — published as Home Assistant entities via MQTT discovery.
- **Writable parameter controls (31 parameters):** output mode, charger priority,
  voltage points, charge currents, battery type and cut-offs, back-to-grid and
  back-to-discharge points, and more — exposed as Home Assistant `number`/`select`
  entities and written to the inverter.
- **Two control paths:**
  - *Isolated mode:* parameters are written locally, encoded as the same `ci`
    configuration frames the cloud would send down to the dongle.
  - *Non-isolated mode:* parameters are written through the **official Siseli
    cloud API**, reproducing the web portal's own signed requests, while
    telemetry still comes from the local interception.
- **Full cloud isolation (`ISOLATE_CLOUD`):** the add-on impersonates the Siseli
  cloud toward the dongle — local MQTT broker endpoint, HTTP report endpoints and
  DNS answers — and acknowledges the dongle's traffic with app-parity replies, so
  the inverter behaves exactly as if the cloud were up. Optionally reboots the
  LAN modem automatically at the moment isolation is switched on (the field
  ritual that makes the dongle land on the fake cloud).
- **DTU restart** from Home Assistant, through the same portal flow the vendor
  app uses.
- **Fail-safe by design:** if the add-on stops in non-isolated mode, the inverter
  simply keeps talking to the real cloud and the official app keeps working.

## How it works (the mechanism)

1. **Getting in the path.** With `AUTO_INTERCEPT` on, the add-on uses ARP
   interception so the dongle's packets reach the Home Assistant host; it then
   reassembles the TCP stream toward the Siseli cloud MQTT endpoint
   (`8.212.18.157:1883`) and extracts the MQTT PUBLISH frames. (A router-level
   redirect of that host to the HA host works too.)
2. **Decoding.** Siseli payloads are JSON envelopes of base64 blocks keyed by
   four-character names; each block is a register image read by offset. This
   fork's parser implements the map for the split-phase POW-RELAB family
   (definition `713`), which the upstream projects — built on single-phase
   230 V units — do not decode.
3. **Publishing.** Decoded values are published to your own MQTT broker with
   Home Assistant auto-discovery, so entities appear on their own, with
   availability tracking and retained state.
4. **Relaying or replacing the cloud.** In normal mode the cloud connection is
   forwarded onward untouched, so the vendor app keeps working. In isolated
   mode the bridge terminates the dongle's cloud protocols locally instead:
   DNS for the vendor hosts resolves to the bridge, the HTTP reporting
   endpoints answer like the real portal, and every downlink the dongle
   expects (session acks, keep-alives, configuration echoes) is generated
   locally with the same reply ids and tokens the real cloud uses.
5. **Writing parameters.** A change in Home Assistant is encoded into the
   protocol's configuration frame (`CSPA`/`ci`, address + raw value) and
   delivered to the dongle in isolated mode, or sent through the signed
   cloud-API write (`/remote/device/config/write`) in non-isolated mode.
   Confirmation always comes from the telemetry read-back, never from the
   send alone.

## Requirements

- Home Assistant OS with the **Mosquitto broker** add-on (or any MQTT broker
  reachable by the add-on).
- The inverter/dongle and the Home Assistant host on the **same LAN** (ARP
  interception is a Layer-2 technique).
- A Siseli-platform inverter. Verified in depth on the **PowMr POW-RELAB
  5KW-SPLIT (5KU)**. Other Siseli-based brands (Solar of Things, Taico,
  SunSaviour, …) may work with the upstream block maps; the split-phase map
  here is specific to the POW-RELAB family.

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**,
   add:
   ```
   https://github.com/amorejon1995-hash/siseli-bridge-ha
   ```
2. Install **Siseli Inverter Bridge** from the store.
3. Configure at least:
   - `MQTT_HOST` / `MQTT_PORT` / `MQTT_USER` / `MQTT_PASSWORD` — your broker.
   - `INVERTER_IP` — the dongle/inverter LAN address; `ROUTER_IP` — your gateway.
   - `AUTO_INTERCEPT: true` (or set up the router redirect instead).
4. Start the add-on. Entities appear under the MQTT integration within a
   minute of the dongle's next telemetry burst.
5. **For parameter writes through the cloud API** (non-isolated mode), also set
   `SISELI_PORTAL_USER` / `SISELI_PORTAL_PASSWORD` (your Siseli portal account)
   and `SISELI_PORTAL_DEVICE_ID` / `SISELI_PORTAL_STATION_ID` (your plant's
   identifiers, visible in the portal's device list requests).
6. **For isolated mode**, set `ISOLATE_CLOUD: true` and restart the add-on,
   then power-cycle or reboot the path so the dongle re-resolves the vendor
   hosts — with `MODEM_REBOOT_ON_ISOLATE: true` and `MODEM_PASSWORD` set
   (TP-Link TD-W8961N flow), the bridge reboots the modem itself a couple of
   seconds after the fake cloud is up. Keep `ENABLE_APP_ACK: true` so the
   dongle receives the acknowledgements it expects.

> ⚠️ One register is deliberately **never written** by this add-on: the
> inverter's serial **baud-rate register (address `0x14`)**. Changing it can
> permanently break dongle communication. Leave it alone in any fork too.

## Key configuration options

| Option | Meaning |
|---|---|
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD` | Broker the bridge publishes to |
| `INVERTER_IP`, `ROUTER_IP` | Dongle and gateway addresses on your LAN |
| `AUTO_INTERCEPT` | ARP interception to get in the traffic path |
| `ISOLATE_CLOUD` | Replace the vendor cloud locally (fake cloud) |
| `ENABLE_APP_ACK` | App-parity acknowledgements toward the dongle |
| `ENABLE_PARAM_CONTROLS` | Expose the 31 writable parameters in HA |
| `ENABLE_LIVE_POLLING` | Active RPC polling of the dongle between bursts |
| `MODEM_REBOOT_ON_ISOLATE`, `MODEM_PASSWORD` | Automatic modem reboot when isolation starts |
| `SISELI_PORTAL_USER`, `SISELI_PORTAL_PASSWORD` | Portal account (cloud-API writes only) |
| `SISELI_DTU_SERIAL`, `SISELI_DTU_ID` | Your dongle's serial / portal DTU id (DTU restart) |
| `SISELI_PORTAL_DEVICE_ID`, `SISELI_PORTAL_STATION_ID` | Your plant's portal identifiers (cloud-API writes) |
| `RATED_OUTPUT_POWER_W` | Inverter rating used for load percentage |

The full option list with defaults is in
[`siseli_bridge/config.yaml`](siseli_bridge/config.yaml); release history in
[`siseli_bridge/CHANGELOG.md`](siseli_bridge/CHANGELOG.md).

## 🇪🇸 En español

Este add-on de Home Assistant hace funcionar un inversor compatible con la
plataforma **Siseli** (probado en producción con un **PowMr POW-RELAB 5 kW
split-phase 110/220 V**) de forma **totalmente local**: intercepta en la red
local el tráfico que el dongle WiFi envía a la nube del fabricante, decodifica
la telemetría (bloques base64 del protocolo propietario; mapa de registros de
la familia split-phase, definición `713`) y la publica en Home Assistant por
MQTT con auto-descubrimiento.

A diferencia de los proyectos originales, de los que desciende, este además:

- **escribe los parámetros del inversor** (31 controles: modo de salida,
  prioridades de carga, voltajes, corrientes, cortes de batería…), en modo
  aislado con las mismas tramas `ci` que enviaría la nube, y en modo normal a
  través de la **API oficial de la nube Siseli**, firmando las peticiones como
  el portal web;
- **sustituye por completo a la nube** (`ISOLATE_CLOUD`): nube falsa local con
  DNS, endpoints HTTP y acuses idénticos a los del servidor real, con reinicio
  automático opcional del módem al activar el aislamiento;
- reinicia el DTU desde Home Assistant y confirma cada escritura leyendo la
  telemetría, nunca fiándose solo del envío.

**Instalación:** añade este repositorio en la tienda de add-ons de Home
Assistant, instala *Siseli Inverter Bridge*, configura el broker MQTT y las IP
del inversor y del router, y arranca. Los detalles están en la sección
*Installation* de arriba. El registro de baudios del inversor (dirección
`0x14`) no se escribe nunca: tocarlo puede dejar el dongle incomunicado.

## Credits and license

Original bridge by **yuraantonov11**; generalized fork by **fadmaz** (MIT);
this extended continuation was written by an **AI assistant**, working under
the direction of the repository's owner, who led the process, made the
design decisions and validated everything on the real hardware. See
[`NOTICE`](NOTICE) for the full attribution chain and [`LICENSE`](LICENSE)
(MIT) for the parts authored in this repository.

## Disclaimer

Reverse-engineered for interoperability with hardware you own. Not
affiliated with Siseli / Solar of Things or PowMr. Use at your own risk —
you are modifying how a power device is controlled; keep the vendor's
safety protections (and your own guardians) in place.
