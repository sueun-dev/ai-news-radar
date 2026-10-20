'use strict';

// --- labels ---------------------------------------------------------------------------------------

const GROUPS = {
  official: '공식 발표', models: '새 모델·저장소', tools: '개발·제품 블로그', releases: '오픈소스 릴리스',
  media: '해외 매체·분석', korea: '한국 소식', research: '논문·연구', community: '커뮤니티',
  discovery: '다국어 뉴스 검색', policy: '정책·기관',
};
const TIME_LABELS = {
  published: '피드 공개 시각', feed_updated: '피드 수정 시각', index_published: '검색엔진 표기 시각',
  community_posted: '커뮤니티 게시 시각', batch_announcement: '논문 배치 공지 시각',
  repository_created: '저장소 생성 시각', sitemap_modified: '사이트맵 수정 시각',
};
const NOTES = {
  source_time_unknown: '정확한 원문 시각 없음', date_only_or_unavailable: '날짜만 있거나 시각 없음',
  older_than_initial_baseline: '기준선보다 오래된 글', source_clock_in_future: '원문 시계가 미래 · 통계 제외',
  edit_without_new_precise_timestamp: '편집 시각 없음 · 통계 제외', synthetic_excluded: '자체 시험 · 통계 제외',
  manual_record_time_not_push_receipt: '수동 기록 · 푸시 수신 시각 아님', published: '공개 시각 기준',
  feed_updated: '피드 수정 시각 기준',
  not_original_publication_time: '검색·커뮤니티·배치·사이트맵 시각 · 원문 공개 지연 통계 제외',
};
const NOTIFY_LABELS = {
  pending: '알림 대기', accepted: 'macOS 접수', denied: '알림 권한 꺼짐', error: '알림 오류',
  duplicate: '같은 URL · 중복 알림 생략', prerelease_silent: '프리릴리스 · 기록만',
  source_silent: '소스 알림 꺼짐 · 기록만', historical_silent: '기존 시각의 뒤늦은 발견 · 기록만', manual: '수동 기록',
};
const LIBRARY_PAGE = 50;
const STATE_REFRESH_MS = 3000;
const LIBRARY_STALE_MS = 30000;
const SEARCH_DEBOUNCE_MS = 350;
const BRIDGE_STALE_SECONDS = 45;
const AUTHORIZED = [2, 3]; // UNAuthorizationStatus .authorized / .provisional

// --- state ----------------------------------------------------------------------------------------

let currentState = null;
let testNotificationId = null;
let restoreSourceFocus = null;           // source id whose checkbox should regain focus after a re-render
const pendingSettings = new Map();        // source id -> requested value while the POST is in flight
const library = { offset: 0, total: 0, request: 0, loadedAt: 0, queryTimer: null };

// --- DOM helpers ----------------------------------------------------------------------------------

const $ = id => document.getElementById(id);
const fmt = t => t ? new Date(t * 1000).toLocaleString('ko-KR', { timeZone: 'America/Chicago', hour12: false }) : '—';
const seconds = v => v == null ? '측정 불가' : v.toFixed(1) + '초';

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

// A link opens only http(s) or same-origin targets; anything else renders as inert text.
function link(text, url) {
  const node = el('a', text);
  try {
    const target = new URL(url);
    if (['https:', 'http:'].includes(target.protocol) || target.origin === location.origin) node.href = target.href;
  } catch (_) { /* not a URL */ }
  node.target = '_blank';
  node.rel = 'noopener noreferrer';
  return node;
}

function postJSON(path, payload) {
  return fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
}

// --- dashboard state rendering --------------------------------------------------------------------

function render(state) {
  currentState = state;
  renderConnection(state);
  renderBridge(state);
  renderTestFeedback(state);
  renderEvents(state);
  renderSources(state);
  renderCoverageSummary(state);
  renderMetrics(state.metrics);
  renderX(state);
}

function isHealthy(source, now) {
  const allowed = Math.max(source.interval * 4, source.next_due - source.last_success + 30, 300);
  return source.status === 'ok' && now - source.last_success < allowed;
}

