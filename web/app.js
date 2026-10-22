/* ==========================================================================
   gameqker - 国产游戏出海信息差情报表 · 前端渲染
   数据来源: web/data/games.json、events.json 与 history.json（由 collector/update.py 巡检后导出）
   ========================================================================== */

'use strict';

const FIELD_LABELS = {
  steam_listed: 'Steam 上架',
  steam_release_date: 'Steam 发售日',
  official_signal: '官网信号',
  last_checked: '巡检时间',
};

const LEVEL_ORDER = { '高': 4, '中高': 3, '中': 2, '低中': 1, '低': 0 };
const LEVEL_CLASS = { '高': 'lv-high', '中高': 'lv-midhigh', '中': 'lv-mid', '低中': 'lv-midlow', '低': 'lv-low' };
const LEVEL_VALUES = ['高', '中高', '中', '低中', '低'];

const CHANGED_WINDOW_MS = 24 * 60 * 60 * 1000; // 巡检每天一次，24 小时内的变更即「今日变更」
const NEAR_DAYS = 2; // 上线前 2 天进入倒计时提醒

const state = {
  genre: 'all',
  market: 'all',
  level: 'all',
  priority: 'all',
  changed: 'all',
  query: '',
  sortKey: 'priority',
  sortDir: 'asc',
};

let DATA = null;
let EVENTS = null;
let HISTORY = null;
let CANDIDATES = null;

/* -------------------------------------------------------------------------
   DOM / 时间 helpers
   ------------------------------------------------------------------------- */

function $(id) { return document.getElementById(id); }

function el(tag, props, children) {
  const node = document.createElement(tag);
  const settings = props || {};
  for (const key in settings) {
    const value = settings[key];
    if (value === null || value === undefined) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value);
  }
  for (const child of children || []) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function parseMoment(iso) {
  const time = Date.parse(iso);
  return Number.isNaN(time) ? NaN : time;
}

function pad(value) { return String(value).padStart(2, '0'); }

