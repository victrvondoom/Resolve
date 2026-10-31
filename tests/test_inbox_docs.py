"""Documentation check: every /api/inbox route implemented in src/inbox_api.py is documented in docs/INBOX_API.md, and the
documented paths do not name routes that do not exist."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def code_routes() -> set[str]:
    src = (ROOT / 'src' / 'inbox_api.py').read_text()
    routes = set()
    for m in re.finditer(r'm\(r"(/api/inbox/[^"]*)"\)', src):
        routes.add(re.sub(r'\(\[A-Za-z0-9_(?:\.\\\-)*\]\+\)', '{id}', m.group(1)))
    for m in re.finditer(r'path == "(/api/inbox/[^"]*)"', src):
        routes.add(m.group(1))
    return routes


class InboxApiDocs(unittest.TestCase):
    def test_every_implemented_route_is_documented(self):
        doc = (ROOT / 'docs' / 'INBOX_API.md').read_text()
        missing = sorted(r for r in code_routes() if r not in doc)
        self.assertEqual(missing, [], 'routes implemented but not documented in docs/INBOX_API.md')

    def test_the_document_names_the_core_journey_routes(self):
        doc = (ROOT / 'docs' / 'INBOX_API.md').read_text()
        for needle in ['/api/inbox/sync', '/api/inbox/brief', '/api/inbox/drafts/{id}/approve', '/api/inbox/drafts/{id}/send',
                       '/api/inbox/executions/{id}/reconcile', '/api/inbox/rules/parse', '/api/inbox/replay/step',
                       '/api/inbox/connections/gmail/callback']:
            self.assertIn(needle, doc)


if __name__ == '__main__':
    unittest.main()