function renderConnection(state) {
  const healthy = state.sources.filter(s => isHealthy(s, state.now)).length;
  const lastChecked = Math.max(0, ...state.sources.map(s => s.last_checked || 0));
  $('connection').className = 'connection' + (healthy === state.sources.length ? '' : ' error');
  $('connection').textContent = '정상 소스 ' + healthy + '/' + state.sources.length + ' · 저장 ' +
    state.coverage.item_count.toLocaleString() + '건 · 최근 확인 ' + fmt(lastChecked) + ' · 시각: Chicago';
  $('updated').textContent = '화면 갱신 ' + fmt(state.now);
}

function renderBridge(state) {
  const bridge = state.meta.notification_bridge;
  const heartbeat = bridge ? Date.parse(bridge.heartbeat) / 1000 : 0;
  const authorized = Boolean(bridge) && AUTHORIZED.includes(bridge.authorization);
  if (!bridge || state.now - heartbeat > BRIDGE_STALE_SECONDS) {
    $('bridge').textContent = '알림 도우미가 연결되지 않았습니다. native 폴더의 AI News Radar 앱을 실행하세요.';
  } else if (authorized) {
    $('bridge').textContent = 'macOS 알림 권한 허용됨 · 집중 모드와 배너 설정은 별도입니다.';
  } else {
    $('bridge').textContent = 'macOS 알림 권한이 꺼져 있거나 아직 결정되지 않았습니다. 시스템 설정 → 알림 → AI News Radar에서 확인하세요.';
  }
  $('bridge').className = 'note' + (authorized ? '' : ' bad');
}

function renderTestFeedback(state) {
  if (!testNotificationId) return;
  const test = state.events.find(e => e.id === testNotificationId);
  if (!test) return;
  const messages = {
    accepted: 'macOS가 시험 알림 요청을 접수했습니다. 실제 배너 표시나 사용자 수신을 확인한 것은 아닙니다.',
    denied: '시험 알림이 거부됐습니다. 시스템 설정 → 알림 → AI News Radar에서 권한을 확인하세요.',
    error: '시험 알림 요청에 오류가 있습니다. ' + (test.notify_error || '다시 시도하세요.'),
  };
  $('notification-feedback').textContent = messages[test.notify_state] ||
    '시험 알림이 수집기에 등록됐습니다. macOS 접수 대기 중입니다.';
}

function eventKindLabel(e) {
  if (e.is_test) return '자체 시험';
  return { updated: '내용 변경', manual: '수동 기록', backfill: '지난 글 수집' }[e.kind] || '새 소식';
}

function renderEvents(state) {
  const showTests = $('show-test').checked;
  const showHistory = $('show-history').checked;
  const events = state.events.filter(e => (showTests || !e.is_test) && (showHistory || e.notify_state !== 'historical_silent'));
  const list = $('events');
  list.replaceChildren();
  if (!events.length) {
    const box = el('div', null, 'empty');
    box.append(el('strong', '새로운 소식을 기다리고 있습니다.'),
      el('p', '첫 확인에서 이미 있던 글은 기준선으로만 저장했습니다. 이후 새 글이나 실제 내용 변경을 발견하면 이곳에 표시됩니다. 시험 알림은 실제 속도 통계에 포함되지 않습니다.'));
    list.append(box);
  }
  for (const e of events) {
    const row = el('article', null, 'event');
    row.append(el('time', fmt(e.detected_at)));
    const body = el('div');
    body.append(el('span', e.source_id, 'tag'), el('span', eventKindLabel(e), 'tag'));
    const title = el('h3');
    title.append(link(e.title, e.url));
    body.append(title,
      el('p', (NOTES[e.measurement_note] || e.measurement_note) + ' · 지연 ' + seconds(e.delay_seconds)),
      el('p', '관측 구간 ' + seconds(e.observation_gap_seconds) + ' · ' + (NOTIFY_LABELS[e.notify_state] || e.notify_state) +
        (e.notify_enqueued_at ? ' ' + fmt(e.notify_enqueued_at) : '')));
    if (e.source_time) body.append(el('p', '원문 기재 시각 ' + fmt(e.source_time)));
    row.append(body);
    list.append(row);
  }
}

