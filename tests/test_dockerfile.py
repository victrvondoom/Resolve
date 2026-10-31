"""The production image must contain every source directory the server imports.

Regression: the image crashed on boot (ModuleNotFoundError: web_fixtures) because apps/ was not copied.
Found by scripts/docker_smoke.py. This check is static so it runs without Docker.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DockerfileTests(unittest.TestCase):
    def test_every_imported_source_dir_is_copied(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        copied = set(re.findall(r'^COPY\s+(\S+?)/?\s+\./', dockerfile, re.M))
        needed = set()
        for py in (ROOT / 'src').glob('*.py'):
            needed |= set(re.findall(r"sys\.path\.insert\(0, str\(ROOT / '([A-Za-z_]+)'\)\)", py.read_text()))
        self.assertIn('src', copied)
        self.assertTrue(needed, 'expected sys.path inserts in src/')
        self.assertFalse(needed - copied, f'Dockerfile does not COPY {sorted(needed - copied)}')

    def test_dockerignore_keeps_secrets_and_models_out(self):
        ignored = (ROOT / '.dockerignore').read_text().split()
        for entry in ('.git', '.venv', 'data/models', 'data/*.sqlite3*'):
            self.assertIn(entry, ignored)


if __name__ == '__main__':
    unittest.main()