function formatStamp(iso) {
  const time = parseMoment(iso);
  if (Number.isNaN(time)) return '未知时间';
  const date = new Date(time);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatShort(iso) {
  const time = parseMoment(iso);
  if (Number.isNaN(time)) return '—';
  const date = new Date(time);
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function relativeTime(iso) {
  const time = parseMoment(iso);
  if (Number.isNaN(time)) return '未知';
  const seconds = (Date.now() - time) / 1000;
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

/* -------------------------------------------------------------------------
   派生数据
   ------------------------------------------------------------------------- */

function marketBucket(label) {
  if (/欧美|北美|欧洲|西方|EU|NA/.test(label)) return '欧美';
  if (/东南亚|新马|SEA/.test(label)) return '东南亚';
  return '其他英文向';
}

function marketsOf(game) {
  const buckets = new Set();
  for (const item of game.target_markets || []) buckets.add(marketBucket(item));
  return [...buckets];
}

function hasRecentChange(game) {
  const cutoff = Date.now() - CHANGED_WINDOW_MS;
  return (game.changes || []).some((change) => parseMoment(change.at) >= cutoff);
}

function matches(game) {
  if (state.genre !== 'all' && game.genre !== state.genre) return false;
  if (state.market !== 'all' && !marketsOf(game).includes(state.market)) return false;
  if (state.level !== 'all' && game.monetization_level !== state.level) return false;
  if (state.priority !== 'all' && String(game.priority) !== state.priority) return false;
  if (state.changed === 'today' && !hasRecentChange(game)) return false;

  const query = state.query.trim().toLowerCase();
  if (!query) return true;
  const haystack = [
    game.name_cn, game.name_en, game.developer, game.genre,
    (game.keywords || []).join(' '),
  ].join(' ').toLowerCase();
  return haystack.includes(query);
}

function sortValue(game) {
  if (state.sortKey === 'priority') return game.priority ?? 999;
  if (state.sortKey === 'monetization_level') return LEVEL_ORDER[game.monetization_level] ?? -1;
  return game[state.sortKey] || '';
}

function visibleGames() {
  return (DATA.games || []).filter(matches).sort((left, right) => {
    const a = sortValue(left);
    const b = sortValue(right);
    let result;
    if (typeof a === 'number' && typeof b === 'number') result = a - b;
    else result = String(a).localeCompare(String(b), 'zh');
    if (result === 0) result = (left.priority ?? 999) - (right.priority ?? 999);
    return state.sortDir === 'desc' ? -result : result;
  });
}

/* -------------------------------------------------------------------------
   表头 / 筛选 chrome
   ------------------------------------------------------------------------- */

function chip(label, active, onClick, count) {
  const children = [label];
  if (count !== undefined) children.push(el('span', { class: 'chip-count', text: count }));
  return el('button', {
    class: 'chip',
    type: 'button',
    'aria-pressed': String(active),
    onClick,
  }, children);
}

function renderChips(container, options, current, onPick) {
  container.replaceChildren();
  for (const option of options) {
    container.append(chip(option.label, current === option.value, () => onPick(option.value), option.count));
  }
}

function countBy(predicate) {
  let total = 0;
  for (const game of DATA.games || []) if (predicate(game)) total += 1;
  return total;
}

function renderFilters() {
  const games = DATA.games || [];
  const withAll = (options) => [{ label: '全部', value: 'all' }].concat(options);

  const genreOptions = withAll([...new Set(games.map((g) => g.genre))]
    .sort((a, b) => a.localeCompare(b, 'zh'))
    .map((genre) => ({ label: genre, value: genre, count: countBy((g) => g.genre === genre) })));

  const marketOptions = withAll(['欧美', '东南亚', '其他英文向']
    .map((bucket) => ({ label: bucket, value: bucket, count: countBy((g) => marketsOf(g).includes(bucket)) })));

  const levelOptions = withAll(LEVEL_VALUES
    .filter((level) => countBy((g) => g.monetization_level === level) > 0)
    .map((level) => ({ label: level, value: level, count: countBy((g) => g.monetization_level === level) })));

  const priorityOptions = withAll([...new Set(games.map((g) => g.priority))]
    .sort((a, b) => a - b)
    .map((priority) => ({ label: 'P' + priority, value: String(priority), count: countBy((g) => g.priority === priority) })));

  const changedOptions = [
    { label: '全部', value: 'all' },
    { label: '今日有变更', value: 'today', count: countBy(hasRecentChange) },
  ];

  const pick = (setter) => (value) => { setter(value); renderFilters(); render(); };

  renderChips($('filter-genre'), genreOptions, state.genre, pick((v) => { state.genre = v; }));
  renderChips($('filter-market'), marketOptions, state.market, pick((v) => { state.market = v; }));
  renderChips($('filter-level'), levelOptions, state.level, pick((v) => { state.level = v; }));
  renderChips($('filter-priority'), priorityOptions, state.priority, pick((v) => { state.priority = v; }));
  renderChips($('filter-changed'), changedOptions, state.changed, pick((v) => { state.changed = v; }));

  // 排序按钮状态
  document.querySelectorAll('.th-btn').forEach((button) => {
    const key = button.dataset.sortKey;
    if (key === state.sortKey) button.setAttribute('data-dir', state.sortDir);
    else button.removeAttribute('data-dir');
  });
}

/* -------------------------------------------------------------------------
   单元格渲染
   ------------------------------------------------------------------------- */

function cell(className, label, children) {
  return el('td', { class: className, 'data-label': label }, children);
}

function tagList(items) {
  return el('div', { class: 'tag-row' }, (items || []).map((item) => el('span', { class: 'tag', text: item })));
}

function bulletList(items) {
  return el('ul', { class: 'cell-list' }, (items || []).map((item) => el('li', {}, [item])));
}

function autoValue(game, key) {
  const steam = (game.watch || {}).steam_appid;
  const official = (game.watch || {}).official;

  if (key === 'steam') {
    if (!steam) return ['未配置 appid，未巡检', null];
    if (game.steam_listed === true) {
      const date = game.steam_release_date ? ' · ' + game.steam_release_date : '';
      return [el('span', {}, ['已上架', el('span', { class: 'auto-key' }, [date])]), null];
    }
    if (game.steam_listed === false) return [el('span', { class: 'auto-bad' }, ['美区未上架 · 信息差仍在']), null];
    return ['本次未取得结论', null];
  }
  if (key === 'official') {
    if (!official) return ['未配置官网，未巡检', null];
    const signal = game.official_signal;
    if (signal && signal.matched) {
      return [signal.label || '命中信号', signal.url ? el('a', { href: signal.url, target: '_blank', rel: 'noopener noreferrer' }, ['原文']) : null];
    }
    return ['未发现信号', null];
  }
  return [game.last_checked ? formatStamp(game.last_checked) : '未巡检', null];
}

function buildAutoBlock(game) {
  const list = el('ul', { class: 'auto-list' });
  const rows = [
    ['Steam', 'steam'],
    ['官网', 'official'],
    ['巡检', 'last_checked'],
  ];
  for (const [key, kind] of rows) {
    const [main, extra] = autoValue(game, kind);
    const li = el('li', {}, [el('span', { class: 'auto-key', text: key })]);
    li.append(typeof main === 'string' ? document.createTextNode(main) : main);
    if (extra) li.append(document.createTextNode(' '), extra);
    list.append(li);
  }
  return el('div', { class: 'auto-block' }, [
    el('div', { class: 'auto-head', text: '自动校验' }),
    list,
  ]);
}

function formatDay(value) {
  const text = String(value || '');
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!parts) return text || '—';
  return Number(parts[1]) === new Date().getFullYear() ? `${parts[2]}-${parts[3]}` : text;
}

function heatBlock(game) {
  if (!game.overseas_heat) return null;
  const head = el('div', { class: 'heat-head' }, [
    el('span', { class: 'heat-key', text: '海外热度' }),
    game.overseas_heat_checked
      ? el('span', { class: 'heat-checked', text: `复核于 ${formatDay(game.overseas_heat_checked)}` })
      : null,
  ]);
  return el('div', { class: 'heat-block' }, [
    head,
    el('div', { class: 'heat-text', text: game.overseas_heat }),
  ]);
}

/* 热度趋势：行内迷你折线。数据来自 web/data/history.json（collector/trends.py 每日追加）。
   取值优先级 A→B→C（Google Trends → Steam 评测数 → 自建信号活跃度），图中标注当前生效的来源；
   不同来源量纲不同，切换来源处曲线会有台阶，这是逐级兜底的固有特性。 */
const TREND_DAYS = 30;
const TREND_SOURCE_LABEL = {
  trends: 'A·Trends 热度',
  reviews: 'B·Steam 评测数',
  signal: 'C·信号活跃度',
};

function trendPoints(game) {
  const all = ((HISTORY && HISTORY.series) || {})[game.id] || [];
  return all.slice(-TREND_DAYS);
}

function sparkline(points) {
  const ns = 'http://www.w3.org/2000/svg';
  const width = 132, height = 26, pad = 3;
  const values = points.map((point) => Number(point.value) || 0);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const stepX = points.length > 1 ? (width - pad * 2) / (points.length - 1) : 0;
  const coords = values.map((value, index) => {
    const ratio = span === 0 ? 0.5 : (value - min) / span;
    return [pad + stepX * index, height - pad - ratio * (height - pad * 2)];
  });

  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', 'sparkline');
  svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('role', 'img');

  const line = document.createElementNS(ns, 'polyline');
  line.setAttribute('points', coords.map(([x, y]) => x.toFixed(1) + ',' + y.toFixed(1)).join(' '));
  svg.append(line);

  const last = coords[coords.length - 1];
  const dot = document.createElementNS(ns, 'circle');
  dot.setAttribute('cx', last[0].toFixed(1));
  dot.setAttribute('cy', last[1].toFixed(1));
  dot.setAttribute('r', '2');
  svg.append(dot);
  return svg;
}

function trendBlock(game) {
  const points = trendPoints(game);
  const latest = points.length ? points[points.length - 1] : null;
  const head = el('div', { class: 'heat-head' }, [
    el('span', { class: 'heat-key trend-key', text: '热度趋势' }),
    latest ? el('span', {
      class: 'trend-source trend-src-' + (latest.source || 'signal'),
      text: TREND_SOURCE_LABEL[latest.source] || latest.source,
    }) : null,
  ]);

  if (points.length < 2) {
    const note = points.length
      ? '数据积累中（已有 ' + points.length + ' 天，至少 2 天才画线）'
      : '暂无数据';
    return el('div', { class: 'heat-block trend-block' }, [
      head,
      el('div', { class: 'trend-pending', text: note }),
    ]);
  }

  const delta = Number(latest.value) - Number(points[0].value);
  const body = el('div', { class: 'trend-body' }, [
    sparkline(points),
    el('span', { class: 'trend-delta', text: (delta > 0 ? '+' : '') + delta }),
  ]);
  body.title = '近 ' + points.length + ' 天：' + points.map((point) => point.date + ' ' + point.value).join(' · ');

  return el('div', { class: 'heat-block trend-block' }, [head, body]);
}

function buildRow(game) {
  const changed = hasRecentChange(game);
  const level = game.monetization_level || '';
  const nameCell = el('div', {}, [
    el('div', { class: 'game-name', text: game.name_cn }),
    el('div', { class: 'game-name-en', text: game.name_en }),
    changed ? el('span', { class: 'tag-changed', text: '今日变更' }) : null,
  ]);

  const row = el('tr', { class: changed ? 'row-changed' : '', id: 'row-' + game.id }, [
    cell('col-first col-name', '游戏', [nameCell]),
    cell('col-developer', '厂商', [game.developer]),
    cell('col-genre', '类型', [el('span', { class: 'tag', text: game.genre })]),
    cell('col-priority', '优先级', [el('span', { class: 'prio', text: 'P' + game.priority })]),
    cell('col-cn_status', '国内状态', [game.cn_status]),
    cell('col-global_status', '海外状态', [
      el('div', {}, [game.global_status]),
      trendBlock(game),
      heatBlock(game),
      buildAutoBlock(game),
    ]),
    cell('col-gap_window', '信息差窗口', [game.gap_window]),
    cell('col-markets', '目标市场', [tagList(game.target_markets)]),
    cell('col-keywords', '潜在英文关键词', [bulletList(game.keywords)]),
    cell('col-opportunities', '内容机会', [bulletList(game.content_opportunities)]),
    cell('col-monetization', '变现可能性', [
      el('span', { class: 'lv ' + (LEVEL_CLASS[level] || 'lv-low'), text: level || '—' }),
      el('div', { class: 'cell-sub', text: game.monetization }),
    ]),
    cell('col-risks', '风险点', [bulletList(game.risks)]),
  ]);
  return row;
}

/* -------------------------------------------------------------------------
   变更历史
   ------------------------------------------------------------------------- */

function formatChangeValue(field, value) {
  if (value === null || value === undefined || value === '') return '—';
  if (field === 'steam_listed') return value ? '已上架' : '未上架';
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value);
}

function allChanges() {
  const records = [];
  for (const game of DATA.games || []) {
    for (const change of game.changes || []) {
      records.push(Object.assign({ game_name: game.name_cn, game_id: game.id }, change));
    }
  }
  records.sort((a, b) => (parseMoment(b.at) || 0) - (parseMoment(a.at) || 0));
  return records;
}

function renderChangesPanel() {
  const records = allChanges();
  $('changes-count').textContent = records.length ? `${records.length} 条记录` : '暂无记录';
  const body = $('changes-body');
  if (!records.length) {
    body.replaceChildren(el('p', { class: 'cell-sub', text: '还没有检测到任何字段变化。' }));
    return;
  }
  body.replaceChildren(...records.map((record) => {
    const field = FIELD_LABELS[record.field] || record.field;
    const diff = el('span', { class: 'change-diff' }, [
      el('span', { class: 'change-old', text: formatChangeValue(record.field, record.old) }),
      document.createTextNode(' → '),
      el('span', { text: formatChangeValue(record.field, record.new) }),
    ]);
    const children = [
      el('span', { class: 'change-time', text: formatShort(record.at) }),
      el('span', { class: 'change-game', text: record.game_name }),
      el('span', { class: 'change-field', text: field }),
      diff,
    ];
    if (record.url) children.push(el('a', { href: record.url, target: '_blank', rel: 'noopener noreferrer' }, ['出处']));
    if (record.evidence) children.push(el('span', { class: 'change-evidence', text: record.evidence }));
    return el('div', { class: 'change-item' }, children);
  }));
}

/* -------------------------------------------------------------------------
   上线倒计时
   ------------------------------------------------------------------------- */

// 与今天零点相差的整天数：0=今天、1=明天、2=后天，负数=已经过去
function daysUntil(isoDate) {
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate || ''));
  if (!parts) return NaN;
  const target = new Date(Number(parts[1]), Number(parts[2]) - 1, Number(parts[3]));
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((target - today) / 86400000);
}