function sourceRow(s) {
  const row = el('article', null, 'source');
  const first = el('div');
  const name = el('h3');
  name.append(link(s.name, s.url));
  first.append(name, el('p', (GROUPS[s.group] || s.group) + ' · ' + s.region),
    el('p', '기본 확인 주기 ' + s.interval + '초 · 최근 응답 항목 ' + s.item_count));

  const times = el('div', null, 'times');
  times.append(el('p', '마지막 정상 ' + fmt(s.last_success)), el('p', '다음 확인 ' + fmt(s.next_due)),
    el('p', 'HTTP ' + (s.http_status || '—') + ' · 응답 ' + (s.fetch_ms == null ? '측정 없음' : s.fetch_ms + 'ms')));

  const status = el('div');
  const label = s.status === 'ok' ? '정상' : s.status === 'error' ? '확인 실패' : '첫 확인 중';
  status.append(el('span', label, 'state ' + (s.status === 'ok' ? 'ok' : 'bad')),
    el('p', s.error || ('캐시 ' + (s.cache_control || '지시 없음'))));
  if (s.cache_age) status.append(el('p', '응답 캐시 Age ' + s.cache_age + '초 · 공개 지연 측정값 아님'));
  const toggle = el('label', null, 'notify-toggle');
  const box = el('input');
  box.type = 'checkbox';
  box.dataset.source = s.id;
  box.checked = pendingSettings.has(s.id) ? pendingSettings.get(s.id) : s.notify;
  box.disabled = pendingSettings.has(s.id);
  box.setAttribute('aria-label', s.name + ' macOS 알림');
  toggle.append(box, document.createTextNode(' macOS 알림'));
  status.append(toggle);

  row.append(first, times, status);
  return row;
}

function renderSources(state) {
  // Re-rendering replaces the checkboxes; keep keyboard focus on the one the user was using.
  const focusedSource = document.activeElement?.dataset?.source;
  const focusWasLost = [document.body, document.documentElement].includes(document.activeElement);
  const focusCandidate = focusedSource || (focusWasLost ? restoreSourceFocus : null);

  const group = $('source-group').value;
  const query = $('source-query').value.trim().toLocaleLowerCase();
  const rows = state.sources.filter(s => (!group || s.group === group) &&
    (!query || (s.name + ' ' + s.url + ' ' + s.id).toLocaleLowerCase().includes(query)));
  const list = $('source-list');
  list.replaceChildren();
  $('source-count').textContent = rows.length + ' / ' + state.sources.length + '개 소스 표시';
  for (const s of rows) list.append(sourceRow(s));
  if (!rows.length) list.append(el('p', '조건에 맞는 소스가 없습니다. 검색어나 분류를 바꿔보세요.', 'empty'));

  if (focusCandidate && !$('sources').hidden) {
    const control = Array.from(list.querySelectorAll('input[data-source]')).find(x => x.dataset.source === focusCandidate);
    if (control && !control.disabled) {
      control.focus({ preventScroll: true });
      restoreSourceFocus = null;
    } else if (control) {
      restoreSourceFocus = focusCandidate; // still saving; try again on the next render
    }
  }
}

function renderCoverageSummary(state) {
  const counts = el('dl');
  for (const [key, value] of Object.entries(state.coverage.groups)) {
    counts.append(el('dt', GROUPS[key] || key), el('dd', value + '개 소스'));
  }
  $('coverage-summary').replaceChildren(counts);
}

function renderMetrics(m) {
  const published = m.by_basis.published;
  const updated = m.by_basis.feed_updated;
  $('metrics').textContent = m.precise_samples
    ? '공개 시각 기준 ' + published.count + '건 · 중앙값 ' + seconds(published.median) + ' · P95 ' + seconds(published.p95) +
      ' / 피드 수정 시각 기준 ' + updated.count + '건 · 중앙값 ' + seconds(updated.median) + ' · P95 ' + seconds(updated.p95)
    : '실전 지연 표본 0건 — 정확한 시각이 있는 신규 소식이 필요합니다.';
  $('observation-status').textContent = '위 통계 중 직전 정상 확인과의 간격이 5분을 초과한 표본 ' + m.long_observation_samples +
    '건. 설치 준비·오프라인·서버 대기 등 긴 관측 공백도 포함하므로, 상시 실행 중의 통상 속도로 단정하지 마세요. 각 감지 기록의 관측 구간을 함께 확인하세요.';
}

