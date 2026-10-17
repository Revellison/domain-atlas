import test from 'node:test';
import assert from 'node:assert/strict';
import worker, {releaseResponse} from './index.mjs';

const payload = {schema_version: 1, generated_at: '2026-10-17T00:00:00Z', summary: {domains: 1}, domains: [{domain: 'example.com'}]};

test('downloads only the fixed latest release asset, with CORS and cache headers', async () => {
  const calls = [];
  const response = await releaseResponse('owner/report', async (url, options) => {
    calls.push([url, options]); return Response.json(payload);
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'https://github.com/owner/report/releases/latest/download/domains.json');
  assert.equal(calls[0][1].redirect, 'follow');
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), '*');
  assert.equal(response.headers.get('X-Data-Source'), 'release');
  assert.match(response.headers.get('Cache-Control'), /s-maxage=600/);
  assert.deepEqual(await response.json(), payload);
});

test('missing release is an error, never a silent demo fallback', async () => {
  const response = await releaseResponse('owner/report', async () => new Response('missing', {status: 404}));
  assert.equal(response.status, 502);
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
});

test('rejects arbitrary URLs and invalid repository names before fetching', async () => {
  for (const value of ['https://evil.example', 'owner/repo/../other', 'owner/../repo', '../repo', '']) {
    const response = await releaseResponse(value, () => { throw new Error('must not fetch'); });
    assert.equal(response.status, 503);
  }
});

test('rejects invalid schema, oversized responses and failed network requests', async () => {
  for (const fetcher of [
    async () => Response.json({schema_version: 999}),
    async () => Response.json({...payload, summary: {domains: 2}}),
    async () => new Response('tiny', {headers: {'Content-Length': '9000000'}}),
    async () => new Response(new Uint8Array(8_000_001)),
    async () => { throw new Error('connection reset'); },
  ]) assert.equal((await releaseResponse('owner/report', fetcher)).status, 502);
});

test('unconfigured Worker serves the local snapshot and labels its source', async () => {
  const paths = [];
  const env = {REPOSITORY: 'OWNER/REPO', ASSETS: {fetch: async r => {
    paths.push(new URL(r.url).pathname); return Response.json(payload);
  }}};
  const result = await worker.fetch(new Request('https://example.test/api/domains'), env, {});
  assert.equal(result.headers.get('X-Data-Source'), 'snapshot');
  assert.deepEqual(paths, ['/data/domains.json']);
  assert.deepEqual(await result.json(), payload);
});

test('supports HEAD, preflight, rejects writes, delegates static assets', async () => {
  const env = {REPOSITORY: 'OWNER/REPO', ASSETS: {fetch: async () => Response.json(payload)}};
  const head = await worker.fetch(new Request('https://example.test/api/domains', {method: 'HEAD'}), env, {});
  assert.equal(await head.text(), '');
  const preflight = await worker.fetch(new Request('https://example.test/api/domains', {method: 'OPTIONS'}), env, {});
  assert.equal(preflight.status, 204);
  const post = await worker.fetch(new Request('https://example.test/api/domains', {method: 'POST'}), env, {});
  assert.equal(post.status, 405);
  const asset = await worker.fetch(new Request('https://example.test/styles.css'), env, {});
  assert.equal(asset.status, 200);
});

test('edge cache is scoped to the repository and ignores cache-busting client queries', async () => {
  const keys = [];
  const original = globalThis.caches;
  globalThis.caches = {default: {match: async request => {
    keys.push(request.url); return Response.json(payload, {headers: {'X-Data-Source': 'release'}});
  }}};
  try {
    const env = {REPOSITORY: 'owner/report'};
    await worker.fetch(new Request('https://example.test/api/domains?random=123'), env, {});
    await worker.fetch(new Request('https://example.test/api/domains?random=456'), env, {});
    assert.equal(keys[0], keys[1]);
    assert.match(keys[0], /owner\/report$/);
  } finally { globalThis.caches = original; }
});