function upcomingLaunches() {
  return ((EVENTS && EVENTS.events) || [])
    .map((event) => Object.assign({}, event, { days: daysUntil(event.date) }))
    .filter((event) => !Number.isNaN(event.days) && event.days >= 0)
    .sort((a, b) => a.days - b.days);
}

function countdownParts(days) {
  if (days <= 0) return ['今天', '发售'];
  if (days === 1) return ['明天', '发售'];
  return [String(days), '天后'];
}

function renderCountdown() {
  const note = $('countdown-note');
  const list = $('countdown-list');
  const alert = $('launch-alert');

  if (!EVENTS) {
    note.textContent = '数据载入失败';
    alert.hidden = true;
    list.replaceChildren(el('p', { class: 'countdown-empty', text: '暂时读不到倒计时数据。' }));
    return;
  }

  const items = upcomingLaunches();
  const near = items.filter((event) => event.days <= NEAR_DAYS);

  note.textContent = items.length
    ? `已确认发售日 ${items.length} 款 · 上线前 ${NEAR_DAYS} 天进入倒计时`
    : '暂无尚未上线的游戏';

  if (near.length) {
    alert.hidden = false;
    alert.textContent = near.length === 1
      ? `${near[0].name_cn} ${countdownParts(near[0].days).join('')}`
      : `近 ${NEAR_DAYS} 天内有 ${near.length} 款即将上线`;
    alert.title = `上线前 ${NEAR_DAYS} 天的倒计时提醒`;
  } else {
    alert.hidden = true;
  }

  if (!items.length) {
    list.replaceChildren(el('p', {
      class: 'countdown-empty',
      text: '目前没有解析出具体发售日的游戏（"待定 / Q1 2027" 这类模糊档期不入表）。',
    }));
    return;
  }

  list.replaceChildren(...items.map((event) => {
    const parts = countdownParts(event.days);
    const origin = event.origin === 'history'
      ? `历史名单中的游戏${event.first_seen ? ' · 首次记录于 ' + formatDay(event.first_seen) : ''}`
      : (event.first_seen ? `首次记录于 ${formatDay(event.first_seen)}` : null);

    return el('div', { class: 'countdown-card' + (event.days <= NEAR_DAYS ? ' is-near' : '') }, [
      el('div', { class: 'countdown-days' }, [parts[0], el('small', { text: parts[1] })]),
      el('div', {}, [
        el('div', { class: 'countdown-name', text: event.name_cn || event.game_id }),
        event.name_en ? el('div', { class: 'countdown-name-en', text: event.name_en }) : null,
      ]),
      el('div', { class: 'countdown-meta' }, [
        el('span', { class: 'countdown-date', text: formatDay(event.date) }),
        event.store_url
          ? el('a', { href: event.store_url, target: '_blank', rel: 'noopener noreferrer' }, ['商店页'])
          : null,
      ]),
      origin ? el('div', { class: 'countdown-origin', text: origin }) : null,
    ]);
  }));
}