function renderX(state) {
  const saved = state.x_setup.accounts || [];
  const verified = saved.filter(a => a.verified).length;
  $('x-status').textContent = verified
    ? '설정 기록 스냅샷 · ' + (state.x_setup.verified_at || '') + ' · @' + state.x_setup.account + ' · 게시물 알림 ' + verified +
      '개 켜짐 확인. 이후 권한 변경은 자동 확인하지 않습니다. Chrome 푸시 권한은 직접 허용해야 합니다. X 설정 → 알림 → 환경설정 → 푸시 알림에서 켜고 브라우저 요청을 허용하세요. 휴대폰 설정과 실제 도착은 미확인입니다.'
    : '계정별 알림 설정은 아직 확인되지 않았습니다.';
  const list = $('x-accounts');
  list.replaceChildren();
  for (const account of state.x_accounts) {
    const item = el('li');
    item.append(link('@' + account, 'https://x.com/' + account));
    const record = saved.find(x => x.handle === account);
    item.append(document.createTextNode(record?.verified ? ' · 게시물 알림 켜짐 확인' : ' · 설정 확인 필요'));
    list.append(item);
  }
}

async function refresh() {
  try {
    const response = await fetch('/api/state');
    if (!response.ok) throw Error('HTTP ' + response.status);
    render(await response.json());
  } catch (e) {
    $('connection').className = 'connection error';
    $('connection').textContent = '수집기에 연결되지 않았습니다. 마지막 화면은 이전 기록입니다. ' + e.message;
  }
}

const rerender = () => currentState && render(currentState);

// --- library (all collected items) ----------------------------------------------------------------

function libraryRow(entry) {
  const row = el('article', null, 'event');
  row.append(el('time', '최초 수집 ' + fmt(entry.first_seen)));
  const body = el('div');
  body.append(el('span', GROUPS[entry.group] || entry.group, 'tag'), el('span', entry.source_name, 'tag'));
  const title = el('h3');
  title.append(link(entry.title, entry.url));
  body.append(title, el('p', entry.source_time
    ? (TIME_LABELS[entry.basis] || '소스 기재 시각') + ' ' + fmt(entry.source_time)
    : '정확한 원문 시각 없음 · 수집 시각은 발행 시각이 아닙니다.'));
  row.append(body);
  return row;
}

async function loadLibrary() {
  const request = ++library.request;   // only the newest request may update the list
  $('library-status').textContent = '수집글을 불러오는 중입니다…';
  try {
    const params = new URLSearchParams({ q: $('library-query').value.trim(), group: $('library-group').value,
      offset: String(library.offset), limit: String(LIBRARY_PAGE) });
    const response = await fetch('/api/library?' + params);
    if (!response.ok) throw Error('HTTP ' + response.status);
    const data = await response.json();
    if (request !== library.request) return;
    library.total = data.total;
    library.loadedAt = Date.now();
    $('library-status').textContent = '같은 URL을 합친 ' + data.total.toLocaleString() + '건 중 ' + data.items.length + '건 표시 · 최근 수집순';
    $('library-page').textContent = data.total ? (library.offset + 1) + '–' + Math.min(library.offset + LIBRARY_PAGE, data.total) : '0건';
    $('library-prev').disabled = library.offset === 0;
    $('library-next').disabled = library.offset + LIBRARY_PAGE >= data.total;
    const list = $('library-items');
    list.replaceChildren(...data.items.map(libraryRow));
    if (!data.items.length) list.append(el('p', '조건에 맞는 수집글이 없습니다. 검색어나 분류를 바꿔보세요.', 'empty'));
  } catch (error) {
    if (request === library.request) {
      $('library-status').textContent = '목록을 갱신하지 못했습니다. 마지막 목록은 이전 기록입니다. 다시 시도하세요. ' + error.message;
    }
  }
}

function reloadLibraryFromStart() {
  library.offset = 0;
  loadLibrary();
}

// --- coverage --------------------------------------------------------------------------------------

async function loadCoverage() {
  try {
    const response = await fetch('/api/coverage');
    if (!response.ok) throw Error('HTTP ' + response.status);
    const data = await response.json();
    const gaps = $('coverage-gaps');
    gaps.replaceChildren();
    for (const note of data.notes || []) gaps.append(el('p', note, 'note'));
    for (const gap of data.excluded || []) {
      const row = el('article', null, 'gap');
      const heading = el('h3');
      heading.append(link(gap.name, gap.url));
      row.append(heading, el('p', gap.reason));
      gaps.append(row);
    }
  } catch (error) {
    $('coverage-gaps').textContent = '범위 기록을 불러오지 못했습니다. 다시 시도하세요. ' + error.message;
  }
}

