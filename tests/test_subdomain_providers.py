import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import domain_report as d
from scanner import subdomain_providers as sources
from tests.test_ct_fallback import args


class AdapterTests(unittest.TestCase):
    def test_documented_payloads_and_in_scope_filter(self):
        cases = {
            'subdomaincenter': ['www.example.com', 'foreign.test', '*.example.com'],
            'chaos': {'subdomains': ['www', 'nested.api']},
            'bufferover': {'Results': ['1.1.1.1,hash,organization,www.example.com']},
            'netlas': [{'data': {'certificate': {'names': ['www.example.com'],
                        'extensions': {'subject_alt_name': {'dns_names': ['api.example.com']}}}}}],
            'censys': {'result': {'hits': [{'cert': {'names': ['www.example.com']}}]}}
        }
        for provider, payload in cases.items():
            raw = sources.extract_names(provider, payload, 'example.com')
            names = d.ct_candidates('example.com', [{'dns_names': raw}], 6)
            self.assertIn('www.example.com', names)
            self.assertNotIn('foreign.test', names)
            self.assertNotIn('*.example.com', names)

    def test_credentials_stay_in_headers(self):
        for provider in sources.ENDPOINTS:
            method, url, options = sources.request_spec(provider, 'example.com', 'SECRET_TEST')
            self.assertNotIn('SECRET_TEST', url)
            self.assertNotIn('SECRET_TEST', str(options.get('params', {})))
            self.assertIn('SECRET_TEST', str(options['headers']))

    def test_unexpected_schema_is_error_not_empty(self):
        for provider in sources.ENDPOINTS:
            with self.assertRaises(ValueError):
                sources.extract_names(provider, {'message': 'unavailable'}, 'example.com')

    def test_netlas_unwrapped_document(self):
        payload = [{'data': {'names': ['www.example.com'], 'extensions': {
            'subject_alt_name': {'dns_names': ['api.example.com']}}}}]
        self.assertEqual(sources.extract_names('netlas', payload, 'example.com'),
                         ['www.example.com', 'api.example.com'])


class AsyncAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_never_sends_request(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = d.Cache(Path(folder)/'cache.sqlite')
            with patch.object(sources, 'get_key', return_value=''):
                client = sources.ExternalAPIClient(cache, args(), d.ct_candidates, d.Pacer, d.retry_after)
            for provider in ('chaos', 'bufferover', 'netlas', 'censys'):
                self.assertEqual((await client.discover('example.com', provider))['status'], 'not_configured')
            self.assertIsNone(client.session)
            self.assertGreaterEqual(client.paces['subdomaincenter'].interval, 12)
            self.assertGreaterEqual(client.paces['netlas'].interval, 20)
            await client.close()
            cache.close()

    async def test_subdomaincenter_success_is_cached_and_filtered(self):
        class Content:
            async def iter_chunked(self, size):
                yield json.dumps(['www.example.com', 'api.example.com', 'www.other.com', '*.example.com']).encode()
        class Response:
            status = 200
            url = 'https://api.subdomain.center/?domain=example.com'
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def raise_for_status(self): pass
        class Session:
            calls = 0
            def request(self, *a, **kw):
                self.calls += 1
                return Response()
            async def close(self): pass
        with tempfile.TemporaryDirectory() as folder:
            cache = d.Cache(Path(folder)/'cache.sqlite')
            client = sources.ExternalAPIClient(cache, args(), d.ct_candidates, d.Pacer, d.retry_after)
            client.session = Session()
            first = await client.discover('example.com', 'subdomaincenter')
            second = await client.discover('example.com', 'subdomaincenter')
            self.assertEqual(first['names'], ['www.example.com', 'api.example.com'])
            self.assertTrue(second['cached'])
            self.assertEqual(client.session.calls, 1)
            self.assertIsNotNone(cache.get('cooldown', 'ct:subdomaincenter'))
            await client.close()
            cache.close()

    async def test_long_retry_after_does_not_become_no_results(self):
        class Response:
            status = 429
            url = 'https://api.subdomain.center/?domain=example.com'
            headers = {'Retry-After': '300'}
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
        class Session:
            calls = 0
            def request(self, *a, **kw):
                self.calls += 1
                return Response()
            async def close(self): pass
        with tempfile.TemporaryDirectory() as folder:
            cache = d.Cache(Path(folder)/'cache.sqlite')
            client = sources.ExternalAPIClient(cache, args(), d.ct_candidates, d.Pacer, d.retry_after)
            client.session = Session()
            first = await client.discover('example.com', 'subdomaincenter')
            second = await client.discover('other.com', 'subdomaincenter')
            self.assertEqual(first['status'], 'deferred')
            self.assertEqual(second['status'], 'deferred')
            self.assertEqual(client.session.calls, 1)
            self.assertGreater(cache.get('cooldown', 'ct:subdomaincenter')['until'], d.time.time()+290)
            self.assertIsNone(cache.get('subdomain-v2', 'subdomaincenter|example.com'))
            await client.close()
            cache.close()

    async def test_certspotter_domain_restriction_does_not_disable_other_domains(self):
        class Content:
            async def read(self, n): return b'{"code":"not_allowed_by_plan","message":"domain not available"}'
            async def iter_chunked(self, size): yield b'[{"dns_names":["www.allowed.com"]}]'
        class Response:
            headers = {}
            url = 'https://api.certspotter.com/v1/issuances'
            content = Content()
            def __init__(self, status): self.status = status
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def raise_for_status(self): pass
        class Session:
            def get(self, url, **kw): return Response(403 if kw['params']['domain']=='restricted.com' else 200)
            async def close(self): pass
        with tempfile.TemporaryDirectory() as folder:
            cache = d.Cache(Path(folder)/'cache.sqlite')
            client = d.CTClient(cache, args())
            client.session = Session()
            with patch.object(d.Pacer, 'wait', AsyncMock()):
                restricted = await client.discover('restricted.com', 'certspotter')
                allowed = await client.discover('allowed.com', 'certspotter')
            self.assertEqual(restricted['status'], 'restricted')
            self.assertEqual(allowed['names'], ['www.allowed.com'])
            self.assertNotIn('certspotter', client.disabled)
            await client.close()
            cache.close()


if __name__ == '__main__':
    unittest.main()
