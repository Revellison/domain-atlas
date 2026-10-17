// Fixed public repository only. No arbitrary-URL proxy and no browser credentials.
const MAX_BYTES = 8_000_000;
const REPO = /^[A-Za-z0-9_-][A-Za-z0-9_.-]*\/[A-Za-z0-9_-][A-Za-z0-9_.-]*$/;

function json(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {status, headers: {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Expose-Headers': 'X-Data-Source, X-Report-Release',
    'X-Content-Type-Options': 'nosniff',
    'Cache-Control': 'no-store', ...headers,
  }});
}

export async function releaseResponse(repository, fetcher = fetch) {
  if (!REPO.test(repository)) return json({error: 'Задайте REPOSITORY в worker/wrangler.toml как owner/repo'}, 503);
  const signal = AbortSignal.timeout(20000);
  try {
    // Stable download URL avoids GitHub REST API's anonymous quota entirely.
    const download = await fetcher(`https://github.com/${repository}/releases/latest/download/domains.json`, {
      headers: {'User-Agent': 'DomainAtlas/1.0'}, redirect: 'follow', signal,
    });
    if (!download.ok) return json({error: download.status === 404 ?
      'domains.json в публичном latest-релизе не найден. Сначала запустите GitHub Actions.' :
      `JSON релиза недоступен (HTTP ${download.status}).`}, 502);
    if (+download.headers.get('Content-Length') > MAX_BYTES) return json({error: 'JSON превышает 8 MB.'}, 502);
    // Read with a limit even if the upstream omits Content-Length or reports the wrong size.
    const reader = download.body.getReader();
    const chunks = []; let length = 0;
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > MAX_BYTES) { await reader.cancel(); return json({error: 'JSON превышает 8 MB.'}, 502); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(length); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const payload = JSON.parse(new TextDecoder().decode(bytes));
    if (payload.schema_version !== 1 || !Array.isArray(payload.domains) || !payload.summary ||
        payload.summary.domains !== payload.domains.length) return json({error: 'Некорректный формат JSON в релизе.'}, 502);
    const headers = {'X-Data-Source': 'release', 'Cache-Control': 'public, max-age=300, s-maxage=600',
      'X-Report-Release': `https://github.com/${repository}/releases/latest`};
    return json(payload, 200, headers);
  } catch (error) {
    return json({error: error.name === 'TimeoutError' ? 'GitHub не ответил за 20 секунд.' : 'Не удалось прочитать JSON из GitHub Release.'}, 502);
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname !== '/api/domains') return env.ASSETS.fetch(request);
    if (request.method === 'OPTIONS') return new Response(null, {status: 204, headers: {
      'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS', 'Access-Control-Max-Age': '86400',
    }});
    if (!['GET', 'HEAD'].includes(request.method)) return json({error: 'Method not allowed'}, 405, {Allow: 'GET, HEAD, OPTIONS'});
    if (!env.REPOSITORY || env.REPOSITORY === 'OWNER/REPO') {
      const demo = await env.ASSETS.fetch(new Request(new URL('/data/domains.json', url)));
      const headers = new Headers(demo.headers); headers.set('X-Data-Source', 'snapshot');
      headers.set('Access-Control-Allow-Origin', '*'); headers.set('Access-Control-Expose-Headers', 'X-Data-Source');
      return new Response(request.method === 'HEAD' ? null : demo.body, {status: demo.status, headers});
    }
    const keyURL = new URL(request.url); keyURL.pathname = '/__release-cache/' + env.REPOSITORY; keyURL.search = '';
    const cacheKey = new Request(keyURL, {method: 'GET'});
    let response = await caches.default.match(cacheKey);
    if (!response) {
      response = await releaseResponse(env.REPOSITORY);
      if (response.ok) ctx.waitUntil(caches.default.put(cacheKey, response.clone()));
    }
    return request.method === 'HEAD' ? new Response(null, {status: response.status, headers: response.headers}) : response;
  },
};
