import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import domain_report as d


def args(**changes):
    values = dict(no_ct=False, ct_providers=['certspotter', 'crtsh'], ct_workers=2,
                  ct_interval=2, ct_timeout=12, ct_domain_timeout=40, ct_dns_timeout=4,
                  ct_budget=600, ct_candidates=6, ct_sample=2, ct_pages=2,
                  ct_max_bytes=4000000, ct_anonymous_limit=50, certspotter_key=None,
                  workers=32, dns_qps=40, dns_timeout=5, nameserver=None,
                  ipv6=False, dns_transport='doh')
    values.update(changes)
    return SimpleNamespace(**values)


class CandidateTests(unittest.TestCase):
    def test_nameserver_zones_prefer_observed_ns_names(self):
        names = d.ct_candidates('awsdns-00.co.uk', [{'dns_names': [
            'www.awsdns-00.co.uk', 'tns-1543.awsdns-00.co.uk', 's-1536.awsdns-00.co.uk',
            'ns-1536.awsdns-00.co.uk', 'ns-1543.awsdns-00.co.uk']}], 2)
        self.assertEqual(set(names), {'ns-1536.awsdns-00.co.uk', 'ns-1543.awsdns-00.co.uk'})

    def test_scope_idn_wildcards_duplicates_and_newest(self):
        certificates = [
            {'not_before': '2020-01-01', 'name_value': 'old.example.com\nwww.example.com\n*.example.com'},
            {'not_before': '2026-01-01', 'dns_names': ['api.example.com', 'www.example.com',
                'example.com', 'evil-example.com', 'example.com.evil.org', '*.deep.example.com',
                'https://url.example.com', 'name@user.example.com', 'пример.example.com', 'new.example.com']},
            None]
        names = d.ct_candidates('example.com', certificates, 20)
        self.assertEqual(names[:2], ['www.example.com', 'api.example.com'])
        self.assertIn('xn--e1afmkfd.example.com', names)
        self.assertEqual(len(names), 5)
        self.assertLess(names.index('new.example.com'), names.index('old.example.com'))

    def test_preserve_actual_subdomain_per_ip(self):
        rows = d.make_rows(['example.com'], {'example.com': {'ips': ['8.8.8.8', '1.1.1.1'],
            'status': 'CT', 'resolved_hosts': {'www.example.com': ['8.8.8.8'], 'api.example.com': ['1.1.1.1']}}}, {}, {})
        mapping = {row['ip']: row['resolved_via'] for row in rows}
        self.assertEqual(mapping, {'8.8.8.8': 'www.example.com', '1.1.1.1': 'api.example.com'})
        self.assertTrue(all(row['domain'] == 'example.com' and row['resolution_method'] == 'ct' for row in rows))


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_failed_domains_and_stop_after_two_live_names(self):
        calls, dns_calls = [], []
        class FakeCT:
            def __init__(self, *a): pass
            async def discover(self, domain, provider):
                calls.append((domain, provider))
                return {'names': ['www.'+domain, 'api.'+domain, 'unused.'+domain], 'status': 'OK'}
            async def close(self): pass
        class FakeDNS:
            def __init__(self, *a): pass
            async def domain(self, name):
                dns_calls.append(name)
                return {'ips': ['8.8.8.8'], 'status': 'A: OK'}
            async def close(self): pass
        data = {'direct.example': {'ips': ['1.1.1.1'], 'status': 'A: OK'},
                'missing.example': {'ips': [], 'status': 'A: NoAnswer'},
                '8.8.8.8': {'ips': [], 'status': 'error'}}
        with patch.object(d, 'CTClient', FakeCT), patch.object(d, 'DNSClient', FakeDNS):
            errors = await d.ct_fallback(data, None, args())
        self.assertEqual(calls, [('missing.example', 'certspotter')])
        self.assertEqual(dns_calls, ['www.missing.example', 'api.missing.example'])
        self.assertEqual(data['direct.example'], {'ips': ['1.1.1.1'], 'status': 'A: OK'})
        self.assertEqual(data['missing.example']['ips'], ['8.8.8.8'])
        self.assertEqual(data['missing.example']['ct']['status'], 'resolved')
        self.assertEqual(errors, [])

    async def test_all_successful_means_no_ct_client_created(self):
        with patch.object(d, 'CTClient') as factory:
            await d.ct_fallback({'ok.example': {'ips': ['1.1.1.1']}}, None, args())
        factory.assert_not_called()

    async def test_domain_timeout_is_not_reported_as_empty_ct(self):
        class FakeCT:
            def __init__(self, *a): pass
            async def discover(self, *a): await asyncio.Event().wait()
            async def close(self): pass
        class FakeDNS:
            def __init__(self, *a): pass
            async def close(self): pass
        data = {'missing.example': {'ips': [], 'status': 'A: NoAnswer'}}
        with patch.object(d, 'CTClient', FakeCT), patch.object(d, 'DNSClient', FakeDNS):
            errors = await d.ct_fallback(data, None, args(ct_domain_timeout=.01))
        self.assertEqual(data['missing.example']['ct']['status'], 'domain_timeout')
        self.assertTrue(errors)

    async def test_http_429_stops_provider_for_other_domains_and_persists(self):
        class Response:
            status = 429
            headers = {'Retry-After': '7200'}
            url = 'https://api.certspotter.com/v1/issuances?domain=missing.example'
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
        class Session:
            calls = 0
            def get(self, *a, **kw):
                self.calls += 1
                return Response()
            async def close(self): pass
        with tempfile.TemporaryDirectory() as directory:
            cache = d.Cache(Path(directory)/'cache.sqlite')
            client = d.CTClient(cache, args())
            client.session = Session()
            first = await client.discover('missing.example', 'certspotter')
            second = await client.discover('another.example', 'certspotter')
            self.assertEqual(first['status'], 'unavailable')
            self.assertEqual(second['status'], 'deferred')
            self.assertEqual(client.session.calls, 1)
            self.assertGreater(cache.get('cooldown', 'ct:certspotter')['until'], d.time.time()+7190)
            self.assertIsNone(cache.get('ct-v1', 'certspotter|missing.example'))
            await client.close()
            cache.close()

    async def test_transient_errors_open_circuit_without_negative_cache(self):
        class Session:
            calls = 0
            def get(self, *a, **kw):
                self.calls += 1
                raise asyncio.TimeoutError()
            async def close(self): pass
        with tempfile.TemporaryDirectory() as directory:
            cache = d.Cache(Path(directory)/'cache.sqlite')
            client = d.CTClient(cache, args())
            client.session = Session()
            with patch.object(d.Pacer, 'wait', AsyncMock()):
                for index in range(10):
                    await client.discover(f'host{index}.example', 'crtsh')
            self.assertEqual(client.session.calls, 3)
            self.assertIsNone(cache.get('ct-v1', 'crtsh|host0.example'))
            await client.close()
            cache.close()


if __name__ == '__main__':
    unittest.main()
