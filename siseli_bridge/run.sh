#!/usr/bin/with-contenv bashio

# Single-source version: read from bashio::addon.version (Home Assistant
# injects the value from config.yaml at runtime), fall back to parsing
# config.yaml directly, then a hardcoded default.
VERSION="$(bashio::addon.version 2>/dev/null || true)"
if [ -z "${VERSION}" ]; then
    VERSION="$(awk -F: '/^version:/ {gsub(/["[:space:]]/, "", $2); print $2; exit}' /app/config.yaml 2>/dev/null || true)"
fi
VERSION="${VERSION:-2.5.37}"
echo "--- Siseli Inverter Bridge ${VERSION} ---"
export SISELI_BRIDGE_VERSION="${VERSION}"

# ===== Opciones congeladas (2026-10-23) =====
# Fuera del panel de ajustes: sus valores van fijos en este archivo
# (marcados FROZEN) y NO se leen de la configuracion del add-on.
# El codigo Python no cambia: sigue leyendolas como variables de entorno.

export MQTT_HOST="$(bashio::config 'MQTT_HOST' 'core-mosquitto')"
export MQTT_PORT="$(bashio::config 'MQTT_PORT' '1883')"
export MQTT_USER="$(bashio::config 'MQTT_USER' '')"
export MQTT_PASSWORD="$(bashio::config 'MQTT_PASSWORD' '')"

export TARGET_HOST="$(bashio::config 'TARGET_HOST' '8.212.18.157')"
export TARGET_PORT="$(bashio::config 'TARGET_PORT' '1883')"
export LISTEN_PORT="18899"  # FROZEN 2026-10-23

export INVERTER_IP="$(bashio::config 'INVERTER_IP')"
export ROUTER_IP="$(bashio::config 'ROUTER_IP')"
export INVERTER_MAC=""  # FROZEN 2026-10-23
export ROUTER_MAC=""  # FROZEN 2026-10-23
export AUTO_INTERCEPT="true"  # FROZEN 2026-10-23
export ISOLATE_CLOUD="$(bashio::config 'ISOLATE_CLOUD' 'true')"
export MODEM_REBOOT_ON_ISOLATE="$(bashio::config 'MODEM_REBOOT_ON_ISOLATE' 'false')"
export MODEM_USER="admin"  # FROZEN 2026-10-23
export MODEM_PASSWORD="$(bashio::config 'MODEM_PASSWORD' '')"
export MODEM_REBOOT_DELAY_SEC="$(bashio::config 'MODEM_REBOOT_DELAY_SEC' '2')"

export MQTT_DISCOVERY_PREFIX="homeassistant"  # FROZEN 2026-10-23
export DEVICE_ID="siseli_inverter_1"  # FROZEN 2026-10-23
export DEVICE_NAME="Siseli Inverter 1"  # FROZEN 2026-10-23
export MODEL_NAME="Siseli Inverter 1"  # FROZEN 2026-10-23
export MANUFACTURER="Siseli Compatible"  # FROZEN 2026-10-23
export STATE_TOPIC="siseli/siseli_inverter_1/state"  # FROZEN 2026-10-23
export AVAILABILITY_TOPIC="siseli/siseli_inverter_1/availability"  # FROZEN 2026-10-23
export SNIFF_IFACE=""  # FROZEN 2026-10-23
export LOG_VERBOSE="true"  # FROZEN 2026-10-23
export ENTITY_PREFIX="Siseli"  # FROZEN 2026-10-23
export LOG_LEVEL="$(bashio::config 'LOG_LEVEL' 'info')"
export UPDATE_INTERVAL_SEC="$(bashio::config 'UPDATE_INTERVAL_SEC' '10')"
export ENABLE_RPC_POLLING="false"  # FROZEN 2026-10-24
export ENABLE_LIVE_POLLING="$(bashio::config 'ENABLE_LIVE_POLLING' 'false')"
export LIVE_POLL_INTERVAL_SEC="$(bashio::config 'LIVE_POLL_INTERVAL_SEC' '3')"
export ENABLE_FIRST_POLL_DELAY="true"  # FROZEN 2026-10-23
export RPC_FIRST_POLL_DELAY_SEC="30"  # FROZEN 2026-10-23
export ENABLE_HTTP_REPORT_PERIOD="false"  # FROZEN 2026-10-23
export HTTP_REPORT_PERIOD_SEC="10"  # FROZEN 2026-10-23
export ENABLE_APP_ACK="$(bashio::config 'ENABLE_APP_ACK' 'true')"
export ENABLE_PARAM_CONTROLS="$(bashio::config 'ENABLE_PARAM_CONTROLS' 'true')"
export SISELI_PORTAL_USER="$(bashio::config 'SISELI_PORTAL_USER' '')"
export SISELI_DTU_ID="$(bashio::config 'SISELI_DTU_ID' '')"
export SISELI_DTU_SERIAL="$(bashio::config 'SISELI_DTU_SERIAL' '')"
export SISELI_PORTAL_DEVICE_ID="$(bashio::config 'SISELI_PORTAL_DEVICE_ID' '')"
export SISELI_PORTAL_STATION_ID="$(bashio::config 'SISELI_PORTAL_STATION_ID' '')"
export SISELI_PORTAL_PASSWORD="$(bashio::config 'SISELI_PORTAL_PASSWORD' '')"
export APP_PAYLOAD_NUL_PREFIX="true"  # FROZEN 2026-10-23
export MQTT_RETAIN="true"  # FROZEN 2026-10-23
export PUBLISH_ON_EVERY_TELEMETRY="true"  # FROZEN 2026-10-23
export INVERTER_COUNT="1"  # FROZEN 2026-10-23
export BATTERY_COUNT="1"  # FROZEN 2026-10-23
export BATTERY_CAPACITY_PER_BATTERY_AH="0.0"  # FROZEN 2026-10-23
export RATED_OUTPUT_POWER_W="5000"  # FROZEN 2026-10-23