/* -------------------------------------------------------------------------
   新发现 · 国服 / 海外服不同步
   ------------------------------------------------------------------------- */

const GAP_META = {
  cn_first: { label: '国服先行', cls: 'gap-cn' },
};

function renderDiscover() {
  const panel = $('discover-panel');
  const list = $('discover-list');
  const note = $('discover-note');
  const items = (CANDIDATES && CANDIDATES.candidates) || [];

  if (!items.length) { panel.hidden = true; return; }
  panel.hidden = false;

  const batches = (CANDIDATES.batches || []).map((batch) => batch.title).join(' / ');
  note.textContent = `候选 ${items.length} 条 · ${CANDIDATES.generated_at || '—'}` +
    (batches ? ` · ${batches}` : '');

  list.replaceChildren(...items.slice(0, 12).map(renderCandidate));
}

function renderCandidate(candidate) {
  const meta = GAP_META[candidate.gap_type] || { label: candidate.gap_type || '待判定', cls: '' };
  const steam = candidate.steam || {};
  const nppa = candidate.nppa || {};

  const tags = [
    el('span', { class: 'd-tag d-origin', text: candidate.origin === 'import' ? '进口' : '国产' }),
    el('span', { class: 'd-tag ' + meta.cls, text: meta.label }),
  ];
  if (candidate.gap_days) {
    tags.push(el('span', { class: 'd-tag d-days', text: `国服早 ${candidate.gap_days} 天` }));
  }

  const usCell = steam.us_listed !== true
    ? '无页面 / 未上架'
    : (steam.us_release_parsed
      ? (steam.us_coming_soon ? `未发售 · 定档 ${formatDay(steam.us_release_parsed)}` : formatDay(steam.us_release_parsed))
      : '未定档');

  const rows = [
    ['版号批准', formatDay(nppa.approved_at)],
    ['审批文号', nppa.approval_no || '—'],
    ['Steam 美区', usCell],
    ['Steam 国区', steam.cn_listed === false ? '不可购买' : (steam.cn_release_parsed ? formatDay(steam.cn_release_parsed) : '可购买')],
    ['运营单位', nppa.operator || '—'],
  ];

  return el('article', { class: 'd-card' }, [
    el('div', { class: 'd-card-head' }, [
      el('div', {}, [
        el('h3', { class: 'd-title', text: candidate.name_cn }),
        candidate.name_en ? el('span', { class: 'd-en', text: candidate.name_en }) : null,
        candidate.note ? el('p', { class: 'd-note', text: candidate.note }) : null,
      ]),
      el('div', { class: 'd-tags' }, tags),
    ]),
    el('dl', { class: 'd-meta' }, rows.flatMap(([key, value]) => [
      el('dt', { text: key }),
      el('dd', { text: value }),
    ])),
    el('div', { class: 'd-links' }, [
      nppa.batch_url
        ? el('a', { href: nppa.batch_url, target: '_blank', rel: 'noopener noreferrer' }, ['版号公示'])
        : null,
      steam.store_url
        ? el('a', { href: steam.store_url, target: '_blank', rel: 'noopener noreferrer' }, ['Steam 商店'])
        : null,
    ]),
  ]);
}

