(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const config = window.DOMAIN_CONFIG || {};
  const rows = $('rows');
  const tableScroll = $('table-scroll');
  const nf = new Intl.NumberFormat('ru-RU');
  const search = $('search');
  const providerToggle = $('provider-toggle');
  const providerPopup = $('provider-popup');
  const providerSearch = $('provider-search');
  const hostList = $('provider-options');
  const status = $('status');
  const reset = $('reset');
  const count = $('result-count');
  const pageSize = $('page-size');
  const pageNumbers = $('page-numbers');
  const previousPage = $('previous');
  const nextPage = $('next');
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
  function icon(name) {
    const namespace = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(namespace, 'svg');
    const use = document.createElementNS(namespace, 'use');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    use.setAttribute('href', '#icon-' + name);
    svg.append(use);
    return svg;
  }
  function siteLink(value) {
    let url;
    try { url = new URL(value); } catch { return el('span', 'muted', 'Не найден'); }
    if (!['https:', 'http:'].includes(url.protocol) || url.username) return el('span', 'muted', 'Не найден');
    const link = el('a', 'website-link', url.hostname.replace(/^www\./, ''));
    link.append(icon('external'));
    link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
    return link;
  }
  function validate(payload) {
    if (payload?.schema_version !== 1 || !Array.isArray(payload.domains) ||
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
    heading.textContent = d.addresses.length ? `IPv4-адресов: ${d.addresses.length}` : 'Результат проверки';
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
    const disclosure = el('button', 'icon-button disclosure');
    disclosure.type = 'button';
    disclosure.setAttribute('aria-label', 'Подробности о домене ' + d.domain);
    disclosure.setAttribute('aria-expanded', 'false');
    disclosure.append(icon('right'));
    const cells = Array.from({length: 5}, () => el('td'));
    const primary = d.addresses.find(a => a.networks.some(n => n.provider_id === d.group.id)) || d.addresses[0];
    const network = primary?.networks.find(n => n.provider_id === d.group.id) || primary?.networks[0];
    const domainCell = el('div', 'domain-cell');
    const domainInfo = el('div');
    domainInfo.append(el('div', 'domain-name', d.domain));
    const meta = el('div', 'domain-meta');
    if (d.cdn) meta.append(el('span', 'tag cdn', 'CDN'));
    if (d.resolution === 'subdomain') meta.append(el('span', 'tag subdomain', 'через поддомен'));
    if (!d.addresses.length) meta.append(el('span', 'tag missing', 'IPv4 не найден'));
    if (meta.childNodes.length) domainInfo.append(meta);
    domainCell.append(disclosure, domainInfo);
    cells[0].append(domainCell);
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
      const expanded = disclosure.getAttribute('aria-expanded') !== 'true';
      clearTimeout(closeTimer);
      disclosure.setAttribute('aria-expanded', String(expanded));
      row.classList.toggle('is-expanded', expanded);
      if (expanded) {
        if (!detail) { detail = makeDetail(d, id); row.after(detail); }
        disclosure.setAttribute('aria-controls', id);
        detail.removeAttribute('aria-hidden'); detail.inert = false;
        const panel = detail;
        frame(() => frame(() => {
          if (row.isConnected && detail === panel && disclosure.getAttribute('aria-expanded') === 'true') panel.classList.add('is-open');
        }));
      } else if (detail) {
        const panel = detail;
        panel.classList.remove('is-open'); panel.setAttribute('aria-hidden', 'true'); panel.inert = true;
        closeTimer = setTimeout(() => {
          if (detail === panel && disclosure.getAttribute('aria-expanded') === 'false') {
            panel.remove(); detail = null; disclosure.removeAttribute('aria-controls');
          }
        }, reducedMotion() ? 0 : 190);
      }
    }
    disclosure.addEventListener('click', toggle);
    row.addEventListener('click', event => {
      if (event.target.closest('a, button')) return;
      // Copying a selected IP/domain should not collapse its details.
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed && row.contains(selection.anchorNode)) return;
      toggle();
    });
    return row;
  }
  function renderPage() {
    if (!loaded) return;
    const size = Number(pageSize.value);
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
        if (d.group.asns.length) cell.append(el('span', '', d.group.asns.map(asn => 'AS' + asn).join(' · ')));
        row.append(cell); fragment.append(row);
      }
      fragment.append(makeRow(d, start + index));
    });
    if (!filtered.length) {
      const row = el('tr'); const cell = el('td', 'empty', domains.length ? 'Ничего не найдено. Измените запрос или сбросьте фильтры.' : 'В снимке нет доменов.');
      cell.colSpan = 5; row.append(cell); fragment.append(row);
    }
    rows.replaceChildren(fragment);
    $('pagination').hidden = !filtered.length;
    $('page-navigation').hidden = totalPages === 1;
    $('page-summary').textContent = filtered.length ? `${nf.format(start + 1)}–${nf.format(Math.min(start + size, filtered.length))} из ${nf.format(filtered.length)}` : '';
    $('page-count').textContent = `${page} / ${totalPages}`;
    previousPage.disabled = page <= 1;
    nextPage.disabled = page >= totalPages;
    renderPageNumbers(totalPages);
    tableScroll.scrollTop = 0;
  }
  function renderPageNumbers(total) {
    let numbers;
    if (total <= 7) numbers = Array.from({length: total}, (_, i) => i + 1);
    else if (page <= 4) numbers = [1, 2, 3, 4, 5, null, total];
    else if (page >= total - 3) numbers = [1, null, total - 4, total - 3, total - 2, total - 1, total];
    else numbers = [1, null, page - 1, page, page + 1, null, total];
    const fragment = document.createDocumentFragment();
    for (const number of numbers) {
      if (number === null) {
        const gap = el('span', 'page-gap', '…');
        gap.setAttribute('aria-hidden', 'true');
        fragment.append(gap);
        continue;
      }
      const button = el('button', 'page-button', String(number)); button.type = 'button';
      button.setAttribute('aria-label', `Страница ${number}`);
      if (number === page) button.setAttribute('aria-current', 'page');
      button.addEventListener('click', event => goToPage(number, event));
      fragment.append(button);
    }
    pageNumbers.replaceChildren(fragment);
  }
  function goToPage(number, event) {
    if (number === page) return;
    const control = event.currentTarget;
    const compact = window.matchMedia('(max-width: 1140px)').matches;
    page = number;
    renderPage();
    if (compact) {
      tableScroll.scrollIntoView({block: 'start', behavior: reducedMotion() ? 'auto' : 'smooth'});
    }
    if (event.detail === 0) {
      const target = compact ? rows.querySelector('.disclosure') :
        control.isConnected && !control.disabled ? control : pageNumbers.querySelector('[aria-current=page]');
      target?.focus({preventScroll: true});
    }
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
    if (providerPopup.hidden) return;
    const viewport = window.visualViewport;
    providerPopup.style.setProperty('--viewport-height', `${viewport?.height || window.innerHeight}px`);
    providerPopup.style.setProperty('--popup-top', `${(viewport?.offsetTop || 0) + 8}px`);
    const offset = providerToggle.getBoundingClientRect().bottom - (viewport?.offsetTop || 0) + 6;
    providerPopup.style.setProperty('--popup-offset', `${offset}px`);
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
    $('host-count').textContent = hostResults.length ? `${nf.format(start + 1)}–${nf.format(Math.min(start + HOST_PAGE_SIZE, hostResults.length))} из ${nf.format(hostResults.length)}` : 'Нет результатов';
    $('host-previous').disabled = hostPage === 0;
    $('host-next').disabled = start + HOST_PAGE_SIZE >= hostResults.length;
  }
  function chooseProvider(p) {
    providerID = p.id;
    $('provider-label').textContent = p.name; providerToggle.title = p.name;
    providerToggle.classList.toggle('is-filtered', Boolean(p.id));
    closeProviders(true); applyFilters();
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
    count.textContent = `Домены: ${nf.format(filtered.length)} из ${nf.format(domains.length)}`;
    reset.disabled = !tokens.length && !providerID && status.value === 'all';
  }
  async function load() {
    try {
      let result;
      const automatic = !config.dataUrl || config.dataUrl === 'auto';
      const demoUrl = config.demoUrl || './data/domains.json';
      const wantsDemo = new URLSearchParams(location.search).get('demo') === '1';
      let demo = wantsDemo && (automatic || new URL(demoUrl, location.href).href !== new URL(config.dataUrl, location.href).href);
      if (demo) result = await getJSON(demoUrl);
      else {
        try { result = await getJSON(automatic ? './api/domains' : config.dataUrl); }
        catch (error) {
          // Only a missing local route permits the explicitly labelled snapshot.
          if (!automatic || error.status !== 404) throw error;
          demo = true; result = await getJSON(demoUrl);
        }
      }
      demo ||= result.response.headers.get('X-Data-Source') === 'snapshot';
      validate(result.payload);
      const timestamp = result.payload.generated_at;
      const date = new Intl.DateTimeFormat('ru-RU', {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(timestamp));
      const source = demo ? 'Локальный снимок' : 'GitHub Release';
      document.title = `Домены · ${source} · ${date}`;
      $('snapshot-source').textContent = source;
      if (!demo && config.releaseTag) $('snapshot-source').title = config.releaseTag;
      $('snapshot-date').dateTime = timestamp;
      $('snapshot-date').textContent = date;
      $('snapshot').hidden = false;
      const warnings = (result.payload.warnings || []).map(w => String(w).replace(/,?\s*подробности в details\.json/gi, '')).join(' · ');
      $('snapshot-warning').textContent = warnings;
      $('snapshot-notice').hidden = !warnings;
      domains = result.payload.domains;
      loaded = true;
      populateProviders();
      providerToggle.disabled = false;
      applyFilters();
    } catch (error) {
      const row = el('tr');
      const cell = el('td', 'empty error', (error.name === 'TimeoutError' ? 'Источник данных не ответил за 25 секунд.' : error.message) + ' Обновите страницу, чтобы повторить загрузку.');
      cell.colSpan = 5; cell.setAttribute('role', 'alert'); row.append(cell); rows.replaceChildren(row);
    }
  }
  search.addEventListener('input', applyFilters);
  status.addEventListener('change', applyFilters);
  $('filters').addEventListener('submit', event => {
    event.preventDefault(); applyFilters();
  });
  reset.addEventListener('click', () => {
    search.value = ''; providerID = ''; status.value = 'all';
    $('provider-label').textContent = 'Все хостеры'; providerToggle.title = '';
    providerToggle.classList.remove('is-filtered'); closeProviders(); search.focus(); applyFilters();
  });
  previousPage.addEventListener('click', event => goToPage(page - 1, event));
  nextPage.addEventListener('click', event => goToPage(page + 1, event));
  pageSize.addEventListener('change', () => {
    page = 1;
    renderPage();
    if (window.matchMedia('(max-width: 1140px)').matches) tableScroll.scrollIntoView({block: 'start'});
  });
  providerToggle.addEventListener('click', () => providerPopup.hidden ? openProviders() : closeProviders(true));
  $('provider-close').addEventListener('click', () => closeProviders(true));
  providerSearch.addEventListener('input', filterHosts);
  const hostPrevious = $('host-previous');
  const hostNext = $('host-next');
  for (const [button, direction] of [[hostPrevious, -1], [hostNext, 1]]) {
    button.addEventListener('click', event => {
      hostPage += direction;
      renderHosts();
      if (event.detail === 0 && button.disabled) (direction < 0 ? hostNext : hostPrevious).focus();
    });
  }
  providerPopup.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopPropagation(); closeProviders(true);
      return;
    }
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
  window.visualViewport?.addEventListener('scroll', syncPopupSize);
  window.addEventListener('resize', syncPopupSize);
  document.addEventListener('keydown', event => {
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey &&
        !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
      event.preventDefault(); search.focus();
    }
    if (event.key === 'Escape' && document.activeElement === search) {
      search.value = ''; applyFilters();
    }
  });
  load();
})();
