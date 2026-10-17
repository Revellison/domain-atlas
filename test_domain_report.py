import asyncio
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import domain_report as d


class CoreTests(unittest.TestCase):
    def test_domain_normalization(self):
        self.assertEqual(d.normalize_domain("https://EXAMPLE.com/path"), "example.com")
        self.assertEqual(d.normalize_domain("*.пример.рф"), "xn--e1afmkfd.xn--p1ai")
        self.assertEqual(d.normalize_domain("[2001:4860:4860::8888]"), "2001:4860:4860::8888")
        self.assertIsNone(d.normalize_domain("Домен"))
        with self.assertRaises(ValueError):
            d.normalize_domain("bad domain.com")

    def test_csv_input_column_header_and_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.csv"
            path.write_text("n;Домен\n1;example.com\n2;EXAMPLE.com\n3;bad domain\n", encoding="utf-8-sig")
            domains, invalid = d.read_input(path, 2, 0)
            self.assertEqual(domains, ["example.com"])
            self.assertEqual(len(invalid), 1)

    def test_bulk_multiple_origins_ipv6_and_bad_prefix(self):
        text = """Bulk mode
13335 | 1.1.1.1 | 1.1.1.0/24 | AU | apnic | 2011-08-11 | CLOUDFLARENET
13335 15169 | 2001:4860::1 | 2001:4860::/32 | US | arin | | MULTI
13335 | 8.8.8.8 | 1.1.1.0/24 | | | | wrong
NA | 9.9.9.9 | NA | | | | NA
"""
        records = d.parse_bulk(text, {"1.1.1.1", "2001:4860::1", "8.8.8.8", "9.9.9.9"})
        self.assertEqual(records["2001:4860::1"][0]["asns"], [13335, 15169])
        self.assertNotIn("8.8.8.8", records)
        self.assertNotIn("9.9.9.9", records)

    def test_group_organization_and_asn_then_domain(self):
        domains = ["z.com", "a.com", "other.com", "same-org.com", "bad.com"]
        resolved = {name: {"ips": [f"8.8.8.{i+1}"], "status": "OK"}
                    for i, name in enumerate(domains[:-1])}
        resolved["bad.com"] = {"ips": [], "status": "NXDOMAIN"}
        records = {f"8.8.8.{i+1}": [{"prefix": "8.8.8.0/24", "asns": [asn], "name": "", "source": "test"}]
                   for i, asn in enumerate([20, 20, 10, 21])}
        metadata = {20: {"name": "Zulu", "org_id": 99, "website": "https://z.com"},
                    21: {"name": "Another network", "org_id": 99, "website": "https://z.com"},
                    10: {"name": "Middle", "org_id": 100, "website": ""}}
        rows = d.make_rows(domains, resolved, records, metadata)
        self.assertEqual([r["domain"] for r in rows], ["a.com", "z.com", "same-org.com", "other.com", "bad.com"])

    def test_no_invented_website_and_cloudflare_label(self):
        rows = d.make_rows(["example.com"], {"example.com": {"ips": ["1.1.1.1"], "status": "OK"}},
                           {"1.1.1.1": [{"prefix": "1.1.1.0/24", "asns": [13335], "name": "CF", "source": "test"}]}, {})
        self.assertEqual(rows[0]["operator"], "Cloudflare (CDN)")
        self.assertEqual(rows[0]["website"], "")

    def test_safe_output_and_retry_after(self):
        self.assertEqual(d.safe_website("example.com"), "https://example.com")
        self.assertEqual(d.safe_website("javascript://alert(1)"), "")
        self.assertEqual(d.csv_safe("=HYPERLINK()"), "'=HYPERLINK()")
        self.assertEqual(d.retry_after("120"), 120)
        self.assertEqual(d.retry_after("Thu, 01 Jan 1970 00:02:00 GMT", now=0), 120)

    def test_cache_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = d.Cache(Path(folder) / "cache.sqlite")
            cache.put("test", "a", {"value": 1}, 60)
            cache.put("test", "b", {"value": 2}, -1)
            self.assertEqual(cache.get("test", "a"), {"value": 1})
            self.assertIsNone(cache.get("test", "b"))
            cache.close()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_doh_ipv6_and_cname_ttl(self):
        from argparse import Namespace
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def raise_for_status(self): pass
            async def json(self, **kwargs):
                return {'Status': 0, 'Answer': [
                    {'type': 5, 'TTL': 10, 'data': 'target.example.'},
                    {'type': 28, 'TTL': 120, 'data': '2001:4860:4860::8888'}]}
        class Session:
            calls = 0
            def get(self, *args, **kwargs):
                self.calls += 1
                return Response()
            async def close(self): pass
        with tempfile.TemporaryDirectory() as folder:
            cache = d.Cache(Path(folder) / 'cache.sqlite')
            args = Namespace(nameserver=['1.1.1.1'], dns_timeout=1, workers=2, dns_qps=100, dns_transport='doh')
            client = d.DNSClient(cache, args)
            client.session = Session()
            result = await client.query('test.example', 'AAAA')
            self.assertEqual(result['values'], ['2001:4860:4860::8888'])
            self.assertEqual((await client.query('test.example', 'AAAA'))['values'], result['values'])
            self.assertEqual(client.session.calls, 1)
            expiry = cache.db.execute('select expires from cache').fetchone()[0]
            self.assertLessEqual(expiry - d.time.time(), 10)
            await client.close()
            cache.close()

    async def test_doh_servfail_is_not_cached_as_no_answer(self):
        from argparse import Namespace
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def raise_for_status(self): pass
            async def json(self, **kwargs): return {'Status': 2}
        class Session:
            def get(self, *args, **kwargs): return Response()
            async def close(self): pass
        with tempfile.TemporaryDirectory() as folder:
            cache = d.Cache(Path(folder) / 'cache.sqlite')
            args = Namespace(nameserver=['1.1.1.1'], dns_timeout=1, workers=2, dns_qps=100, dns_transport='doh')
            client = d.DNSClient(cache, args)
            client.session = Session()
            with patch.object(d.asyncio, 'sleep', AsyncMock()):
                result = await client.query('broken.example', 'A')
            self.assertEqual(result['status'], 'DoH DNS RCODE 2')
            self.assertEqual(cache.db.execute('select count(*) from cache').fetchone()[0], 0)
            await client.close()
            cache.close()

    async def test_http_429_long_retry_after_stops_and_persists_cooldown(self):
        from argparse import Namespace
        class Response:
            status = 429
            headers = {"Retry-After": "181"}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        class Session:
            calls = 0
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def get(self, url):
                Session.calls += 1
                return Response()
        with tempfile.TemporaryDirectory() as folder:
            cache = d.Cache(Path(folder) / "cache.sqlite")
            with patch.object(d.aiohttp, 'ClientSession', Session), patch.object(d.Pacer, 'wait', AsyncMock()):
                result, errors = await d.peering_metadata(set(range(1, 400)), cache, Namespace(peeringdb_key=None))
            self.assertEqual(Session.calls, 1)
            self.assertEqual(result, {})
            self.assertTrue(errors)
            self.assertGreater(cache.get('cooldown', 'peeringdb')['until'], d.time.time()+175)
            self.assertIsNone(cache.get('pdb-v1', '1'))
            cache.close()

    async def test_ipv6_reverse_nibbles(self):
        dns = AsyncMock()
        dns.query.return_value = {"values": ["15169 | 2001:4860::/32 | US | arin | date"]}
        records = await d.origin_dns("2001:4860:4860::8888", dns)
        name = dns.query.call_args.args[0]
        self.assertEqual(len(name.removesuffix(".origin6.asn.cymru.com").split(".")), 32)
        self.assertEqual(records[0]["asns"], [15169])

    async def test_nxdomain_and_cache(self):
        from argparse import Namespace
        import dns.resolver
        with tempfile.TemporaryDirectory() as folder:
            cache = d.Cache(Path(folder) / "cache.sqlite")
            args = Namespace(nameserver=["1.1.1.1"], dns_timeout=1, workers=2, dns_qps=100, ipv6=True)
            client = d.DNSClient(cache, args)
            client.resolver.resolve = AsyncMock(side_effect=dns.resolver.NXDOMAIN)
            self.assertEqual((await client.domain("absent.example"))["ips"], [])
            self.assertEqual((await client.domain("absent.example"))["ips"], [])
            self.assertEqual(client.resolver.resolve.await_count, 2)
            cache.close()


if __name__ == "__main__":
    unittest.main()