echo "[Config] INVERTER_IP=${INVERTER_IP} ROUTER_IP=${ROUTER_IP}"
echo "[Config] TARGET=${TARGET_HOST}:${TARGET_PORT} MQTT=${MQTT_HOST}:${MQTT_PORT}"
echo "[Config] AUTO_INTERCEPT=${AUTO_INTERCEPT}"
echo "[Config] ISOLATE_CLOUD=${ISOLATE_CLOUD}"
echo "[Config] MODEM_REBOOT_ON_ISOLATE=${MODEM_REBOOT_ON_ISOLATE} MODEM_REBOOT_DELAY_SEC=${MODEM_REBOOT_DELAY_SEC}"
echo "[Config] ENABLE_RPC_POLLING=${ENABLE_RPC_POLLING}"
echo "[Config] ENABLE_LIVE_POLLING=${ENABLE_LIVE_POLLING} LIVE_POLL_INTERVAL_SEC=${LIVE_POLL_INTERVAL_SEC}"
echo "[Config] ENABLE_FIRST_POLL_DELAY=${ENABLE_FIRST_POLL_DELAY} RPC_FIRST_POLL_DELAY_SEC=${RPC_FIRST_POLL_DELAY_SEC}"
echo "[Config] ENABLE_HTTP_REPORT_PERIOD=${ENABLE_HTTP_REPORT_PERIOD} HTTP_REPORT_PERIOD_SEC=${HTTP_REPORT_PERIOD_SEC}"
echo "[Config] ENABLE_APP_ACK=${ENABLE_APP_ACK} APP_PAYLOAD_NUL_PREFIX=${APP_PAYLOAD_NUL_PREFIX}"
echo "[Config] ENABLE_PARAM_CONTROLS=${ENABLE_PARAM_CONTROLS}"

# Direct the inverter's cloud MQTT connection to the local proxy while cloud
# isolation is enabled.  The cleanup makes repeated add-on starts idempotent.
cleanup_nat() {
    nft delete table ip siseli_nat 2>/dev/null || true
    nft delete table ip siseli_filter 2>/dev/null || true
    iptables -t nat -D PREROUTING -s "${INVERTER_IP}" -p tcp --dport "${TARGET_PORT}" -j REDIRECT --to-ports "${LISTEN_PORT}" 2>/dev/null || true
    iptables -D OUTPUT -p icmp --icmp-type redirect -j DROP 2>/dev/null || true
    ip addr del "${TARGET_HOST}/32" dev lo 2>/dev/null || true
}

# Run cleanup on container stop (SIGTERM/SIGINT) as well as at startup so the
# network is not left poisoned between add-on stop and next start.
trap cleanup_nat TERM INT EXIT

cleanup_nat

if [ "${ISOLATE_CLOUD}" = "true" ]; then
    echo "[PROXY] Aliasing ${TARGET_HOST} on loopback"
    ip addr add "${TARGET_HOST}/32" dev lo 2>/dev/null || true

    echo "[PROXY] Redirecting ${TARGET_PORT} to local port ${LISTEN_PORT}"
    if command -v nft >/dev/null 2>&1 && \
        nft add table ip siseli_nat && \
        nft 'add chain ip siseli_nat prerouting { type nat hook prerouting priority dstnat; }' && \
        nft add rule ip siseli_nat prerouting ip saddr "${INVERTER_IP}" tcp dport "${TARGET_PORT}" redirect to :"${LISTEN_PORT}" && \
        nft add table ip siseli_filter && \
        nft 'add chain ip siseli_filter output { type filter hook output priority filter; }' && \
        nft add rule ip siseli_filter output ip protocol icmp icmp type 5 drop; then
        echo "[PROXY] nftables redirect active"
    else
        echo "[PROXY] nftables unavailable; using iptables redirect"
        nft delete table ip siseli_nat 2>/dev/null || true
        nft delete table ip siseli_filter 2>/dev/null || true
        if ! iptables -t nat -A PREROUTING -s "${INVERTER_IP}" -p tcp --dport "${TARGET_PORT}" -j REDIRECT --to-ports "${LISTEN_PORT}" || \
           ! iptables -A OUTPUT -p icmp --icmp-type redirect -j DROP; then
            echo "[PROXY ERROR] Could not create local redirect rules. Check NET_ADMIN permissions."
            exit 1
        fi
        echo "[PROXY] iptables redirect active"
    fi
fi

# Run the bridge as a child process so shell traps (TERM/INT/EXIT) survive
# and can clean up network state.  Using `exec` would replace the shell and
# the traps would never fire.
python3 -u -m src.siseli_bridge.core &
child_pid=$!

forward_term() {
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
    cleanup_nat
    exit 0
}

trap forward_term TERM INT
trap cleanup_nat EXIT

wait "$child_pid"