// --- actions -----------------------------------------------------------------------------------------

function showTab(button) {
  restoreSourceFocus = null;
  document.querySelectorAll('nav button').forEach(x => x.removeAttribute('aria-current'));
  button.setAttribute('aria-current', 'page');
  document.querySelectorAll('.panel').forEach(panel => { panel.hidden = panel.id !== button.dataset.tab; });
  if (button.dataset.tab === 'library') loadLibrary();
  if (button.dataset.tab === 'coverage') loadCoverage();
}

async function sendTestNotification() {
  const button = $('test');
  button.disabled = true;
  testNotificationId = null;
  $('notification-feedback').textContent = '시험 알림을 요청하는 중입니다…';
  try {
    const response = await postJSON('/api/test', {});
    const result = await response.json();
    if (!response.ok) throw Error(result.error);
    testNotificationId = result.id;
    $('notification-feedback').textContent = '시험 알림이 수집기에 등록됐습니다. macOS 접수 대기 중입니다.';
    $('show-test').checked = true;
    await refresh();
  } catch (e) {
    $('notification-feedback').textContent = '시험 알림을 요청하지 못했습니다. ' + e.message;
  } finally {
    button.disabled = false;
  }
}

async function recordXObservation(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  if (button.disabled) return;
  button.disabled = true;
  $('x-result').textContent = '저장 중입니다…';
  try {
    const response = await postJSON('/api/x-log', { url: $('x-url').value, published_at: $('x-time').value });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'HTTP ' + response.status);
    $('x-result').textContent = '본 시각을 저장했습니다. 자동 수신 지연 통계와 구분됩니다.';
    $('x-url').value = '';
    $('x-time').value = '';
    await refresh();
  } catch (error) {
    $('x-result').textContent = '저장하지 못했습니다. 입력은 유지했습니다. 연결을 확인하고 다시 시도하세요. ' + error.message;
  } finally {
    button.disabled = false;
  }
}

async function saveSourceNotification(event) {
  const box = event.target;
  if (!box.matches('input[data-source]')) return;
  const sid = box.dataset.source;
  if (document.activeElement === box) restoreSourceFocus = sid;
  pendingSettings.set(sid, box.checked);
  box.disabled = true;
  try {
    const response = await postJSON('/api/source-settings', { id: sid, notify: box.checked });
    if (!response.ok) throw Error('HTTP ' + response.status);
    $('source-feedback').textContent = '알림 설정을 저장했습니다. 수집은 계속됩니다.';
  } catch (error) {
    $('source-feedback').textContent = '알림 설정을 저장하지 못했습니다. 다시 시도하세요. ' + error.message;
  } finally {
    pendingSettings.delete(sid);
    await refresh();
  }
}

// --- wiring ------------------------------------------------------------------------------------------

document.querySelectorAll('nav button').forEach(button => button.addEventListener('click', () => showTab(button)));
document.querySelectorAll('.group-select').forEach(select => {
  for (const [value, label] of Object.entries(GROUPS)) {
    const option = el('option', label);
    option.value = value;
    select.append(option);
  }
});
$('show-test').addEventListener('change', rerender);
$('show-history').addEventListener('change', rerender);
$('test').addEventListener('click', sendTestNotification);
$('x-form').addEventListener('submit', recordXObservation);
$('source-group').addEventListener('change', rerender);
$('source-query').addEventListener('input', rerender);
$('source-list').addEventListener('change', saveSourceNotification);
$('library-group').addEventListener('change', reloadLibraryFromStart);
$('library-query').addEventListener('input', () => {
  clearTimeout(library.queryTimer);
  library.queryTimer = setTimeout(reloadLibraryFromStart, SEARCH_DEBOUNCE_MS);
});
$('library-refresh').addEventListener('click', () => loadLibrary());
$('library-prev').addEventListener('click', () => {
  library.offset = Math.max(0, library.offset - LIBRARY_PAGE);
  loadLibrary();
});
$('library-next').addEventListener('click', () => {
  if (library.offset + LIBRARY_PAGE < library.total) {
    library.offset += LIBRARY_PAGE;
    loadLibrary();
  }
});

refresh();
loadLibrary();
setInterval(refresh, STATE_REFRESH_MS);
setInterval(() => {
  if (!$('library').hidden && Date.now() - library.loadedAt > LIBRARY_STALE_MS) loadLibrary();
}, 5000);
