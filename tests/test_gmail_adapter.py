"""Gmail adapter tests with recorded provider responses: configuration, PKCE, minimal scopes, parsing, pagination,
expired and revoked tokens, rate limits, inaccessible messages and token secrecy. No network access."""
import base64
import hashlib
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gmail_adapter import (COMPOSE_SCOPE, READONLY_SCOPE, SEND_SCOPE, GmailClient, GmailConfig, GmailError, authorization_url,
                           connection_status, parse_message, pkce_pair)

TOKEN = 'ya29.SECRET-ACCESS-TOKEN'


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


MESSAGE = {
    'id': 'm1', 'threadId': 't1', 'internalDate': '1767225600000', 'labelIds': ['INBOX'],
    'payload': {'mimeType': 'multipart/alternative', 'headers': [{'name': 'From', 'value': 'Alex <alex@example.com>'},
                                                                 {'name': 'Subject', 'value': 'Proposal'}],
                'parts': [{'mimeType': 'text/html', 'body': {'data': b64('<p>html</p>')}},
                          {'mimeType': 'text/plain', 'body': {'data': b64('Could you send the proposal by Wednesday?')}}]},
}


class Recorder:
    """A transport that replays scripted responses and records every request it receives."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers.get('Authorization')))
        item = self.script.pop(0)
        return item


class ConfigAndStatus(unittest.TestCase):
    def test_missing_configuration_is_not_configured(self):
        self.assertIsNone(GmailConfig.from_env({}))
        self.assertEqual(connection_status(None, has_token=False)['status'], 'NOT_CONFIGURED')

    def test_configured_without_a_token_is_not_connected_and_revoked_is_reported(self):
        cfg = GmailConfig.from_env({'GOOGLE_OAUTH_CLIENT_ID': 'id', 'GOOGLE_OAUTH_CLIENT_SECRET': 's', 'GOOGLE_OAUTH_REDIRECT_URI': 'http://x/cb'})
        self.assertEqual(connection_status(cfg, has_token=False)['status'], 'NOT_CONNECTED')
        self.assertEqual(connection_status(cfg, has_token=True)['status'], 'CONNECTED')
        self.assertEqual(connection_status(cfg, has_token=True, revoked=True)['status'], 'REVOKED')


class OAuth(unittest.TestCase):
    def setUp(self):
        self.cfg = GmailConfig('client-id', 'client-secret', 'http://127.0.0.1:8765/api/inbox/oauth/callback')

    def test_authorization_url_uses_pkce_s256_offline_access_and_read_only_by_default(self):
        verifier, challenge = pkce_pair()
        self.assertEqual(challenge, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode())
        q = parse_qs(urlparse(authorization_url(self.cfg, 'state-1', challenge)).query)
        self.assertEqual(q['code_challenge_method'], ['S256'])
        self.assertEqual(q['access_type'], ['offline'])
        self.assertEqual(q['scope'], [READONLY_SCOPE])
        self.assertNotIn(SEND_SCOPE, q['scope'][0])
        self.assertNotIn(COMPOSE_SCOPE, q['scope'][0])

    def test_send_and_compose_scopes_are_only_requested_when_asked(self):
        _, challenge = pkce_pair()
        q = parse_qs(urlparse(authorization_url(self.cfg, 's', challenge, [COMPOSE_SCOPE])).query)
        self.assertIn(COMPOSE_SCOPE, q['scope'][0])
        self.assertNotIn(SEND_SCOPE, q['scope'][0])


class Parsing(unittest.TestCase):
    def test_multipart_message_yields_the_plain_text_body_and_provider_ids(self):
        m = parse_message(MESSAGE)
        self.assertEqual((m['provider_message_id'], m['provider_thread_id']), ('m1', 't1'))
        self.assertEqual(m['body'], 'Could you send the proposal by Wednesday?')
        self.assertEqual(m['sender'], 'Alex <alex@example.com>')
        self.assertAlmostEqual(m['sent_at'], 1767225600.0)

    def test_missing_plain_text_part_gives_an_empty_body_not_a_guess(self):
        m = parse_message({'id': 'm2', 'payload': {'mimeType': 'text/html', 'headers': [], 'body': {'data': b64('<p>x</p>')}}})
        self.assertEqual(m['body'], '')


class Pagination(unittest.TestCase):
    def test_all_pages_are_fetched_once_and_each_message_is_returned_once(self):
        page1 = (200, {'messages': [{'id': 'm1'}], 'nextPageToken': 'P2'}, {})
        page2 = (200, {'messages': [{'id': 'm2'}]}, {})
        get1 = (200, MESSAGE, {})
        get2 = (200, dict(MESSAGE, id='m2'), {})
        t = Recorder([page1, get1, page2, get2])
        out = GmailClient(t, TOKEN).sync_all()
        self.assertEqual([m['provider_message_id'] for m in out], ['m1', 'm2'])
        self.assertIn('pageToken=P2', t.calls[2][1])

    def test_inaccessible_message_is_reported_as_not_found(self):
        t = Recorder([(404, {}, {})])
        with self.assertRaises(GmailError) as ctx:
            GmailClient(t, TOKEN).get_message('gone')
        self.assertEqual(ctx.exception.code, 'NOT_FOUND')


class TokenFailures(unittest.TestCase):
    def test_expired_token_is_refreshed_once_and_the_request_retried(self):
        t = Recorder([(401, {}, {}), (200, {'messages': []}, {})])
        client = GmailClient(t, TOKEN, refresh=lambda: 'NEW-TOKEN')
        client.list_page()
        self.assertEqual(t.calls[1][2], 'Bearer NEW-TOKEN')

    def test_expired_token_that_cannot_be_refreshed_is_reported_without_the_token(self):
        t = Recorder([(401, {}, {})])
        with self.assertRaises(GmailError) as ctx:
            GmailClient(t, TOKEN).list_page()
        self.assertEqual(ctx.exception.code, 'TOKEN_EXPIRED')
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_revoked_access_stops_immediately(self):
        t = Recorder([(403, {'error': 'invalid_grant'}, {})])
        with self.assertRaises(GmailError) as ctx:
            GmailClient(t, TOKEN, refresh=lambda: 'x').list_page()
        self.assertEqual(ctx.exception.code, 'REVOKED')
        self.assertEqual(len(t.calls), 1)

    def test_rate_limit_waits_then_succeeds(self):
        slept = []
        t = Recorder([(429, {}, {'Retry-After': '2'}), (200, {'messages': []}, {})])
        GmailClient(t, TOKEN, sleep=slept.append).list_page()
        self.assertEqual(slept, [2.0])

    def test_persistent_rate_limit_is_reported_with_retry_after(self):
        t = Recorder([(429, {}, {'Retry-After': '5'})] * 4)
        with self.assertRaises(GmailError) as ctx:
            GmailClient(t, TOKEN, sleep=lambda s: None, max_retries=3).list_page()
        self.assertEqual((ctx.exception.code, ctx.exception.retry_after), ('RATE_LIMITED', 5.0))

    def test_provider_outage_is_typed(self):
        t = Recorder([(503, {}, {})])
        with self.assertRaises(GmailError) as ctx:
            GmailClient(t, TOKEN).list_page()
        self.assertEqual(ctx.exception.code, 'PROVIDER_ERROR')


if __name__ == '__main__':
    unittest.main()
