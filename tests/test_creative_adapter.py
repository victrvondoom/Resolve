"""Tests for CreativeAdapter (cu_holodev and cu_handyman)."""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from creative_adapter import CreativeAdapter, CreativeError, MeshSpec, WalkthroughStep


class CreativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = CreativeAdapter(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_holodev_mesh_generation_and_disk_hash_verification(self):
        spec = MeshSpec(name='test_cube', dimensions=(2.0, 2.0, 2.0), segments=1, material='metallic')
        res = self.adapter.generate_procedural_mesh(spec, 'cube.obj')
        self.assertEqual(res['vertex_count'], 8)
        self.assertEqual(res['face_count'], 6)
        file_path = Path(res['path'])
        self.assertTrue(file_path.is_file())
        raw = file_path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), res['sha256'])

    def test_holodev_refuses_invalid_mesh_dimensions(self):
        spec = MeshSpec(name='bad_mesh', dimensions=(0.0, 1.0, 1.0), segments=1, material='default')
        with self.assertRaises(CreativeError) as ctx:
            self.adapter.generate_procedural_mesh(spec, 'bad.obj')
        self.assertEqual(ctx.exception.code, 'INVALID_DIMENSIONS')

    def test_handyman_walkthrough_compilation(self):
        steps = [
            WalkthroughStep(step_number=1, target_element_id='btn_new', bounding_box=(100, 200, 80, 30), narration='Click New to begin', action_type='click'),
            WalkthroughStep(step_number=2, target_element_id='input_name', bounding_box=(100, 250, 200, 40), narration='Enter project title', action_type='type'),
        ]
        res = self.adapter.compile_walkthrough('Editor App', steps)
        self.assertEqual(res['step_count'], 2)
        self.assertEqual(res['steps'][0]['center'], {'x': 140, 'y': 215})
        self.assertTrue(bool(res['plan_digest']))


if __name__ == '__main__':
    unittest.main()
