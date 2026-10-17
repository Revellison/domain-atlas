import csv
import ipaddress
import tempfile
import unittest
from pathlib import Path

from build_payload import HEADERS, make_payload


class PayloadTests(unittest.TestCase):
    def convert(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.csv'
            with path.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(HEADERS)
                writer.writerows(rows)
            return make_payload(path)

    def test_multiple_origins_and_hosts_are_not_lost(self):
        p = self.convert([
            ['example.com', '1.1.1.1', '1.1.1.0/24', 'AS13335 — Cloudflare (CDN)', 'https://cloudflare.com', 'www.example.com'],
            ['example.com', '1.1.1.1', '1.1.1.0/24', 'AS42 — Another network', '', 'api.example.com'],
            ['example.com', '1.0.0.1', '1.0.0.0/24', 'AS13335 — Cloudflare (CDN)', 'https://cloudflare.com', 'www.example.com'],
        ])
        d = p['domains'][0]
        self.assertEqual(p['summary']['domains'], 1)
        self.assertEqual(p['summary']['asns'], 2)
        self.assertEqual(p['summary']['ipv4'], 2)
        self.assertEqual(d['resolution'], 'subdomain')
        self.assertTrue(d['cdn'])
        self.assertEqual(len(d['addresses'][1]['networks']), 2)
        self.assertEqual(d['addresses'][1]['resolved_via'], ['api.example.com', 'www.example.com'])

    def test_missing_ip_and_nonpublic_ip_remain_distinct(self):
        p = self.convert([
            ['missing.example', 'Не найден', 'Не определён', 'DNS: timeout', 'Не найден', 'Не найден'],
            ['local.example', '127.0.0.1', 'Не определён', 'Непубличный IPv4', 'javascript:alert(1)', 'local.example'],
        ])
        self.assertEqual(p['summary']['resolved'], 1)
        self.assertEqual(p['summary']['unresolved'], 1)
        self.assertEqual(p['summary']['asns'], 0)
        self.assertEqual(p['domains'][0]['addresses'][0]['networks'][0]['website'], '')
        self.assertEqual(p['domains'][1]['status'], 'DNS: timeout')

    def test_ipv6_rejected(self):
        with self.assertRaisesRegex(ValueError, 'IPv6'):
            self.convert([['example.com', '::1', '', '', '', 'example.com']])

    def test_prefix_must_contain_address(self):
        with self.assertRaises(ValueError):
            self.convert([['example.com', '1.1.1.1', '2.2.2.0/24', 'AS1 — Test', '', 'example.com']])

    def test_provider_grouping_and_numeric_ip_sort(self):
        p = self.convert([
            ['z.example', '1.1.1.20', '1.1.1.0/24', 'AS1 — A network', '', 'z.example'],
            ['b.example', '2.2.2.2', '2.2.2.0/24', 'AS2 — Z network', '', 'b.example'],
            ['z.example', '1.1.1.2', '1.1.1.0/24', 'AS1 — A network', '', 'z.example'],
            ['a.example', '1.1.1.3', '1.1.1.0/24', 'AS1 — A network', '', 'a.example'],
        ])
        self.assertEqual([d['domain'] for d in p['domains']], ['a.example', 'z.example', 'b.example'])
        self.assertEqual([a['ip'] for a in p['domains'][1]['addresses']], ['1.1.1.2', '1.1.1.20'])


if __name__ == '__main__':
    unittest.main()
