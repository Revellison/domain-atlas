(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const config = window.DOMAIN_CONFIG || {};
  const rows = document.getElementById('rows');
  const description = document.getElementById('table-description');
  const nf = new Intl.NumberFormat('ru-RU');
  const search = document.getElementById('search');
  const providerToggle = $('provider-toggle');
  const providerPopup = $('provider-popup');
  const providerSearch = $('provider-search');
  const hostList = $('provider-options');
  const status = document.getElementById('status');
  const reset = document.getElementById('reset');
  const count = document.getElementById('result-count');
  let domains = [];
  let loaded = false;
  let filtered = [];
  let page = 1;
  let providerID = '';
  let providers = [];
  let hostResults = [];
  let hostPage = 0;
  let hostActive = -1;
  const HOST_PAGE_SIZE = 20;
  const frame = window.requestAnimationFrame?.bind(window) || (callback => setTimeout(callback, 0));
  const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches || false;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (tag === 'tr') node.setAttribute('role', 'row');
    if (tag === 'td') node.setAttribute('role', 'cell');
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function siteLink(value) {
    let url;
    try { url = new URL(value); } catch { return el('span', 'muted', 'Не найден'); }
    if (!['https:', 'http:'].includes(url.protocol) || url.username) return el('span', 'muted', 'Не найден');
    const link = el('a', 'website-link', url.hostname.replace(/^www\./, '') + ' ↗');
    link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
    return link;
  }
  function validate(payload) {
    if (payload.schema_version !== 1 || !Array.isArray(payload.domains) ||
        payload.summary?.domains !== payload.domains.length || !Number.isFinite(Date.parse(payload.generated_at))) {
      throw new Error('Неподдерживаемый формат снимка');
    }
    const names = new Set();
    for (const d of payload.domains) {
      if (!d.domain || names.has(d.domain) || !Array.isArray(d.addresses) || !Array.isArray(d.providers) || !d.group?.id) {
        throw new Error('Повреждённая запись домена');
      }
      names.add(d.domain);
      for (const a of d.addresses) {
        if (typeof a.ip !== 'string' || !/^(\d{1,3}\.){3}\d{1,3}$/.test(a.ip) ||
            a.ip.split('.').some(x => +x > 255) || !Array.isArray(a.networks) || !Array.isArray(a.resolved_via)) {
          throw new Error('Повреждённая запись IPv4');
        }
      }
      d._search = [d.domain, ...d.addresses.flatMap(a => [a.ip, ...a.resolved_via]),
        ...d.addresses.flatMap(a => a.networks.flatMap(n => [n.prefix, n.asn ? 'AS' + n.asn : '', n.operator, n.website]))]
        .join(' ').toLowerCase();
    }
  }
  async function getJSON(url) {
    const response = await fetch(url, {signal: AbortSignal.timeout(25000), cache: 'no-cache'});
    if (!response.ok) {
      let message = `Не удалось загрузить снимок (HTTP ${response.status})`;
      try { const body = await response.json(); if (typeof body.error === 'string') message = body.error; } catch { /* Keep HTTP status. */ }
      const error = new Error(message); error.status = response.status; throw error;
    }
    return {payload: await response.json(), response};
  }
  function makeDetail(d, id) {
    const row = el('tr', 'detail-row'); row.id = id;
    const cell = el('td'); cell.colSpan = 5;
    const box = el('div', 'detail-box');
    const heading = el('div', 'detail-heading');
    heading.append(el('span', '', d.addresses.length ? `IPv4-адресов: ${d.addresses.length}` : 'Результат проверки'));
    box.append(heading);
    if (!d.addresses.length) box.append(el('div', 'detail-status', d.status || 'IPv4 не найден'));
    for (const a of d.addresses) {
      const address = el('div', 'detail-address');
      const ip = el('div'); ip.append(el('span', 'mono', a.ip));
      for (const host of a.resolved_via) ip.append(el('span', 'via-host', host === d.domain ? 'Прямой DNS' : '↳ ' + host));
      const networks = el('div');
      for (const n of a.networks) {
        const network = el('div', 'detail-network');
        network.append(el('span', 'mono', n.prefix || 'Диапазон неизвестен'),
          el('span', 'asn', n.asn ? 'AS' + n.asn : 'ASN —'), el('span', '', n.operator), siteLink(n.website));
        ['Диапазон', 'ASN', 'Оператор', 'Сайт оператора'].forEach((label, i) => {
          network.children[i].dataset.label = label;
        });
        networks.append(network);
      }
      address.append(ip, networks); box.append(address);
    }
    const clip = el('div', 'detail-clip');
    const body = el('div', 'detail-body');
    body.append(box); clip.append(body); cell.append(clip); row.append(cell); return row;
  }
  function makeRow(d, index) {
    const row = el('tr', 'domain-row');
    const id = 'details-' + index;
    row.tabIndex = 0;
    row.setAttribute('aria-expanded', 'false');
    row.setAttribute('aria-describedby', 'table-description');
    const cells = Array.from({length: 5}, () => el('td'));
    ['Домен', 'IPv4', 'Диапазон / BGP', 'ASN / оператор', 'Сайт оператора'].forEach((label, i) => {
      cells[i].dataset.label = label;
    });
    const primary = d.addresses.find(a => a.networks.some(n => n.provider_id === d.group.id)) || d.addresses[0];
    const network = primary?.networks.find(n => n.provider_id === d.group.id) || primary?.networks[0];
    cells[0].append(el('div', 'domain-name', d.domain));
    const meta = el('div', 'domain-meta');
    if (d.cdn) meta.append(el('span', 'tag cdn', 'CDN'));
    if (d.resolution === 'subdomain') meta.append(el('span', 'tag subdomain', 'через поддомен'));
    if (!d.addresses.length) meta.append(el('span', 'tag missing', 'IPv4 не найден'));
    if (meta.childNodes.length) cells[0].append(meta);
    if (primary) {
      const ip = el('span', 'mono ip-value', primary.ip);
      if (d.addresses.length > 1) ip.append(el('span', 'ip-count', '+' + (d.addresses.length - 1)));
      cells[1].append(ip);
    } else cells[1].append(el('span', 'muted', 'Не найден'));
    cells[2].append(el('span', 'mono', network?.prefix || '—'));
    const allNetworks = d.addresses.flatMap(a => a.networks);
    const prefixes = new Set(allNetworks.map(n => n.prefix).filter(Boolean));
    if (prefixes.size > 1) cells[2].append(el('div', 'secondary', 'ещё диапазонов: ' + (prefixes.size - 1)));
    const asns = [...new Set([network?.asn, ...allNetworks.map(n => n.asn)].filter(Boolean))];
    cells[3].append(el('span', 'asn', asns.length ? 'AS' + asns[0] + (asns.length > 1 ? ' +' + (asns.length - 1) : '') : '—'),
      el('div', 'secondary', d.providers[0]?.name || 'Оператор не определён'));
    if (d.providers.length > 1) cells[3].append(el('div', 'muted', 'ещё операторов: ' + (d.providers.length - 1)));
    cells[4].append(siteLink(d.providers[0]?.website));
    row.append(...cells);
    let detail;
    let closeTimer;
    function toggle() {
      const expanded = row.getAttribute('aria-expanded') !== 'true';
      clearTimeout(closeTimer);
      row.setAttribute('aria-expanded', String(expanded));
      row.classList.toggle('is-expanded', expanded);
      if (expanded) {
        if (!detail) { detail = makeDetail(d, id); row.after(detail); }
        row.setAttribute('aria-controls', id);
        detail.removeAttribute('aria-hidden'); detail.inert = false;
        const panel = detail;
        frame(() => frame(() => {
          if (row.isConnected && detail === panel && row.getAttribute('aria-expanded') === 'true') panel.classList.add('is-open');
        }));
      } else if (detail) {
        const panel = detail;
        panel.classList.remove('is-open'); panel.setAttribute('aria-hidden', 'true'); panel.inert = true;
        closeTimer = setTimeout(() => {
          if (detail === panel && row.getAttribute('aria-expanded') === 'false') {
            panel.remove(); detail = null; row.removeAttribute('aria-controls');
          }
        }, reducedMotion() ? 0 : 190);
      }
    }
    row.addEventListener('click', event => {
      if (event.target.closest('a')) return;
      // Copying a selected IP/domain should not collapse its details.
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed && row.contains(selection.anchorNode)) return;
      toggle();
    });
    row.addEventListener('keydown', event => {
      if (event.target === row && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); toggle(); }
    });
    return row;
  }
  function renderPage() {
    if (!loaded) return;
    const size = +$('page-size').value;
    const totalPages = Math.max(1, Math.ceil(filtered.length / size));
    page = Math.max(1, Math.min(page, totalPages));
    const start = (page - 1) * size;
    const items = filtered.slice(start, start + size);
    const fragment = document.createDocumentFragment();
    let lastGroup;
    items.forEach((d, index) => {
      if (lastGroup !== d.group.id) {
        lastGroup = d.group.id;
        const row = el('tr', 'group-row'); const cell = el('td', '', d.group.name); cell.colSpan = 5;
        cell.append(el('span', '', d.group.asns.map(asn => 'AS' + asn).join(' · ')));
        row.append(cell); fragment.append(row);
      }
      fragment.append(makeRow(d, start + index));
    });
    if (!filtered.length) {
      const row = el('tr'); const cell = el('td', 'empty', domains.length ? 'Ничего не найдено. Измените запрос или сбросьте фильтры.' : 'В снимке нет доменов.');
      cell.colSpan = 5; row.append(cell); fragment.append(row);
    }
    rows.replaceChildren(fragment);
    $('pagination').hidden = false;
    $('page-summary').textContent = filtered.length ? `${nf.format(start + 1)}–${nf.format(Math.min(start + size, filtered.length))} из ${nf.format(filtered.length)}` : 'Нет результатов';
    $('page-count').textContent = `${page} / ${totalPages}`;
    $('previous').disabled = page <= 1;
    $('next').disabled = page >= totalPages;
    renderPageNumbers(totalPages);
    document.querySelector('.table-scroll').scrollTop = 0;
  }
  function renderPageNumbers(total) {
    const numbers = new Set([1, total, page]);
    for (let n = Math.max(1, page - 1); n <= Math.min(total, page + 1); n++) numbers.add(n);
    if (page <= 2) for (let n = 1; n <= Math.min(3, total); n++) numbers.add(n);
    if (page >= total - 1) for (let n = Math.max(1, total - 2); n <= total; n++) numbers.add(n);
    const fragment = document.createDocumentFragment();
    let previous = 0;
    for (const n of [...numbers].sort((a, b) => a - b)) {
      if (previous && n - previous > 1) fragment.append(el('span', 'page-gap', '…'));
      const button = el('button', 'page-button', String(n)); button.type = 'button';
      button.setAttribute('aria-label', `Страница ${n}`);
      if (n === page) button.setAttribute('aria-current', 'page');
      button.addEventListener('click', () => goToPage(n)); fragment.append(button); previous = n;
    }
    $('page-numbers').replaceChildren(fragment);
  }
  function goToPage(nextPage) {
    page = nextPage; renderPage();
    if (window.matchMedia?.('(max-width: 1140px)')?.matches) {
      document.querySelector('.table-scroll').scrollIntoView?.({block: 'start', behavior: reducedMotion() ? 'auto' : 'smooth'});
    }
    rows.querySelector('.domain-row')?.focus({preventScroll: true});
  }
  function populateProviders() {
    const options = new Map();
    for (const d of domains) for (const p of d.providers) {
      const item = options.get(p.id) || {name: p.name, asns: new Set(), count: 0};
      item.count++;
      p.asns.forEach(asn => item.asns.add(asn));
      options.set(p.id, item);
    }
    providers = [{id: '', name: 'Все хостеры', asns: [], count: domains.length},
      ...[...options].sort((a, b) => a[1].name.localeCompare(b[1].name, 'ru', {numeric: true}))
        .map(([id, item]) => ({...item, id, asns: [...item.asns].sort((a, b) => a - b)}))];
    for (const p of providers) p.search = `${p.name} ${p.asns.map(n => 'AS' + n).join(' ')}`.toLowerCase();
    if (!providerPopup.hidden) filterHosts();
  }
  function syncPopupSize() {
    const viewport = window.visualViewport;
    providerPopup.style.setProperty('--viewport-height', `${viewport?.height || window.innerHeight}px`);
    providerPopup.style.setProperty('--popup-top', `${(viewport?.offsetTop || 0) + 8}px`);
  }
  function openProviders() {
    providerPopup.hidden = false;
    providerToggle.setAttribute('aria-expanded', 'true'); providerSearch.setAttribute('aria-expanded', 'true');
    providerSearch.value = ''; hostResults = providers;
    hostPage = Math.max(0, Math.floor(providers.findIndex(p => p.id === providerID) / HOST_PAGE_SIZE));
    syncPopupSize(); renderHosts(); providerSearch.focus();
  }
  function closeProviders(returnFocus = false) {
    providerPopup.hidden = true; hostList.replaceChildren();
    providerToggle.setAttribute('aria-expanded', 'false'); providerSearch.setAttribute('aria-expanded', 'false');
    providerSearch.removeAttribute('aria-activedescendant'); hostActive = -1;
    if (returnFocus) providerToggle.focus();
  }
  function filterHosts() {
    const tokens = providerSearch.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    hostResults = providers.filter(p => tokens.every(token => p.search.includes(token)));
    hostPage = 0; renderHosts();
  }
  function renderHosts() {
    hostActive = -1; providerSearch.removeAttribute('aria-activedescendant');
    const start = hostPage * HOST_PAGE_SIZE;
    const fragment = document.createDocumentFragment();
    hostResults.slice(start, start + HOST_PAGE_SIZE).forEach((p, index) => {
      const option = el('button', 'host-option'); option.type = 'button'; option.tabIndex = -1;
      option.id = 'host-option-' + index; option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', String(p.id === providerID));
      option.append(el('strong', '', p.name), el('small', '',
        (p.asns.length ? p.asns.slice(0, 3).map(n => 'AS' + n).join(' · ') + (p.asns.length > 3 ? ' +' + (p.asns.length - 3) : '') + ' · ' : '') + 'Доменов: ' + nf.format(p.count)));
      option.addEventListener('click', () => chooseProvider(p)); fragment.append(option);
    });
    if (!hostResults.length) fragment.append(el('p', 'host-empty', 'Хостер не найден'));
    hostList.replaceChildren(fragment); hostList.scrollTop = 0;
    $('host-count').textContent = hostResults.length ? `${start + 1}–${Math.min(start + HOST_PAGE_SIZE, hostResults.length)} из ${hostResults.length}` : 'Нет результатов';
    $('host-previous').disabled = hostPage === 0;
    $('host-next').disabled = start + HOST_PAGE_SIZE >= hostResults.length;
  }
  function chooseProvider(p) {
    providerID = p.id;
    $('provider-label').textContent = p.name; providerToggle.title = p.name;
    providerToggle.classList.toggle('is-filtered', Boolean(p.id));
    closeProviders(true); clearTimeout(searchTimer); applyFilters();
  }
  function applyFilters() {
    if (!loaded) return;
    const tokens = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    filtered = domains.filter(d => tokens.every(token => d._search.includes(token)) &&
      (!providerID || d.providers.some(p => p.id === providerID)) &&
      (status.value === 'all' || status.value === 'resolved' && d.addresses.length > 0 ||
        status.value === 'unresolved' && !d.addresses.length || status.value === 'multi' && d.addresses.length > 1 ||
        status.value === 'cdn' && d.cdn || status.value === 'subdomain' && d.resolution === 'subdomain'));
    page = 1; renderPage();
    count.textContent = `${nf.format(filtered.length)} из ${nf.format(domains.length)}`;
    reset.disabled = !tokens.length && !providerID && status.value === 'all';
  }
  async function load() {
    try {
      let result;
      let demo = new URLSearchParams(location.search).get('demo') === '1';
      const automatic = !config.dataUrl || config.dataUrl === 'auto';
      if (demo) result = await getJSON(config.demoUrl || './data/domains.json');
      else {
        try { result = await getJSON(automatic ? './api/domains' : config.dataUrl); }
        catch (error) {
          // Only a missing local route permits the explicitly labelled snapshot.
          if (!automatic || error.status !== 404) throw error;
          demo = true; result = await getJSON(config.demoUrl || './data/domains.json');
        }
      }
      demo ||= result.response.headers.get('X-Data-Source') === 'snapshot';
      validate(result.payload);
      const date = new Intl.DateTimeFormat('ru-RU', {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(result.payload.generated_at));
      const source = demo ? 'Локальный снимок' : 'GitHub Release' + (config.releaseTag ? ' · ' + config.releaseTag : '');
      document.title = `Домены · ${source} · ${date}`;
      const warnings = (result.payload.warnings || []).map(w => String(w).replace(/,?\s*подробности в details\.json/gi, '')).join(' · ');
      description.textContent = `${source}, ${date}. Доменов: ${nf.format(result.payload.domains.length)}. ` +
        'Группировка по оператору и ASN. Нажмите на строку или используйте Enter либо пробел, чтобы раскрыть все IP.' + (warnings ? ' ' + warnings : '');
      domains = result.payload.domains;
      loaded = true;
      populateProviders();
      applyFilters();
    } catch (error) {
      const row = el('tr');
      const cell = el('td', 'empty error', (error.name === 'TimeoutError' ? 'Источник данных не ответил за 25 секунд.' : error.message) + ' Обновите страницу, чтобы повторить загрузку.');
      cell.colSpan = 5; cell.setAttribute('role', 'alert'); row.append(cell); rows.replaceChildren(row);
    }
  }
  let searchTimer;
  search.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(applyFilters, 160); });
  status.addEventListener('change', () => {
    clearTimeout(searchTimer); applyFilters();
  });
  document.getElementById('filters').addEventListener('submit', event => {
    event.preventDefault(); clearTimeout(searchTimer); applyFilters();
  });
  reset.addEventListener('click', () => {
    clearTimeout(searchTimer); search.value = ''; providerID = ''; status.value = 'all';
    $('provider-label').textContent = 'Все хостеры'; providerToggle.title = '';
    providerToggle.classList.remove('is-filtered'); closeProviders(); applyFilters();
  });
  $('previous').addEventListener('click', () => goToPage(page - 1));
  $('next').addEventListener('click', () => goToPage(page + 1));
  $('page-size').addEventListener('change', () => { page = 1; renderPage(); });
  providerToggle.addEventListener('click', () => providerPopup.hidden ? openProviders() : closeProviders(true));
  $('provider-close').addEventListener('click', () => closeProviders(true));
  providerSearch.addEventListener('input', filterHosts);
  $('host-previous').addEventListener('click', () => { hostPage--; renderHosts(); });
  $('host-next').addEventListener('click', () => { hostPage++; renderHosts(); });
  providerPopup.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeProviders(true); }
    if (event.target !== providerSearch) return;
    const options = [...hostList.querySelectorAll('[role=option]')];
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!options.length) return;
      hostActive = event.key === 'ArrowDown' ? Math.min(hostActive + 1, options.length - 1) : Math.max(0, hostActive - 1);
      options.forEach((option, i) => option.classList.toggle('is-active', i === hostActive));
      providerSearch.setAttribute('aria-activedescendant', options[hostActive].id);
      options[hostActive].scrollIntoView?.({block: 'nearest'});
    }
    if (event.key === 'Enter') { event.preventDefault(); options[Math.max(0, hostActive)]?.click(); }
  });
  document.addEventListener('pointerdown', event => {
    if (!providerPopup.hidden && !providerPopup.contains(event.target) && !providerToggle.contains(event.target)) closeProviders();
  });
  document.addEventListener('focusin', event => {
    if (!providerPopup.hidden && !providerPopup.contains(event.target) && !providerToggle.contains(event.target)) closeProviders();
  });
  window.visualViewport?.addEventListener('resize', syncPopupSize);
  window.addEventListener('resize', syncPopupSize);
  document.addEventListener('keydown', event => {
    if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
      event.preventDefault(); search.focus();
    }
    if (event.key === 'Escape' && document.activeElement === search) {
      clearTimeout(searchTimer); search.value = ''; applyFilters();
    }
  });
  load();
})();
