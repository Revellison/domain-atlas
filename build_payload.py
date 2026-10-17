"""Convert the report CSV to the public, grouped IPv4 JSON consumed by site/."""
from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

HEADERS = ['Домен', 'IP-адрес', 'Диапазон (BGP-префикс)', 'ASN / оператор сети',
           'Сайт оператора', 'Резолв через']


def website(value):
    value = value.strip()
    parsed = urlsplit(value)
    return value if parsed.scheme in ('https', 'http') and parsed.hostname and not parsed.username else ''


def make_payload(csv_path, details_path=None):
    csv_path = Path(csv_path)
    details = json.loads(Path(details_path).read_text(encoding='utf-8')) if details_path else {}
    extra = {(r['domain'], r.get('ip', ''), r.get('prefix', ''), r.get('asn')): r
             for r in details.get('records', [])}
    domains, row_count = {}, 0
    with csv_path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle, delimiter=';')
        if reader.fieldnames != HEADERS:
            raise ValueError('Ожидались шесть столбцов CSV, созданного domain_report.py')
        for row in reader:
            row_count += 1
            name = row['Домен'].strip()
            if not name:
                raise ValueError(f'Строка {row_count + 1}: пустой домен')
            item = domains.setdefault(name, {'domain': name, 'addresses': {}, 'status': '', 'resolution': 'unresolved'})
            ip_text = row['IP-адрес'].strip()
            if ip_text in ('', 'Не найден'):
                item['status'] = row['ASN / оператор сети'].strip()
                continue
            address = ipaddress.ip_address(ip_text)
            if address.version != 4:
                raise ValueError(f'{name}: в отчёте встретился IPv6')
            ip = str(address)
            match = re.fullmatch(r'AS(\d+)\s*[—–-]\s*(.+)', row['ASN / оператор сети'].strip())
            asn, operator = (int(match[1]), match[2].strip()) if match else (None, 'Не определён')
            prefix = row['Диапазон (BGP-префикс)'].strip()
            if prefix in ('', 'Не определён', 'Не найден'):
                prefix = ''
            elif address not in ipaddress.IPv4Network(prefix):
                raise ValueError(f'{name}: {ip} не входит в {prefix}')
            metadata = extra.get((name, ip, prefix, asn), {})
            net = {'prefix': prefix, 'asn': asn, 'operator': operator,
                   'website': website(row['Сайт оператора']), 'org_id': metadata.get('org_id'),
                   'source': website(metadata.get('source', ''))}
            entry = item['addresses'].setdefault(ip, {'ip': ip, 'networks': [], 'resolved_via': []})
            if net not in entry['networks']:
                entry['networks'].append(net)
            hosts = [h.strip() for h in row['Резолв через'].split(';') if h.strip() not in ('', 'Не найден')]
            entry['resolved_via'] = sorted(set(entry['resolved_via'] + hosts))
            subdomain = any(h != name for h in hosts)
            if item['resolution'] != 'subdomain':
                item['resolution'] = 'subdomain' if subdomain else 'direct'

    canonical = {}
    for item in domains.values():
        for a in item['addresses'].values():
            for n in a['networks']:
                key = f"org:{n['org_id']}" if n['org_id'] else f"operator:{n['operator'].casefold()}"
                n['provider_id'] = key
                canonical[key] = min(canonical.get(key, n['operator']), n['operator'], key=str.casefold)
    for item in domains.values():
        item['addresses'] = sorted(item['addresses'].values(), key=lambda a: int(ipaddress.IPv4Address(a['ip'])))
        providers = {}
        for a in item['addresses']:
            for n in a['networks']:
                key = n['provider_id']
                p = providers.setdefault(key, {'id': key, 'name': canonical[key], 'asns': [], 'website': n['website']})
                if n['asn'] and n['asn'] not in p['asns']:
                    p['asns'].append(n['asn'])
                if not p['website']:
                    p['website'] = n['website']
        item['providers'] = sorted(providers.values(), key=lambda p: (not bool(p['asns']), p['name'].casefold()))
        for p in item['providers']:
            p['asns'].sort()
        item['group'] = item['providers'][0] if item['providers'] else {
            'id': 'unresolved', 'name': 'Без IPv4', 'asns': [], 'website': ''}
        if item['addresses']:
            item['status'] = 'IPv4 найден через поддомены' if item['resolution'] == 'subdomain' else 'IPv4 найден'
        item['cdn'] = any('(CDN)' in n['operator'] for a in item['addresses'] for n in a['networks'])
    result = sorted(domains.values(), key=lambda d: (
        not bool(d['addresses']), not bool(d['group']['asns']), d['group']['name'].casefold(),
        min(d['group']['asns'], default=2**32), d['domain']))
    if details and (details.get('domains') != len(result) or details.get('rows') != row_count):
        raise ValueError('CSV и details.json относятся к разным отчётам')
    resolved = sum(bool(d['addresses']) for d in result)
    return {
        'schema_version': 1,
        'generated_at': details.get('generated_at_utc') or datetime.fromtimestamp(csv_path.stat().st_mtime, timezone.utc).isoformat(),
        'summary': {
            'domains': len(result), 'resolved': resolved, 'unresolved': len(result) - resolved,
            'ipv4': len({a['ip'] for d in result for a in d['addresses']}),
            'asns': len({n['asn'] for d in result for a in d['addresses'] for n in a['networks'] if n['asn']}),
            'via_subdomains': sum(d['resolution'] == 'subdomain' for d in result),
            'multi_ip': sum(len(d['addresses']) > 1 for d in result), 'rows': row_count,
        },
        'warnings': details.get('service_errors', []), 'domains': result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--details', type=Path)
    parser.add_argument('-o', '--output', type=Path, default=Path('site/data/domains.json'))
    parser.add_argument('--min-resolved-ratio', type=float, default=0)
    args = parser.parse_args()
    if not 0 <= args.min_resolved_ratio <= 1:
        parser.error('--min-resolved-ratio должен быть между 0 и 1')
    try:
        payload = make_payload(args.csv, args.details)
        s = payload['summary']
        if not s['domains'] or s['resolved'] / s['domains'] < args.min_resolved_ratio:
            raise ValueError('Слишком мало доменов с IPv4; предыдущий релиз останется актуальным')
        if args.min_resolved_ratio > 0 and not s['asns']:
            raise ValueError('В отчёте нет ни одного ASN; публикация остановлена')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        temporary.replace(args.output)
        print(json.dumps(s, ensure_ascii=False))
        print(f'JSON: {args.output} ({args.output.stat().st_size:,} bytes)')
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f'Ошибка: {exc}\n')


if __name__ == '__main__':
    main()