/* -------------------------------------------------------------------------
   顶栏 / 概览
   ------------------------------------------------------------------------- */

function renderChrome() {
  const games = DATA.games || [];
  const changedCount = games.filter(hasRecentChange).length;

  $('updated-at').textContent = DATA.last_run_at
    ? `更新于 ${relativeTime(DATA.last_run_at)}`
    : '尚未巡检';

  const changedChip = $('changed-today');
  if (changedCount) {
    changedChip.hidden = false;
    changedChip.textContent = `今日变更 ${changedCount} 款`;
    changedChip.title = '最近 24 小时内被巡检改写过的字段行数';
  } else {
    changedChip.hidden = true;
  }

  $('intro-text').textContent =
    `共 ${games.length} 款国产游戏 · 定性复核截止 ${DATA.data_cutoff || '—'} · 自动巡检 ${relativeTime(DATA.last_run_at)} · 巡检每日一次`;

  const reports = DATA.reports || [];
  const ok = reports.filter((r) => r.status === 'ok' || r.status === 'partial').length;
  const failed = reports.filter((r) => r.status === 'failed').length;
  const skipped = reports.filter((r) => r.status === 'skipped').length;
  $('footer-meta').textContent =
    `本轮巡检：正常 ${ok} · 失败 ${failed} · 跳过 ${skipped} · 数据生成于 ${DATA.generated_at || '—'}${DATA.last_run_at ? ' ' + formatStamp(DATA.last_run_at) : ''}`;
}

