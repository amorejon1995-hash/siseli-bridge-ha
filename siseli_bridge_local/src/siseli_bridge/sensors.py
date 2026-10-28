from typing import Dict

def sensor(name: str, **kwargs) -> Dict[str, object]:
    data: Dict[str, object] = {"name": name}
    data.update(kwargs)
    return data


SENSORS: Dict[str, Dict[str, object]] = {
    # Info / identity
    "model_code": sensor("Device Info - Device Type", icon="mdi:identifier", entity_category="diagnostic"),
    "mode": sensor("Device Info - Mode", icon="mdi:transmission-tower-export"),
    "mode_code": sensor("Device Info - Mode Code", icon="mdi:identifier", entity_category="diagnostic"),
    "status_code": sensor("Device Info - Status Code", icon="mdi:identifier", entity_category="diagnostic"),
    "firmware_version": sensor("Device Info - Firmware Version", icon="mdi:chip", entity_category="diagnostic"),
    "controller_program_version": sensor("Device Info - Controller Program Version", icon="mdi:chip", entity_category="diagnostic"),

    # Battery / BMS main page
    "bat_v": sensor("Battery Status - Battery Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:battery"),
    "bat_cap": sensor("Battery Status - Battery Capacity", unit="%", device_class="battery", state_class="measurement"),
    "dischg_current": sensor("Battery Status - Battery Discharge Current", unit="A", device_class="current", state_class="measurement", icon="mdi:battery-minus"),
    "bat_series_count": sensor("Battery Status - Battery Number In Series", state_class="measurement", icon="mdi:numeric"),
    "battery_status": sensor("Battery Status - Battery Status", icon="mdi:battery-sync"),

    # BMS page

    # Grid page
    "grid_v": sensor("Grid Status - AC Input Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:transmission-tower"),
    "grid_hz": sensor("Grid Status - Mains Frequency", unit="Hz", device_class="frequency", state_class="measurement", icon="mdi:current-ac"),

    # Load page
    "out_v": sensor("Load Status - Output Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:power-plug"),
    "out_hz": sensor("Load Status - Output Frequency", unit="Hz", device_class="frequency", state_class="measurement", icon="mdi:current-ac"),
    "load_w": sensor("Load Status - Output Active Power", unit="W", device_class="power", state_class="measurement", icon="mdi:home-lightning-bolt"),
    "load_pct": sensor("Load Status - Output Load Percent", unit="%", state_class="measurement", icon="mdi:gauge"),

    # PV page
    "generation_power_w": sensor("PV Panel Status - Generation Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-power"),
    "c_generation_power_w": sensor("PV Panel Status - Calculated Generation Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-power"),
    "pv_v": sensor("PV Panel Status - PV Voltage", unit="V", device_class="voltage", state_class="measurement", icon="mdi:solar-panel"),
    "pv_current_a": sensor("PV Panel Status - PV Current", unit="A", device_class="current", state_class="measurement", icon="mdi:current-dc"),
    "pv_w": sensor("PV Panel Status - PV Power", unit="W", device_class="power", state_class="measurement", icon="mdi:solar-power"),
    "pv_temp": sensor("PV Panel Status - PV Temperature", unit="°C", device_class="temperature", state_class="measurement", icon="mdi:thermometer"),
    "controller_charging_current_a": sensor("PV Panel Status - Controller Charging Current", unit="A", device_class="current", state_class="measurement", icon="mdi:current-dc"),
    "inverter_charging_current_a": sensor("Battery Status - Inverter Charging Current", unit="A", device_class="current", state_class="measurement", icon="mdi:battery-charging"),
    "grid_power_w": sensor("Grid Status - Grid Power", unit="W", device_class="power", state_class="measurement", icon="mdi:transmission-tower"),
    "controller_temp_c": sensor("PV Panel Status - Controller Temperature", unit="°C", device_class="temperature", state_class="measurement", icon="mdi:thermometer"),
    "controller_status_code": sensor("PV Panel Status - Controller Status Code", icon="mdi:identifier", entity_category="diagnostic"),
    "solar_charging_switch": sensor("PV Panel Status - Solar Charging Switch", icon="mdi:solar-power-variant-outline"),

    # App "More" page – mapped / partially mapped
    "bms_current_soc": sensor("Settings - BMS Current SOC", unit="%", device_class="battery", state_class="measurement"),
    "bms_discharge_current_a": sensor("Settings - BMS Discharge Current", unit="A", device_class="current", state_class="measurement", icon="mdi:battery-minus", entity_category="diagnostic"),
    "inverter_temperature_c": sensor("Settings - Inverter Temperature", unit="°C", device_class="temperature", state_class="measurement", icon="mdi:thermometer", entity_category="diagnostic"),
    "software_version": sensor("Settings - Software Version", icon="mdi:chip", entity_category="diagnostic"),

    # Raw / decoded helper sensors
    "mains_wdrr_value": sensor("Settings - Mains WdRR Value", state_class="measurement", icon="mdi:numeric", entity_category="diagnostic", enabled_by_default=False),

    # Compatibility aliases for older entity names
    "bat_temp": sensor("Settings - Inverter Temperature (legacy)", unit="°C", device_class="temperature", state_class="measurement", icon="mdi:thermometer", entity_category="diagnostic", enabled_by_default=False),

    # New sensors for the GPeV/PWDK/5yXy inverter family
}

SENSOR_GROUP_TITLES: Dict[str, str] = {
    "main": "Main",
    "battery": "Battery",
    "bms": "BMS",
    "grid": "Grid",
    "load": "Load",
    "pv": "PV",
    "diagnostics": "Diagnostics",
}

MAIN_SENSOR_KEYS = {
    "mode",
    "bms_current_soc",
}

_BATTERY_HINTS = (
    "battery",
    "bms",
    "charge",
    "discharge",
    "soc",
    "cell",
    "equalization",
    "boost",
)

_GRID_HINTS = (
    "grid",
    "mains",
    "utility",
    "input",
    "relay",
    "ct_",
)

_PV_HINTS = (
    "pv",
    "solar",
    "mppt",
)

_LOAD_HINTS = (
    "output",
    "load",
    "inverter",
    "parallel",
)


def _settings_group_for_key(key: str) -> str:
    k = key.lower()
    if any(token in k for token in _BATTERY_HINTS):
        return "battery"
    if any(token in k for token in _GRID_HINTS):
        return "grid"
    if any(token in k for token in _PV_HINTS):
        return "pv"
    if any(token in k for token in _LOAD_HINTS):
        return "load"
    return "diagnostics"


def get_sensor_group(key: str) -> str:
    if key.startswith("c_"):
        return "main"
    if key in MAIN_SENSOR_KEYS:
        return "main"
    meta = SENSORS.get(key)
    if not meta:
        return "diagnostics"

    name = str(meta.get("name", ""))
    if name.startswith("Battery Status - "):
        return "battery"
    if name.startswith("BMS Status - "):
        return "bms"
    if name.startswith("Grid Status - "):
        return "grid"
    if name.startswith("Load Status - "):
        return "load"
    if name.startswith("PV Panel Status - "):
        return "pv"
    if name.startswith("Settings - "):
        return _settings_group_for_key(key)
    return "diagnostics"


def get_group_title(group: str) -> str:
    return SENSOR_GROUP_TITLES.get(group, "Diagnostics")


def get_grouped_sensor_keys() -> Dict[str, list]:
    grouped: Dict[str, list] = {}
    for key in SENSORS:
        group = get_sensor_group(key)
        grouped.setdefault(group, []).append(key)
    for keys in grouped.values():
        keys.sort()
    return grouped

