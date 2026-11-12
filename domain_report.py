#!/usr/bin/env python3
"""Resolve domains, enrich IPs in batches, group by operator/ASN, export CSV.

Python 3.10+; pip install -r requirements.txt
python domain_report.py build/input/domains.csv
No API keys required. CSV is UTF-8 BOM / semicolon for Russian Excel.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import email.utils
import ipaddress
import json
import logging
import os
from pathlib import Path
import random
import re
import socket
import sqlite3
import time
from types import SimpleNamespace
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
import xml.etree.ElementTree as ET
import zipfile

import aiohttp
import dns.asyncresolver
import dns.exception
import dns.resolver
import dns.rdata
import dns.rdataclass
import dns.rdatatype
from subdomain_providers import ExternalAPIClient, ENDPOINTS as EXTRA_ENDPOINTS

LOG = logging.getLogger("domains")
HEADERS = ["Домен", "IP-адрес", "Диапазон (BGP-префикс)", "ASN / оператор сети", "Сайт оператора", "Резолв через"]
CDNS = {13335: "Cloudflare (CDN)", 54113: "Fastly (CDN)",
        20940: "Akamai (CDN)", 16625: "Akamai (CDN)"}
HEADER_NAMES = {"domain", "domains", "домен", "домены", "url", "hostname", "host", "сайт"}


def normalize_domain(value):
    value = str(value).strip().lstrip("\ufeff")
    if not value or value.startswith("#"):
        return None
    if value.casefold() in HEADER_NAMES:
        return None
    if value.startswith("*."):
        value = value[2:]
    try:
        host = urlsplit(value if "://" in value else "//" + value).hostname
        if not host:
            raise ValueError("нет имени хоста")
        host = host.rstrip(".").lower()
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            pass
        host = host.encode("idna").decode("ascii")
        if len(host) > 253 or "." not in host or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            raise ValueError("некорректное доменное имя")
        return host
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{value!r}: {exc}") from exc


def read_input(path: Path, column: int, sheet_index: int):
    """Read CSV/TXT, XLSX (stdlib) or XLS (optional xlrd); column is 1-based."""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(path) as z:
            shared = []
            if "xl/sharedStrings.xml" in z.namelist():
                root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                shared = ["".join(el.itertext()) for el in root.findall("s:si", ns)]
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            sheets = wb.findall("s:sheets/s:sheet", ns)
            selected = sheets[sheet_index]
            rel_id = selected.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            target = next(x.attrib["Target"] for x in rels if x.attrib["Id"] == rel_id)
            target = target.lstrip("/") if target.startswith("/") else "xl/" + target
            root = ET.fromstring(z.read(target))
            values = []
            for row in root.findall("s:sheetData/s:row", ns):
                for cell in row.findall("s:c", ns):
                    letters = re.match(r"[A-Z]+", cell.attrib["r"]).group()
                    number = 0
                    for letter in letters:
                        number = number * 26 + ord(letter) - 64
                    if number != column:
                        continue
                    typ = cell.attrib.get("t")
                    raw = cell.findtext("s:v", "", ns)
                    if typ == "s":
                        raw = shared[int(raw)]
                    elif typ == "inlineStr":
                        raw = "".join(cell.find("s:is", ns).itertext())
                    values.append(raw)
    elif suffix == ".xls":
        try:
            import xlrd
        except ImportError as exc:
            raise ValueError("Для XLS установите: python -m pip install xlrd") from exc
        book = xlrd.open_workbook(str(path))
        values = book.sheet_by_index(sheet_index).col_values(column - 1)
    else:
        data = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-16", "cp1251"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError("Не удалось определить кодировку")
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        values = [row[column - 1] for row in csv.reader(text.splitlines(), dialect)
                  if len(row) >= column]
    domains, invalid, seen = [], [], set()
    for value in values:
        try:
            host = normalize_domain(value)
        except ValueError as exc:
            invalid.append({"input": str(value), "error": str(exc)})
            continue
        if host and host not in seen:
            domains.append(host)
            seen.add(host)
    return domains, invalid


class Cache:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS cache "
                        "(kind TEXT, key TEXT, expires REAL, value TEXT, PRIMARY KEY(kind,key))")

    def get(self, kind, key):
        row = self.db.execute("SELECT expires,value FROM cache WHERE kind=? AND key=?", (kind, key)).fetchone()
        return json.loads(row[1]) if row and row[0] > time.time() else None

    def put(self, kind, key, value, ttl):
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                        (kind, key, time.time() + ttl, json.dumps(value, ensure_ascii=False)))
        self.db.commit()  # checkpoint each completed DNS request/batch, survives Ctrl+C

    def close(self):
        self.db.close()


class Pacer:
    def __init__(self, qps):
        self.interval = 1 / qps
        self.next = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            await asyncio.sleep(max(0, self.next - time.monotonic()))
            self.next = time.monotonic() + self.interval

    def postpone(self, seconds):
        self.next = max(self.next, time.monotonic() + seconds)


def ip_key(ip):
    obj = ipaddress.ip_address(ip)
    return obj.version, int(obj)


class DNSClient:
    def __init__(self, cache, args):
        self.cache = cache
        self.args = args
        self.resolver = dns.asyncresolver.Resolver(configure=not bool(args.nameserver))
        if args.nameserver:
            self.resolver.nameservers = args.nameserver
        self.resolver.timeout = min(2.0, args.dns_timeout)
        self.limit = asyncio.Semaphore(args.workers)
        self.pace = Pacer(args.dns_qps)
        self.cymru_pace = Pacer(min(10, args.dns_qps))
        self.session = None
        self.doh_disabled = False

    async def close(self):
        if self.session is not None:
            await self.session.close()

    async def query_doh(self, name, typ):
        key = name + '|' + typ + '|https://dns.google/resolve'
        cached = self.cache.get('dns-v1', key)
        if cached is not None:
            return cached
        cooldown = self.cache.get('cooldown', 'dns.google')
        if self.doh_disabled:
            return {'values': [], 'status': 'DoH Retry-After'}
        if cooldown and cooldown['until'] > time.time():
            delay = cooldown['until'] - time.time()
            if delay > 180:
                self.doh_disabled = True
                return {'values': [], 'status': 'DoH Retry-After'}
            self.pace.postpone(delay)
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=getattr(self.args, 'doh_timeout', max(12, self.args.dns_timeout))),
                headers={'User-Agent': 'DomainNetworkReport/1.0', 'Accept': 'application/dns-json'})
        code = {'A': 1, 'AAAA': 28, 'TXT': 16}[typ]
        error = 'DoH unavailable'
        attempts = getattr(self.args, 'dns_attempts', 3)
        for attempt in range(attempts):
            async with self.limit:
                await self.pace.wait()
                if self.doh_disabled:
                    return {'values': [], 'status': 'DoH Retry-After'}
                if name.lower().endswith('.asn.cymru.com'):
                    await self.cymru_pace.wait()
                try:
                    async with self.session.get('https://dns.google/resolve',
                            params={'name': name, 'type': typ, 'edns_client_subnet': '0.0.0.0/0'}) as response:
                        if response.status == 429 or response.status >= 500:
                            error = f'DoH HTTP {response.status}'
                            delay = max(retry_after(response.headers.get('Retry-After')), 2 ** attempt * 5)
                            if response.status == 429:
                                self.cache.put('cooldown', 'dns.google', {'until': time.time()+delay}, delay)
                            if delay > 180:
                                self.doh_disabled = True
                            else:
                                self.pace.postpone(delay)
                            LOG.warning('%s, пауза %.1f сек', error, delay)
                            continue
                        response.raise_for_status()
                        payload = await response.json(content_type=None)
                        status = int(payload.get('Status', -1))
                        if status not in (0, 3):
                            error = f'DoH DNS RCODE {status}'
                        else:
                            records = [r for r in payload.get('Answer', []) if r.get('type') == code]
                            values = []
                            for record in records:
                                if typ == 'TXT':
                                    txt = dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.TXT, record['data'])
                                    values.append(b''.join(txt.strings).decode('utf-8', 'replace'))
                                else:
                                    ip = ipaddress.ip_address(record['data'])
                                    if ip.version == (4 if typ == 'A' else 6):
                                        values.append(str(ip))
                            result = {'values': values, 'status': 'NXDOMAIN' if status == 3 else 'OK' if values else 'NoAnswer'}
                            ttls = [int(r.get('TTL', 0)) for r in payload.get('Answer', [])]
                            self.cache.put('dns-v1', key, result, min([3600]+ttls) if values else 300)
                            return result
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, dns.exception.DNSException) as exc:
                    error = 'DoH ' + type(exc).__name__
            if attempt + 1 < attempts:
                await asyncio.sleep(2 ** attempt + random.uniform(0, .3))
        return {'values': [], 'status': error}

    async def query(self, name, typ):
        if getattr(self.args, 'dns_transport', 'system') == 'doh':
            return await self.query_doh(name, typ)
        key = name + "|" + typ + "|" + ",".join(self.resolver.nameservers)
        cached = self.cache.get("dns-v1", key)
        if cached is not None:
            return cached
        attempts = getattr(self.args, 'dns_attempts', 3)
        for attempt in range(attempts):
            async with self.limit:
                await self.pace.wait()
                if name.lower().endswith(".asn.cymru.com"):
                    await self.cymru_pace.wait()
                try:
                    answer = await self.resolver.resolve(name, typ, lifetime=self.args.dns_timeout,
                                                         search=False)
                    if typ == "TXT":
                        values = [b"".join(item.strings).decode("utf-8", "replace") for item in answer]
                    else:
                        values = [item.to_text() for item in answer]
                    result = {"values": values, "status": "OK"}
                    self.cache.put("dns-v1", key, result, min(answer.rrset.ttl, 3600))
                    return result
                except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer) as exc:
                    result = {"values": [], "status": type(exc).__name__}
                    self.cache.put("dns-v1", key, result, 300)
                    return result
                except (dns.exception.DNSException, OSError) as exc:
                    error = type(exc).__name__
            if attempt + 1 < attempts:
                await asyncio.sleep((2 ** attempt) + random.uniform(0, .3))
        return {"values": [], "status": error}  # transient errors are NOT cached

    async def domain(self, host):
        try:
            obj = ipaddress.ip_address(host)
            if obj.version == 6 and not self.args.ipv6:
                return {"ips": [], "status": "IPv6 отключён; IPv4 не указан"}
            return {"ips": [str(obj)], "status": "IP_LITERAL"}
        except ValueError:
            pass
        types = ["A", "AAAA"] if self.args.ipv6 else ["A"]
        answers = await asyncio.gather(*(self.query(host, typ) for typ in types))
        ips = sorted({ip for ans in answers for ip in ans["values"]}, key=ip_key)
        return {"ips": ips, "status": "; ".join(f"{typ}: {ans['status']}" for typ, ans in zip(types, answers))}


def parse_bulk(text, requested):
    results = {}
    for line in text.splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 7:
            continue
        try:
            ip = str(ipaddress.ip_address(parts[1]))
            if ip not in requested:
                continue
            network = ipaddress.ip_network(parts[2], strict=False)
            if ipaddress.ip_address(ip) not in network:
                continue
            asns = sorted({int(x) for x in re.findall(r"\d+", parts[0])})
            if not asns:
                continue
        except ValueError:
            continue
        record = {"prefix": str(network), "asns": asns, "name": parts[6], "source": "Team Cymru bulk"}
        if record not in results.setdefault(ip, []):
            results[ip].append(record)
    return results


def whois_bulk(ips, timeout):
    chunks = []
    try:
        with socket.create_connection(("whois.cymru.com", 43), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(("begin\nverbose\nnotruncate\n" + "\n".join(ips) + "\nend\n").encode("ascii"))
            deadline = time.monotonic() + timeout
            size = 0
            while True:
                sock.settimeout(max(.1, deadline - time.monotonic()))
                part = sock.recv(65536)
                if not part:
                    break
                chunks.append(part)
                size += len(part)
                if size > 16_000_000 or time.monotonic() >= deadline:
                    break
    except OSError:
        if not chunks:
            raise
    return parse_bulk(b"".join(chunks).decode("utf-8", "replace"), set(ips))


async def origin_dns(ip, dns):
    obj = ipaddress.ip_address(ip)
    if obj.version == 4:
        query = ".".join(reversed(ip.split("."))) + ".origin.asn.cymru.com"
    else:
        query = ".".join(reversed(obj.exploded.replace(":", ""))) + ".origin6.asn.cymru.com"
    answer = await dns.query(query, "TXT")
    records = []
    for value in answer["values"]:
        parts = [part.strip() for part in value.split("|")]
        try:
            network = ipaddress.ip_network(parts[1], strict=False)
            asns = sorted({int(x) for x in parts[0].split()})
            if obj in network and asns:
                records.append({"prefix": str(network), "asns": asns, "name": "", "source": "Team Cymru DNS"})
        except (ValueError, IndexError):
            continue
    return records


async def enrich_ips(ips, cache, dns, args):
    result, pending = {}, []
    for ip in ips:
        if not ipaddress.ip_address(ip).is_global:
            result[ip] = []
            continue
        cached = cache.get("bgp-v1", ip)
        if cached is not None:
            result[ip] = cached
        else:
            pending.append(ip)
    LOG.info("BGP: %d IP из кеша, %d требуют запроса", len(result), len(pending))
    bulk_pace = Pacer(.5)
    if not args.dns_only:
        for offset in range(0, len(pending), args.batch_size):
            batch = pending[offset:offset + args.batch_size]
            await bulk_pace.wait()
            try:
                found = await asyncio.to_thread(whois_bulk, batch, args.whois_timeout)
            except OSError as exc:
                LOG.warning("Bulk WHOIS недоступен (%s); переключение на DNS", exc)
                break
            for ip, records in found.items():
                result[ip] = records
                cache.put("bgp-v1", ip, records, 14400)
            LOG.info("BGP: обработан пакет %d/%d, ответов %d/%d",
                     offset // args.batch_size + 1, (len(pending) + args.batch_size - 1) // args.batch_size,
                     len(found), len(batch))
    missing = [ip for ip in pending if ip not in result]
    if missing:
        LOG.info("BGP DNS: %d оставшихся IP (не более 10 запросов/сек)", len(missing))
        fallback_pace = Pacer(min(10, args.dns_qps))
        async def fallback(ip):
            await fallback_pace.wait()
            records = await origin_dns(ip, dns)
            result[ip] = records
            if records:
                cache.put("bgp-v1", ip, records, 14400)
        await asyncio.gather(*(fallback(ip) for ip in missing))
    return result


def safe_website(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in ("http", "https") and parsed.hostname and not parsed.username:
            return value
    except ValueError:
        pass
    return ""


def retry_after(value, now=None):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            return max(0, parsed.timestamp() - (time.time() if now is None else now))
        except (ValueError, TypeError, OverflowError):
            return 0


def ct_candidates(domain, certificates, limit):
    """Concrete, in-scope DNS names only: a wildcard is not an observed host."""
    names = {}
    for certificate in certificates:
        if not isinstance(certificate, dict):
            continue
        raw_names = certificate.get('dns_names', [])
        if not isinstance(raw_names, list):
            raw_names = []
        raw_names = list(raw_names) + str(certificate.get('name_value', '')).splitlines()
        date = str(certificate.get('not_before', certificate.get('entry_timestamp', '')))
        for value in raw_names:
            if not isinstance(value, str) or any(char in value for char in ('*', '/', ':', '@', '%')):
                continue
            try:
                host = normalize_domain(value)
            except ValueError:
                continue
            if not host or not host.endswith('.' + domain):
                continue
            names[host] = max(names.get(host, ''), date)
    # Prefer recently observed names, then common shallow web names.
    ordered = sorted(names, key=lambda host: (names[host], host), reverse=True)
    priority = {'www': 0, 'web': 1, 'cdn': 2, 'api': 3}
    is_aws_dns = bool(re.fullmatch(r'awsdns-\d+\.(?:co\.uk|com|net|org)', domain))
    def rank(host):
        label = host[:-len(domain)-1]
        # For Route 53 DNS zones, prefer concrete ns-NNN names actually in the
        # provider response. Do not manufacture any names from this pattern.
        service = (0 if re.fullmatch(r'ns-\d+', label) else 1 if re.fullmatch(r'(?:g|pdns)-ns-\d+', label)
                   else 10) if is_aws_dns else priority.get(label, 4)
        return service, host.count('.'), len(label)
    ordered.sort(key=rank)
    return ordered[:limit]


class CTClient:
    """Small CT samples; independent pacing/circuit breakers for each API."""
    ENDPOINTS = {'certspotter': 'https://api.certspotter.com/v1/issuances',
                 'crtsh': 'https://crt.sh/', **EXTRA_ENDPOINTS}

    def __init__(self, cache, args):
        self.cache, self.args = cache, args
        self.session = None
        self.paces = {provider: Pacer(1 / args.ct_interval) for provider in args.ct_providers}
        self.failures = {provider: 0 for provider in args.ct_providers}
        self.disabled = {}
        self.request_counts = {provider: 0 for provider in args.ct_providers}
        self.extra = ExternalAPIClient(cache, args, ct_candidates, Pacer, retry_after)

    async def close(self):
        await self.extra.close()
        if self.session is not None:
            await self.session.close()

    def stop_provider(self, provider, seconds, reason):
        self.disabled[provider] = reason
        self.cache.put('cooldown', 'ct:' + provider,
                       {'until': time.time() + seconds, 'reason': reason}, seconds)
        LOG.warning('CT %s: %s; остановлен до следующего запуска (пауза %.0f сек)', provider, reason, seconds)

    async def discover(self, domain, provider):
        if provider in EXTRA_ENDPOINTS:
            return await self.extra.discover(domain, provider)
        key = provider + '|' + domain
        cached = self.cache.get('ct-v1', key)
        if cached is not None:
            return {**cached, 'cached': True}
        cooldown = self.cache.get('cooldown', 'ct:' + provider)
        if provider in self.disabled or (cooldown and cooldown['until'] > time.time()):
            return {'names': [], 'status': 'deferred', 'provider': provider,
                    'error': self.disabled.get(provider) or cooldown.get('reason', 'Retry-After')}
        if self.session is None:
            self.session = aiohttp.ClientSession(
                headers={'User-Agent': 'DomainNetworkReport/1.2', 'Accept': 'application/json'},
                timeout=aiohttp.ClientTimeout(total=self.args.ct_timeout,
                                             connect=min(5, self.args.ct_timeout),
                                             sock_read=min(10, self.args.ct_timeout)))
        params = ({'domain': domain, 'include_subdomains': 'true', 'expand': 'dns_names'}
                  if provider == 'certspotter' else {'q': '%.' + domain, 'output': 'json'})
        headers = {}
        if provider == 'certspotter' and self.args.certspotter_key:
            headers['Authorization'] = 'Bearer ' + self.args.certspotter_key
        certificates, source, error = [], '', ''
        for page in range(self.args.ct_pages if provider == 'certspotter' else 1):
            # Check again AFTER waiting: another worker may have hit a limit.
            await self.paces[provider].wait()
            cooldown = self.cache.get('cooldown', 'ct:' + provider)
            if provider in self.disabled or (cooldown and cooldown['until'] > time.time()):
                error = 'provider cooldown'
                break
            if provider == 'certspotter' and not self.args.certspotter_key and self.request_counts[provider] >= self.args.ct_anonymous_limit:
                self.stop_provider(provider, 3600, 'достигнут локальный лимит анонимных CT-запросов')
                error = 'anonymous query budget'
                break
            try:
                # Persist a rolling one-hour request count, including unsuccessful requests.
                if provider == 'certspotter' and not self.args.certspotter_key:
                    usage = self.cache.get('ct-usage', provider) or {'count': 0, 'until': time.time()+3600}
                    if usage['count'] >= self.args.ct_anonymous_limit:
                        self.stop_provider(provider, max(1, usage['until']-time.time()), 'часовой лимит анонимных запросов')
                        error = 'anonymous query budget'
                        break
                    usage['count'] += 1
                    self.cache.put('ct-usage', provider, usage, max(1, usage['until']-time.time()))
                self.request_counts[provider] += 1
                async with self.session.get(self.ENDPOINTS[provider], params=params, headers=headers) as response:
                    source = str(response.url)
                    if response.status == 403 and provider == 'certspotter':
                        try:
                            detail = json.loads(await response.content.read(8192))
                        except (ValueError, aiohttp.ClientError):
                            detail = {}
                        if detail.get('code') in ('not_allowed_by_plan', 'timeout', 'bad_query', 'invalid_value'):
                            result = {'names': [], 'status': 'restricted', 'provider': provider,
                                      'source': source, 'error': detail.get('code'),
                                      'message': str(detail.get('message', ''))[:400]}
                            self.cache.put('ct-v1', key, result, 3600)
                            return result  # domain-specific rejection; do not disable the API
                    if response.status in (401, 403, 429):
                        delay = max(retry_after(response.headers.get('Retry-After')), 3600 if response.status == 429 else 300)
                        self.stop_provider(provider, delay, f'HTTP {response.status}')
                        error = f'HTTP {response.status}'
                        break
                    if response.status >= 500:
                        delay = max(retry_after(response.headers.get('Retry-After')), 15)
                        self.paces[provider].postpone(delay)
                        self.cache.put('cooldown', 'ct:' + provider,
                                       {'until': time.time()+delay, 'reason': f'HTTP {response.status}'}, delay)
                        raise ValueError(f'HTTP {response.status}')
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > self.args.ct_max_bytes:
                            raise ValueError('CT response exceeds size limit')
                        chunks.append(chunk)
                    payload = json.loads(b''.join(chunks))
                    if not isinstance(payload, list):
                        raise ValueError('CT response is not a JSON array')
                    self.failures[provider] = 0
                    certificates.extend(payload)
                    names = ct_candidates(domain, certificates, self.args.ct_candidates)
                    if names or not payload or provider == 'crtsh':
                        result = {'names': names, 'status': 'OK' if names else 'empty',
                                  'provider': provider, 'source': source, 'pages': page+1}
                        self.cache.put('ct-v1', key, result, 86400 if names else 3600)
                        return result
                    params['after'] = str(payload[-1]['id'])
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
                error = type(exc).__name__ + ': ' + str(exc)
                self.failures[provider] += 1
                if self.failures[provider] >= 3:
                    self.stop_provider(provider, 60, 'три последовательных сетевых ошибки')
                LOG.warning('CT %s %s: %s', provider, domain, error[:160])
                break  # one bounded attempt; next provider instead of repeated long timeouts
        names = ct_candidates(domain, certificates, self.args.ct_candidates)
        # A page/size limit or failure is never cached as a confirmed empty result.
        return {'names': names, 'status': 'partial' if names else 'unavailable' if error else 'page_limit',
                'provider': provider, 'source': source, 'error': error}


async def ct_fallback(dns_results, cache, args):
    pending = []
    for host, info in dns_results.items():
        if info['ips']:
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pending.append(host)
    if not pending or args.no_ct:
        return []
    LOG.info('CT: только %d доменов без IPv4; остальные %d пропущены',
             len(pending), len(dns_results)-len(pending))
    ct = CTClient(cache, args)
    ct_dns = DNSClient(cache, SimpleNamespace(**{**vars(args), 'workers': 8, 'dns_qps': min(10, args.dns_qps),
                                               'dns_timeout': args.ct_dns_timeout, 'doh_timeout': args.ct_dns_timeout,
                                               'dns_attempts': 2, 'ipv6': False}))
    semaphore = asyncio.Semaphore(args.ct_workers)
    done = 0

    async def process(host):
        info = dns_results[host]
        info['apex_status'] = info['status']
        info['ct'] = {'status': 'pending', 'providers': [], 'tested': [], 'resolved': {}}
        async def bounded():
            tested, resolved = set(), {}
            for provider in args.ct_providers:
                discovered = await ct.discover(host, provider)
                info['ct']['providers'].append(discovered)
                names = [name for name in discovered['names'] if name not in tested]
                names = names[:max(0, args.ct_candidates-len(tested))]
                for index in range(0, len(names), 2):
                    batch = names[index:index+2]
                    answers = await asyncio.gather(*(ct_dns.domain(name) for name in batch))
                    for name, answer in zip(batch, answers):
                        tested.add(name)
                        info['ct']['tested'].append({'host': name, 'status': answer['status']})
                        if answer['ips'] and len(resolved) < args.ct_sample:
                            resolved[name] = answer['ips']
                    # Save usable discoveries immediately, even if a later DNS call times out.
                    if resolved:
                        info['ct']['resolved'] = resolved
                        info['resolved_hosts'] = resolved
                        info['ips'] = sorted({ip for ips in resolved.values() for ip in ips}, key=ip_key)
                        info['status'] = 'CT fallback (IPv4)'
                    if len(resolved) >= args.ct_sample:
                        break
                if len(resolved) >= args.ct_sample or len(tested) >= args.ct_candidates:
                    break
            statuses = [r['status'] for r in info['ct']['providers']]
            dns_incomplete = any(any(token in result['status'] for token in
                ('Timeout', 'RCODE', 'NoNameservers', 'DoH HTTP', 'Retry-After')) for result in info['ct']['tested'])
            info['ct']['status'] = ('resolved' if resolved else 'dns_unavailable' if dns_incomplete else 'unavailable' if any(
                s in ('unavailable', 'deferred', 'page_limit', 'partial', 'restricted') for s in statuses)
                or not any(s in ('OK', 'empty') for s in statuses) else 'no_live_names')
        async with semaphore:
            try:
                await asyncio.wait_for(bounded(), timeout=args.ct_domain_timeout)
            except asyncio.TimeoutError:
                info['ct']['status'] = 'resolved_partial' if info['ips'] else 'domain_timeout'
            nonlocal done
            done += 1
            if done % 10 == 0 or done == len(pending):
                LOG.info('CT: %d/%d, восстановлено %d', done, len(pending),
                         sum(bool(dns_results[h]['ips']) for h in pending))
    tasks = [asyncio.create_task(process(host)) for host in pending]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=args.ct_budget)
    except asyncio.TimeoutError:
        LOG.warning('CT: исчерпан общий лимит времени %.0f сек; прогресс будет сохранён', args.ct_budget)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ct.close()
        await ct_dns.close()
    unfinished = 0
    for host in pending:
        info = dns_results[host]
        state = info.get('ct', {}).get('status', 'pending')
        if state == 'pending':
            info.setdefault('ct', {})['status'] = 'resolved_partial' if info['ips'] else 'budget_exceeded'
        if not info['ips'] and info['ct']['status'] != 'no_live_names':
            unfinished += 1
    return ([f'CT: для {unfinished} доменов проверка неполная (API/таймаут/лимит), подробности в details.json']
            if unfinished else [])


async def peering_metadata(asns, cache, args):
    result, pending, errors = {}, [], []
    for asn in sorted(asns):
        cached = cache.get("pdb-v1", str(asn))
        if cached is not None:
            result[asn] = cached
        else:
            pending.append(asn)
    pace = Pacer(1 / 4)  # 15/min, below the documented anonymous 20/min
    headers = {"User-Agent": "DomainNetworkReport/1.0", "Accept": "application/json"}
    if args.peeringdb_key:
        headers["Authorization"] = "Api-Key " + args.peeringdb_key
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        for offset in range(0, len(pending), 150):
            batch = pending[offset:offset + 150]
            url = "https://www.peeringdb.com/api/net?" + urlencode({
                "asn__in": ",".join(map(str, batch)), "depth": 0, "limit": 250})
            data = None
            for attempt in range(4):
                await pace.wait()
                try:
                    async with session.get(url) as response:
                        if response.status == 429 or response.status >= 500:
                            delay = max(retry_after(response.headers.get("Retry-After")),
                                        (2 ** attempt) * 5 + random.uniform(0, 1))
                            # Persist cooldown across runs, never shorten server Retry-After.
                            cache.put("cooldown", "peeringdb", {"until": time.time() + delay}, delay)
                            pace.postpone(delay)
                            if delay > 180:
                                errors.append("PeeringDB: длительная пауза Retry-After, запросы прекращены")
                                return result, errors
                            LOG.warning("PeeringDB HTTP %s, пауза %.1f сек", response.status, delay)
                            continue
                        response.raise_for_status()
                        payload = await response.json()
                        if not isinstance(payload.get("data"), list):
                            raise ValueError("Некорректный ответ PeeringDB")
                        data = payload["data"]
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    LOG.warning("PeeringDB: %s", type(exc).__name__)
                    pace.postpone((2 ** attempt) * 5)
            if data is None:
                errors.append("PeeringDB недоступен после повторных попыток; сайты не определены")
                break  # do not continue hammering subsequent batches
            mapped = {int(row["asn"]): {"name": row.get("name", ""),
                       "website": safe_website(row.get("website")),
                       "org_id": row.get("org_id"),
                       "source": f"https://www.peeringdb.com/net/{row['id']}"}
                      for row in data if int(row.get("asn", 0)) in batch}
            for asn in batch:
                meta = mapped.get(asn, {"name": "", "website": "", "org_id": None, "source": ""})
                result[asn] = meta
                cache.put("pdb-v1", str(asn), meta, 604800 if meta["website"] else 86400)
            LOG.info("PeeringDB: %d/%d ASN", min(offset + 150, len(pending)), len(pending))
    return result, errors


def make_rows(domains, dns_results, ip_results, metadata):
    rows = []
    for domain in domains:
        ips = dns_results[domain]["ips"]
        if not ips:
            rows.append({"domain": domain, "ip": "", "prefix": "", "asn": None,
                         "operator": "", "website": "", "org_id": None,
                         "status": "DNS: " + dns_results[domain]["status"], "source": ""})
        for ip in ips:
            records = ip_results.get(ip, [])
            if not records:
                status = "Непубличный IP" if not ipaddress.ip_address(ip).is_global else "BGP/ASN не определён"
                rows.append({"domain": domain, "ip": ip, "prefix": "", "asn": None,
                             "operator": "", "website": "", "org_id": None, "status": status, "source": ""})
            for record in records:
                for asn in record["asns"]:
                    meta = metadata.get(asn, {})
                    # In multi-origin records, a combined WHOIS description is not attributed to one ASN.
                    name = meta.get("name") or (record["name"] if len(record["asns"]) == 1 else "")
                    name = CDNS.get(asn, name or "Название не найдено")
                    rows.append({"domain": domain, "ip": ip, "prefix": record["prefix"], "asn": asn,
                                 "operator": name, "website": meta.get("website", ""),
                                 "org_id": meta.get("org_id"),
                                 "status": "OK" if meta.get("website") else "Сайт оператора не найден в PeeringDB",
                                 "source": meta.get("source", ""), "bgp_source": record["source"]})
    for row in rows:
        info = dns_results[row['domain']]
        via = [host for host, addresses in info.get('resolved_hosts', {}).items() if row['ip'] in addresses]
        row['resolved_via'] = '; '.join(sorted(via)) if via else row['domain'] if row['ip'] else ''
        row['resolution_method'] = 'ct' if via else 'direct' if row['ip'] else 'unresolved'
        if not row['ip'] and info.get('ct'):
            row['status'] += '; CT: ' + info['ct']['status']
    # Group known common PeeringDB organizations even when they operate several ASNs.
    group_names = {}
    for row in rows:
        if row["org_id"]:
            key = row["org_id"]
            group_names[key] = min(group_names.get(key, row["operator"].casefold()), row["operator"].casefold())
    def sort_key(row):
        operator = group_names.get(row["org_id"], row["operator"].casefold())
        return (row["asn"] is None, operator, row["org_id"] or 0,
                row["asn"] or 0, row["domain"], ip_key(row["ip"]) if row["ip"] else (0, 0), row["prefix"])
    rows.sort(key=sort_key)
    return rows


def csv_safe(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def write_outputs(output, rows, invalid, dns_results, service_errors, elapsed):
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(HEADERS)
        for row in rows:
            writer.writerow([csv_safe(x) for x in [row["domain"], row["ip"] or "Не найден",
                row["prefix"] or "Не определён",
                f"AS{row['asn']} — {row['operator']}" if row["asn"] else row["status"],
                row["website"] or "Не найден",
                row.get('resolved_via') or "Не найден"]])
    os.replace(temp, output)
    details = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "elapsed_seconds": round(elapsed, 2), "domains": len(dns_results),
               "domains_without_ip": sum(not d["ips"] for d in dns_results.values()),
               "domains_via_ct": sum(bool(d.get('resolved_hosts')) for d in dns_results.values()),
               "rows": len(rows), "rows_with_website": sum(bool(r["website"]) for r in rows),
               "distinct_asns": len({r["asn"] for r in rows if r["asn"]}),
               "invalid_input": invalid, "service_errors": service_errors,
               "dns": dns_results, "records": rows,
               "method": "DNS A (IPv4 only); CT fallback for unresolved domains; Team Cymru BGP origin (not legal ownership); PeeringDB operator website",
               "sources": ["https://www.team-cymru.com/ip-asn-mapping",
                           "https://docs.peeringdb.com/howto/work_within_peeringdbs_query_limits/",
                           "https://developers.google.com/speed/public-dns/docs/doh/json",
                           "https://sslmate.com/help/reference/ct_search_api_v1", "https://crt.sh/"]}
    detail_path = output.with_suffix(".details.json")
    temp = detail_path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, detail_path)
    return details


async def run(args):
    start = time.monotonic()
    domains, invalid = read_input(args.input, args.column, args.sheet - 1)
    if args.limit:
        domains = domains[:args.limit]
    if not domains:
        raise ValueError("В выбранном столбце не найдено доменов")
    LOG.info("Доменов: %d; некорректных входных значений: %d", len(domains), len(invalid))
    cache = Cache(args.cache)
    try:
        dns = DNSClient(cache, args)
        dns_results = {}
        if args.retry_unresolved:
            previous = json.loads(args.output.with_suffix('.details.json').read_text(encoding='utf-8'))
            for host in domains:
                info = previous.get('dns', {}).get(host, {})
                addresses = [ip for ip in info.get('ips', []) if ipaddress.ip_address(ip).version == 4]
                if addresses:
                    dns_results[host] = {**info, 'ips': addresses}
            LOG.info('Из отчёта сохранено %d доменов с IPv4; перепроверяются только оставшиеся', len(dns_results))
        async def resolve(host):
            if host in dns_results:
                return
            dns_results[host] = await dns.domain(host)
            if len(dns_results) % 100 == 0 or len(dns_results) == len(domains):
                LOG.info("DNS: %d/%d", len(dns_results), len(domains))
        await asyncio.gather(*(resolve(host) for host in domains))
        ct_errors = await ct_fallback(dns_results, cache, args)
        ips = sorted({ip for res in dns_results.values() for ip in res["ips"]}, key=ip_key)
        LOG.info("Уникальных IP: %d", len(ips))
        ip_results = await enrich_ips(ips, cache, dns, args)
        asns = {asn for records in ip_results.values() for r in records for asn in r["asns"]}
        cooldown = cache.get("cooldown", "peeringdb")
        if cooldown and cooldown["until"] > time.time():
            LOG.warning("PeeringDB: действует сохранённый Retry-After")
            metadata = {asn: (cache.get("pdb-v1", str(asn)) or {}) for asn in asns}
            errors = ["PeeringDB: сохранённая пауза Retry-After, использован только кеш"]
        else:
            metadata, errors = await peering_metadata(asns, cache, args)
        errors.extend(ct_errors)
        # DNS fallback lacks operator names; one TXT query per otherwise unknown ASN.
        unknown = {asn for records in ip_results.values() for rec in records for asn in rec["asns"]
                   if not metadata.get(asn, {}).get("name") and (not rec["name"] or len(rec["asns"]) > 1)}
        async def as_name(asn):
            answer = await dns.query(f"AS{asn}.asn.cymru.com", "TXT")
            for text in answer["values"]:
                fields = text.split("|", 4)
                if len(fields) == 5:
                    metadata.setdefault(asn, {})["name"] = fields[4].strip()
                    break
        await asyncio.gather(*(as_name(asn) for asn in unknown))
        rows = make_rows(domains, dns_results, ip_results, metadata)
        summary = write_outputs(args.output, rows, invalid, dns_results, errors, time.monotonic() - start)
        LOG.info("Готово: %s; %d строк, %d ASN, %d доменов без IP, %.1f сек",
                 args.output, len(rows), summary["distinct_asns"], summary["domains_without_ip"], summary["elapsed_seconds"])
        for error in errors:
            LOG.warning(error)
    finally:
        if 'dns' in locals():
            await dns.close()
        cache.close()


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="CSV/TXT/XLS/XLSX со списком доменов")
    parser.add_argument("-o", "--output", type=Path, default=Path("domains_report.csv"))
    parser.add_argument("--cache", type=Path, default=Path(".domain_cache.sqlite3"))
    parser.add_argument("--column", type=int, default=1, help="Номер столбца с доменами, начиная с 1")
    parser.add_argument("--sheet", type=int, default=1, help="Номер листа, начиная с 1")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--dns-qps", type=float, default=40, help="Максимум DNS-запросов/сек, включая повторы")
    parser.add_argument("--dns-timeout", type=float, default=5)
    parser.add_argument("--whois-timeout", type=float, default=30)
    parser.add_argument("--batch-size", type=int, default=1000, help="IP в одном WHOIS-запросе, максимум 2000")
    parser.add_argument("--nameserver", action="append", help="IP DNS-сервера; можно повторить. Включает системный DNS-транспорт")
    parser.add_argument("--dns-transport", choices=['doh', 'system'], help="По умолчанию Google DNS-over-HTTPS; с --nameserver используется system")
    parser.add_argument("--ipv4-only", dest="ipv6", action="store_false", default=False,
                        help="Только IPv4 (уже включено по умолчанию)")
    parser.add_argument("--dns-only", action="store_true", help="Использовать DNS вместо TCP/43 для ASN")
    parser.add_argument("--limit", type=int, help="Обработать первые N доменов для проверки")
    parser.add_argument('--retry-unresolved', action='store_true', help='Взять успешные результаты из текущего details.json и доработать только домены без IPv4')
    parser.add_argument('--no-ct', action='store_true', help='Отключить резервную проверку Certificate Transparency')
    parser.add_argument('--ct-providers', default='chaos,certspotter,subdomaincenter,bufferover,netlas,censys,crtsh',
                        help='Источники поддоменов, через запятую; API с ключами пропускаются, если ключ не настроен')
    parser.add_argument('--ct-workers', type=int, default=2, help='Параллельно обрабатываемые домены в CT')
    parser.add_argument('--ct-interval', type=float, default=2, help='Минимальная пауза между запросами к одному CT API')
    parser.add_argument('--ct-timeout', type=float, default=12, help='Полный таймаут одного HTTP-запроса CT, сек')
    parser.add_argument('--ct-dns-timeout', type=float, default=4, help='Таймаут DNS-запроса найденного поддомена, сек; не более двух попыток')
    parser.add_argument('--ct-domain-timeout', type=float, default=120, help='Общий лимит API + DNS на один домен, включая очередь API, сек')
    parser.add_argument('--ct-budget', type=float, default=3600, help='Общий лимит времени резервного этапа, сек; бесплатный Subdomain Center: 5 запросов/мин')
    parser.add_argument('--ct-candidates', type=int, default=20, help='Проверить не более N имён из баз поддоменов на один домен')
    parser.add_argument('--ct-sample', type=int, default=2, help='Остановиться после N поддоменов с IPv4')
    parser.add_argument('--ct-pages', type=int, default=2, help='Максимум страниц Cert Spotter на домен')
    parser.add_argument('--ct-max-bytes', type=int, default=4_000_000, help='Максимум байт одного CT-ответа')
    parser.add_argument('--ct-anonymous-limit', type=int, default=50, help='Локальный предел запросов Cert Spotter без ключа за час (не гарантия квоты API)')
    parser.add_argument('--certspotter-key', default=os.environ.get('CERTSPOTTER_API_KEY'), help='Необязательный ключ CT Search API, лучше переменная CERTSPOTTER_API_KEY')
    parser.add_argument("--peeringdb-key", default=os.environ.get("PEERINGDB_API_KEY"), help="Необязательный API key (лучше переменная PEERINGDB_API_KEY)")
    args = parser.parse_args()
    args.ct_providers = list(dict.fromkeys(part.strip() for part in args.ct_providers.split(',') if part.strip()))
    if not args.ct_providers or any(p not in CTClient.ENDPOINTS for p in args.ct_providers):
        parser.error('--ct-providers: допустимы ' + ','.join(CTClient.ENDPOINTS))
    if min(args.ct_workers, args.ct_interval, args.ct_timeout, args.ct_dns_timeout, args.ct_domain_timeout,
           args.ct_budget, args.ct_candidates, args.ct_sample, args.ct_pages, args.ct_max_bytes, args.ct_anonymous_limit) <= 0:
        parser.error('Параметры CT должны быть положительными')
    if args.ct_sample > args.ct_candidates:
        parser.error('--ct-sample не может превышать --ct-candidates')
    if args.retry_unresolved and not args.output.with_suffix('.details.json').is_file():
        parser.error('--retry-unresolved требует существующий details.json рядом с выходным CSV')
    if args.dns_transport is None:
        args.dns_transport = 'system' if args.nameserver else 'doh'
    elif args.nameserver and args.dns_transport == 'doh':
        parser.error('--nameserver применяется только с --dns-transport system')
    if (min(args.workers, args.dns_qps, args.dns_timeout, args.whois_timeout, args.column, args.sheet) <= 0
            or not 1 <= args.batch_size <= 2000 or (args.limit is not None and args.limit <= 0)):
        parser.error("Параметры должны быть положительными; batch-size от 1 до 2000")
    if args.output.suffix.lower() != ".csv":
        parser.error("Основной результат — CSV. Для XLSX используйте export_xlsx.mjs (см. README.md)")
    if args.input.resolve() in {args.output.resolve(), args.cache.resolve(), args.output.with_suffix('.details.json').resolve()}:
        parser.error("Входной файл нельзя перезаписывать результатом или кешем")
    return args


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(run(arguments()))
    except KeyboardInterrupt:
        LOG.warning("Остановлено. Прогресс сохранён в кеше; повторите ту же команду для продолжения.")
        raise SystemExit(130)
    except (ValueError, OSError, IndexError, zipfile.BadZipFile) as exc:
        LOG.error("%s", exc)
        raise SystemExit(1)