/* -------------------------------------------------------------------------
   主渲染
   ------------------------------------------------------------------------- */

function render() {
  if (!DATA) return;
  const games = visibleGames();
  const body = $('table-body');
  body.replaceChildren(...games.map(buildRow));

  $('result-count').textContent = `显示 ${games.length} / ${(DATA.games || []).length} 款`;
  const empty = $('empty-state');
  if (games.length) {
    empty.hidden = true;
  } else {
    empty.hidden = false;
    empty.replaceChildren(el('div', {}, [
      el('p', { text: '没有符合条件的游戏。' }),
      el('button', { class: 'text-btn', type: 'button', onClick: resetFilters }, ['重置筛选']),
    ]));
  }
  renderChrome();
}

function resetFilters() {
  state.genre = 'all';
  state.market = 'all';
  state.level = 'all';
  state.priority = 'all';
  state.changed = 'all';
  state.query = '';
  $('search').value = '';
  renderFilters();
  render();
}

/* -------------------------------------------------------------------------
   主题 & 启动
   ------------------------------------------------------------------------- */

function initTheme() {
  const stored = localStorage.getItem('gameqker-theme');
  document.documentElement.dataset.theme = stored === 'light' ? 'light' : 'dark';
  $('theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('gameqker-theme', next);
  });
}

