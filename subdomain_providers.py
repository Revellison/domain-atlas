"""Independent subdomain API adapters. Keys are read from the environment only.

Anonymous Subdomain Center: at most 5 requests/minute; headers and retries
are handled per provider. API records are only hostname hints: DNS is checked
by domain_report.py before any IP is used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import aiohttp

LOG = logging.getLogger('domains')
ENDPOINTS = {
    'subdomaincenter': 'https://api.subdomain.center/',
    'chaos': 'https://dns.projectdiscovery.io/dns/{domain}/subdomains',
    'bufferover': 'https://tls.bufferover.run/dns',
    'netlas': 'https://app.netlas.io/api/certs/download/',
    'censys': 'https://api.platform.censys.io/v3/global/search/query',
}
KEY_NAMES = {'subdomaincenter': ('SUBDOMAIN_CENTER_API_KEY',),
             'chaos': ('PDCP_API_KEY', 'CHAOS_API_KEY'),
             'bufferover': ('BUFFEROVER_API_KEY',), 'netlas': ('NETLAS_API_KEY',),
             'censys': ('CENSYS_PAT',)}


def get_key(provider):
    return next((os.environ[name] for name in KEY_NAMES[provider] if os.environ.get(name)), '')


def request_spec(provider, domain, key):
    url, method, options = ENDPOINTS[provider].format(domain=domain), 'GET', {}
    if provider == 'subdomaincenter':
        options['params'] = {'domain': domain}
        if key:
            options['headers'] = {'X-API-Key': key}
    elif provider == 'chaos':
        options['headers'] = {'Authorization': key}
    elif provider == 'bufferover':
        options['params'] = {'q': '.' + domain}
        options['headers'] = {'x-api-key': key}
    elif provider == 'netlas':
        method = 'POST'
        options['headers'] = {'Authorization': 'Bearer ' + key}
        options['json'] = {'q': 'certificate.names:*.' + domain, 'size': 10,
                           'fields': ['names', 'extensions.subject_alt_name.dns_names'],
                           'source_type': 'include'}
    elif provider == 'censys':
        method = 'POST'
        options['headers'] = {'Authorization': 'Bearer ' + key}
        options['json'] = {'query': 'cert.names: "*.' + domain + '"', 'page_size': 10,
                           'fields': ['cert.names']}
    return method, url, options


def extract_names(provider, payload, domain):
    """Return only documented hostname fields, never arbitrary response strings."""
    if provider == 'subdomaincenter':
        if not isinstance(payload, list):
            raise ValueError('Subdomain Center must return an array')
        return payload
    if provider == 'chaos':
        if not isinstance(payload, dict) or not isinstance(payload.get('subdomains'), list):
            raise ValueError('Chaos response has no subdomains array')
        return [name if name.rstrip('.').endswith('.'+domain) else name+'.'+domain
                for name in payload['subdomains'] if isinstance(name, str) and name]
    if provider == 'bufferover':
        if not isinstance(payload, dict) or not isinstance(payload.get('Results'), list):
            raise ValueError('BufferOver response has no Results array')
        return [line.rsplit(',', 1)[-1] for line in payload['Results'] if isinstance(line, str)]
    if provider == 'netlas':
        if not isinstance(payload, list):
            raise ValueError('Netlas download must return an array')
        result = []
        for item in payload:
            data = item.get('data', {}) if isinstance(item, dict) else {}
            certificate = data.get('certificate', data)
            result.extend(certificate.get('names', []))
            result.extend(certificate.get('extensions', {}).get('subject_alt_name', {}).get('dns_names', []))
        return result
    if provider == 'censys':
        hits = payload.get('result', {}).get('hits') if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            raise ValueError('Censys response has no result.hits array')
        result = []
        for hit in hits:
            if isinstance(hit, dict):
                result.extend(hit.get('cert', {}).get('names', []))
                result.extend(hit.get('fields', {}).get('cert.names', []))
        return result
    raise ValueError('Unknown provider: ' + provider)


class ExternalAPIClient:
    def __init__(self, cache, args, candidate_filter, pacer_factory, retry_after):
        self.cache, self.args = cache, args
        self.filter, self.retry_after = candidate_filter, retry_after
        self.keys = {provider: get_key(provider) for provider in ENDPOINTS}
        self.paces = {provider: pacer_factory(1 / max(args.ct_interval, 12.5 if provider == 'subdomaincenter'
                        and not self.keys[provider] else 20.5 if provider == 'netlas' else 1.1))
                      for provider in ENDPOINTS}
        self.locks = {provider: asyncio.Lock() for provider in ENDPOINTS}
        self.failures = {provider: 0 for provider in ENDPOINTS}
        self.disabled = {}
        self.session = None

    async def close(self):
        if self.session:
            await self.session.close()

    async def discover(self, domain, provider):
        key = provider + '|' + domain
        cached = self.cache.get('subdomain-v2', key)
        if cached is not None:
            return {**cached, 'names': cached['names'][:self.args.ct_candidates], 'cached': True}
        if provider != 'subdomaincenter' and not self.keys[provider]:
            return {'names': [], 'status': 'not_configured', 'provider': provider,
                    'error': 'API key not configured: ' + '/'.join(KEY_NAMES[provider])}
        async with self.locks[provider]:
            return await self._discover_locked(domain, provider, key)

    async def _discover_locked(self, domain, provider, cache_key):
        result = {'names': [], 'status': 'unavailable', 'provider': provider}
        if provider in self.disabled:
            return {**result, 'status': 'deferred', 'error': self.disabled[provider]}
        if self.session is None:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.args.ct_timeout,
                                             connect=min(5, self.args.ct_timeout),
                                             sock_read=min(10, self.args.ct_timeout)),
                headers={'User-Agent': 'DomainNetworkReport/1.3', 'Accept': 'application/json'})
        for attempt in range(2):
            cooldown = self.cache.get('cooldown', 'ct:' + provider)
            if cooldown and cooldown['until'] > time.time():
                delay = cooldown['until'] - time.time()
                if delay > 90:
                    return {**result, 'status': 'deferred', 'error': cooldown.get('reason', 'Retry-After')}
                self.paces[provider].postpone(delay)
            await self.paces[provider].wait()
            method, url, options = request_spec(provider, domain, self.keys[provider])
            try:
                # Persist minimum spacing as well as server cooldown across processes/runs.
                interval = self.paces[provider].interval
                self.cache.put('cooldown', 'ct:' + provider,
                    {'until': time.time()+interval, 'reason': 'minimum request spacing'}, interval)
                async with self.session.request(method, url, **options) as response:
                    result['source'] = str(response.url)
                    result['http_status'] = response.status
                    if response.status == 429:
                        delay = max(self.retry_after(response.headers.get('Retry-After')), 15 * 2**attempt)
                        self.cache.put('cooldown', 'ct:'+provider,
                            {'until': time.time()+delay, 'reason': 'HTTP 429'}, delay)
                        LOG.warning('%s HTTP 429: пауза %.1f сек', provider, delay)
                        if delay <= 90 and attempt == 0:
                            continue
                        return {**result, 'status': 'deferred', 'error': 'HTTP 429; Retry-After saved'}
                    if response.status in (401, 402, 403):
                        self.disabled[provider] = f'HTTP {response.status}: credentials or subscription required'
                        return {**result, 'status': 'restricted', 'error': self.disabled[provider]}
                    if 400 <= response.status < 500:
                        # Malformed/unsupported scope is a domain-specific failure,
                        # not evidence that the whole API is unavailable.
                        result.update(status='restricted', error=f'HTTP {response.status}: request scope rejected')
                        self.cache.put('subdomain-v2', cache_key, result, 3600)
                        return result
                    if response.status >= 500:
                        delay = max(self.retry_after(response.headers.get('Retry-After')), 5 * 2**attempt)
                        self.cache.put('cooldown', 'ct:'+provider,
                            {'until': time.time()+delay, 'reason': f'HTTP {response.status}'}, delay)
                        raise ValueError(f'HTTP {response.status}')
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > self.args.ct_max_bytes:
                            raise ValueError('response size limit exceeded')
                        chunks.append(chunk)
                    payload = json.loads(b''.join(chunks))
                    names = self.filter(domain, [{'dns_names': extract_names(provider, payload, domain)}], 1000)
                    result.update(names=names, status='OK' if names else 'empty')
                    result['data_type'] = ('ct_and_other_sources' if provider in ('subdomaincenter', 'chaos')
                                           else 'tls_scans' if provider == 'bufferover' else 'certificates')
                    self.failures[provider] = 0
                    self.cache.put('subdomain-v2', cache_key, result, 86400 if names else 3600)
                    return {**result, 'names': names[:self.args.ct_candidates]}
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
                self.failures[provider] += 1
                result['error'] = type(exc).__name__ + ': ' + str(exc)[:160]
                LOG.warning('%s %s: %s', provider, domain, result['error'])
                if self.failures[provider] >= 3:
                    self.disabled[provider] = 'three consecutive transient failures'
                    self.cache.put('cooldown', 'ct:'+provider,
                        {'until': time.time()+60, 'reason': self.disabled[provider]}, 60)
                    break
                if attempt == 0:
                    self.paces[provider].postpone(5)
        return result
