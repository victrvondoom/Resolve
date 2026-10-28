"""Tests for ScienceAdapter (cu_arcflash_copilot and cu_geowave)."""
import sys
import unittest
from pathlib import Path
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from science_adapter import (
    ArcFlashInput,
    GeoWaveInput,
    ScienceAdapter,
    ScienceError,
    verify_arcflash_oracle,
)


class ScienceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = ScienceAdapter(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_arcflash_calculation_and_oracle_verification(self):
        inp = ArcFlashInput(
            system_voltage_kv=0.48,
            bolted_fault_current_ka=25.0,
            clearing_time_sec=0.1,
            working_distance_mm=455.0,
        )
        res = self.adapter.calculate_arc_flash(inp)
        self.assertGreater(res.incident_energy_cal_cm2, 0.0)
        self.assertGreater(res.arc_flash_boundary_mm, 100.0)
        self.assertIn(res.ppe_category, (1, 2, 3, 4, 5))
        self.assertTrue(verify_arcflash_oracle(inp, res))

    def test_arcflash_refuses_invalid_parameters(self):
        with self.assertRaises(ScienceError) as ctx:
            self.adapter.calculate_arc_flash(
                ArcFlashInput(system_voltage_kv=-1.0, bolted_fault_current_ka=20.0, clearing_time_sec=0.1, working_distance_mm=455.0)
            )
        self.assertEqual(ctx.exception.code, 'INVALID_VOLTAGE')

    def test_geowave_propagation_and_attenuation(self):
        inp = GeoWaveInput(
            epicentral_distance_km=60.0,
            p_wave_velocity_km_s=6.0,
            s_wave_velocity_km_s=3.5,
            initial_amplitude=100.0,
        )
        res = self.adapter.calculate_geowave(inp)
        self.assertEqual(res.p_wave_arrival_time_s, 10.0)
        self.assertAlmostEqual(res.s_wave_arrival_time_s, 17.143, places=2)
        self.assertGreater(res.s_minus_p_interval_s, 7.0)
        self.assertLess(res.attenuated_amplitude, 100.0)
        self.assertTrue(bool(res.calculation_digest))


if __name__ == '__main__':
    unittest.main()