function bindSorting() {
  document.querySelectorAll('.th-btn').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.sortKey;
      if (state.sortKey === key) {
        state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sortKey = key;
        // 数值型：优先级越小越靠前；其余字符串默认升序
        state.sortDir = key === 'monetization_level' ? 'desc' : 'asc';
      }
      renderFilters();
      render();
    });
  });
}

async function boot() {
  initTheme();
  bindSorting();
  $('search').addEventListener('input', (event) => { state.query = event.target.value; render(); });
  $('reset').addEventListener('click', resetFilters);

  // 倒计时与热度趋势都只是附加面板，读不到也不影响主表
  try {
    const eventsResponse = await fetch('data/events.json', { cache: 'no-store' });
    if (eventsResponse.ok) EVENTS = await eventsResponse.json();
  } catch (error) {
    EVENTS = null;
  }

  try {
    const historyResponse = await fetch('data/history.json', { cache: 'no-store' });
    if (historyResponse.ok) HISTORY = await historyResponse.json();
  } catch (error) {
    HISTORY = null;
  }

  // 候选池：读不到就隐藏整个「新发现」区块
  try {
    const candidatesResponse = await fetch(`data/candidates.json?t=${Date.now()}`, { cache: 'no-store' });
    if (candidatesResponse.ok) CANDIDATES = await candidatesResponse.json();
  } catch (error) {
    CANDIDATES = null;
  }

  try {
    const response = await fetch('data/games.json', { cache: 'no-store' });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    DATA = await response.json();
  } catch (error) {
    $('intro-text').textContent = '数据载入失败：' + error.message;
    $('empty-state').hidden = false;
    $('empty-state').replaceChildren(el('p', {}, ['数据载入失败：', error.message]));
    renderCountdown();
    return;
  }

  renderFilters();
  render();
  renderChangesPanel();
  renderCountdown();
  renderDiscover();
}

document.addEventListener('DOMContentLoaded', boot);
