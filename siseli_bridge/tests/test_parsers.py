import unittest
import sys
import os
import base64
from unittest import mock
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.siseli_bridge.parsers import decode_remaining_length, extract_mqtt_packets_from_stream, validate_publish_packet, SolarParser
from src.siseli_bridge import parsers as parser_module
from src.siseli_bridge import state as shared_state

class TestParsers(unittest.TestCase):
    def setUp(self):
        self._last_state_backup = dict(shared_state.LAST_STATE)

    def tearDown(self):
        shared_state.LAST_STATE.clear()
        shared_state.LAST_STATE.update(self._last_state_backup)

    def test_decode_remaining_length_single_byte(self):
        # Length 5 takes 1 byte
        buf = b'\x03\x05\x01\x02\x03\x04\x05'
        val, idx = decode_remaining_length(buf, 1)
        self.assertEqual(val, 5)
        self.assertEqual(idx, 2)

    def test_decode_remaining_length_multi_byte(self):
        # Length 321 evaluates to \xc1\x02
        buf = b'\x03\xc1\x02' + b'x' * 321
        val, idx = decode_remaining_length(buf, 1)
        self.assertEqual(val, 321)
        self.assertEqual(idx, 3)

    def test_validate_publish_packet_valid(self):
        # Type 3 (Publish), Length 6 -> total 8 bytes
        # Topic len is 3 ("a/b") -> 97, 47, 98 -> \x00\x03a/b
        packet = b'\x30\x06\x00\x03a/b\x99'
        self.assertTrue(validate_publish_packet(packet))

    def test_validate_publish_packet_invalid_type(self):
        packet = b'\x40\x06\x00\x03a/b\x99' # Type 4
        self.assertFalse(validate_publish_packet(packet))

    def test_solar_parser_safe_b64decode(self):
        self.assertEqual(SolarParser._safe_b64decode("dGVzdA=="), b"test")
        self.assertEqual(SolarParser._safe_b64decode("dGVzdA"), b"test")
        self.assertIsNone(SolarParser._safe_b64decode(""))

    def test_stream_assembler(self):
        stream = bytearray(b'\x30\x06\x00\x03a/b\x99') # Valid Pub
        stream.extend(b'\x30\x06') # Partial Pub
        
        packets = extract_mqtt_packets_from_stream(stream)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0], b'\x30\x06\x00\x03a/b\x99')
        
        # Assert partial bytes were properly left intact in stream
        self.assertEqual(stream, bytearray(b'\x30\x06'))

    def test_scale_main_power_uses_count_only(self):
        with mock.patch("src.siseli_bridge.parsers.INVERTER_COUNT", 3):
            self.assertEqual(SolarParser._scale_main_power(100), 300)

    def test_ascii_schema_normalizes_software_version(self):
        state = SolarParser._try_ascii_schema({
            "hR6Y": b"(0010.11 20250630 14)"
        })

        self.assertEqual(state["firmware_version"], "0010.11")
        self.assertEqual(state["software_version"], "10.11")

    def test_ascii_schema_rounds_output_set_frequency_to_app_style(self):
        state = SolarParser._try_ascii_schema({
            "2l0E": b"(229.8 49.9 252 129 2 24 11000 006.1 0044)"
        })

        self.assertEqual(state["out_hz"], 49.9)
        self.assertEqual(state["output_set_frequency"], 50)

    def test_ascii_schema_93vq_preset_sets_charging_light_to_flicker(self):
        state = SolarParser._try_ascii_schema({
            "93VQ": b"(1 050 010 13310110230 011 1 1 0 1 1 015 035 050 025 056.4 056.4 042.0 020 0 0)"
        })

        self.assertEqual(state["charging_light_status"], "Flicker")
        self.assertEqual(state["mains_light_status"], "Flicker")

    def test_ascii_schema_eo8w_preset_sets_charging_light_to_flicker(self):
        state = SolarParser._try_ascii_schema({
            "eo8w": b"(00 B0100000000000 20211002110B117020000)"
        })

        self.assertEqual(state["charging_light_status"], "Flicker")
        self.assertEqual(state["mains_light_status"], "Flicker")

    def test_ascii_schema_parses_bms_average_temperature_from_yavb_tail(self):
        state = SolarParser._try_ascii_schema({
            "Yavb": b"(04 1001100000000000 042.0 057.6 195.0 054 0022.3 0000.0 02921 000000 18.95)"
        })

        self.assertEqual(state["bms_current_soc"], 54)
        self.assertEqual(state["bms_charging_current_a"], 22.3)
        self.assertEqual(state["bms_discharge_current_a"], 0.0)
        self.assertEqual(state["bms_avg_temp_c"], 18.95)

    def test_ascii_schema_yavb_without_tail_keeps_bms_average_temperature_absent(self):
        state = SolarParser._try_ascii_schema({
            "Yavb": b"(04 1001100000000000 042.0 057.6 195.0 054 0022.3 0000.0 02921 000000)"
        })

        self.assertNotIn("bms_avg_temp_c", state)

    def test_binary_ogbu_matches_verified_portal_telemetry(self):
        # Captured from a real dev_rpc_reply.  The portal reported 0.0 V/Hz
        # input, 120.0 V / 60.0 Hz output, 52.6 V battery and 95% SOC.
        raw = base64.b64decode(
            "VlBIQlEMBAAAAAAAAAACCjwAAAAAsARYAg4CXwABKRQIbQMGBAEBQAAEKRR9fX19fX0CAgEBMhSOjnR4iPACAgEDAQICAgAAAXiLBgMCCjBQUHwCAEg="
        )
        state = SolarParser._try_ascii_schema({"OgBU": raw})

        self.assertEqual(state["firmware_version"], "2.10")
        self.assertEqual(state["model_code"], "60")
        self.assertEqual(state["grid_v"], 0.0)
        self.assertEqual(state["grid_hz"], 0.0)
        self.assertEqual(state["out_v"], 120.0)
        self.assertEqual(state["out_hz"], 60.0)
        self.assertEqual(state["bat_v"], 52.6)
        self.assertEqual(state["bat_cap"], 95)
        self.assertEqual(state["bms_current_soc"], 95)
        self.assertEqual(state["load_pct"], 1)
        self.assertEqual(state["inverter_temperature_c"], 41.0)
        self.assertEqual(state["pv_v"], 64.0)
        self.assertEqual(state["controller_charging_current_a"], 4.0)

    def test_binary_ogbu_maps_verified_controller_status(self):
        # Captured with the Siseli table showing: 26% load, 42 C device,
        # inversion mode, four batteries, controller 1.01 / 60 V / 1 A / 40 C.
        raw = base64.b64decode(
            "VlBIQlEMBAAAAAAAAAACCjwAAAAArgRYAg0CVwAaKhQEbgMGBAEBPAABKBR9fX19fX0CAgEBMhSOjnR4iPACAgEDAQICAgADAXiLBgMCCjBQUHwCAE8="
        )
        state = SolarParser._try_ascii_schema({"OgBU": raw})

        self.assertEqual(state["bat_cap"], 87)
        self.assertEqual(state["load_pct"], 26)
        self.assertEqual(state["inverter_temperature_c"], 42.0)
        self.assertEqual(state["status_code"], 20)
        self.assertEqual(state["mode"], "Inversion Mode")
        self.assertEqual(state["bat_series_count"], 4.0)
        self.assertEqual(state["controller_program_version"], "1.01")
        self.assertEqual(state["pv_v"], 60.0)
        self.assertEqual(state["controller_charging_current_a"], 1.0)
        self.assertEqual(state["pv_current_a"], 1.0)
        self.assertEqual(state["pv_w"], 52)
        self.assertEqual(state["generation_power_w"], 52)
        self.assertEqual(state["controller_temp_c"], 40.0)
        self.assertEqual(state["controller_status_code"], 20)

    def test_binary_ogbu_maps_operation_code_six_to_inversion_mode(self):
        raw = base64.b64decode(
            "VlBIQlEMBAAAAAAAAAACCjwAAAAArgRYAg8CVQAIKRQGbgMGBAEBUwAAKBR9fX19fX0CAgEBMhSOjnR4iPACAgEDAQICAgABAXiLBgMCCjBQUHwCAFI="
        )
        state = SolarParser._try_ascii_schema({"OgBU": raw})

        self.assertEqual(state["mode"], "Inversion Mode")
        self.assertEqual(state["load_pct"], 8)
        self.assertEqual(state["pv_v"], 83.0)

    def test_binary_ogbu_derives_load_power_and_battery_discharge(self):
        # Captured while the portal showed 8% load, 52.8 V battery and 400 W
        # AC output. At 5 kW rated output: 8% = 400 W; 400 / 52.8 = 7.58 A.
        raw = base64.b64decode(
            "VlBIQlEMBAAAAAAAAAACCjwAAAAArARYAhACVQAIKBQGbgMGBAEBUwAAJxR9fX19fX0CAgEBMhSOjnR4iPACAgEDAQICAgABAXiLBgMCCjBQUHwCAE8="
        )
        state = SolarParser._try_ascii_schema({"OgBU": raw})

        self.assertEqual(state["load_pct"], 8)
        self.assertEqual(state["load_w"], 400)
        self.assertEqual(state["dischg_current"], 7.58)
        self.assertEqual(state["bms_discharge_current_a"], 7.58)
        self.assertEqual(state["battery_status"], "Discharge")
        self.assertEqual(state["pv_temp"], 39.0)

    def test_binary_ogbu_truncated_31_bytes_does_not_crash(self):
        """Fix 1: a 31-byte VPHBQ block must not raise IndexError.

        Previously the guard was ``len > 30`` but the code indexed [31] and
        [32], so a 31- or 32-byte block raised IndexError (swallowed by the
        broad handler, silently discarding all telemetry).  With staged
        guards, a 31-byte block yields load_pct and inverter_temperature_c
        but skips status_code and mode.
        """
        # Build a 31-byte VPHBQ block: 5-byte header + 26 bytes of data.
        raw = b"VPHBQ" + b"\x00" * 26
        self.assertEqual(len(raw), 31)
        state = SolarParser._try_ascii_schema({"OgBU": raw})
        # Should not crash; should yield the fields up to index 30.
        self.assertIn("load_pct", state)
        self.assertIn("inverter_temperature_c", state)
        # status_code and mode require indices 31 and 32, absent at len 31.
        self.assertNotIn("status_code", state)
        self.assertNotIn("mode", state)

    def test_binary_ogbu_truncated_32_bytes_does_not_crash(self):
        """Fix 1: a 32-byte VPHBQ block must yield status_code but not mode."""
        raw = b"VPHBQ" + b"\x00" * 27
        self.assertEqual(len(raw), 32)
        state = SolarParser._try_ascii_schema({"OgBU": raw})
        self.assertIn("load_pct", state)
        self.assertIn("inverter_temperature_c", state)
        self.assertIn("status_code", state)
        # mode requires index 32, absent at len 32.
        self.assertNotIn("mode", state)

    def test_binary_ogbu_truncated_29_bytes_yields_no_compact_fields(self):
        """Fix 1: a 29-byte block (len > 29 is False) skips all compact fields."""
        raw = b"VPHBQ" + b"\x00" * 24
        self.assertEqual(len(raw), 29)
        state = SolarParser._try_ascii_schema({"OgBU": raw})
        # Core fields from the >= 28 block are still present.
        self.assertIn("bat_v", state)
        # Compact fields start at index 29, absent at len 29 (guard is > 29).
        self.assertNotIn("load_pct", state)

if __name__ == '__main__':
    unittest.main()
