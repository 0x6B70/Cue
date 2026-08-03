/**
 * Search + queue UI.
 *
 * State is never computed locally: the backend pushes the whole AppState over
 * the WebSocket and this renders it. Actions are fire-and-forget POSTs whose
 * effects come back through the same channel, so the two windows can never
 * disagree about what is playing.
 */

(() => {
  'use strict';

  const TOKEN = window.__APP_TOKEN__ || '';
  const $ = (id) => document.getElementById(id);
  const { t, tMessage, setLocale, applyStatic } = window.i18n;

  const el = {
    form: $('search-form'),
    input: $('search-input'),
    searchButton: $('search-button'),
    results: $('results'),
    resultsStatus: $('results-status'),
    queue: $('queue'),
    queueEmpty: $('queue-empty'),
    queueCount: $('queue-count'),
    connection: $('connection'),
    notice: $('notice'),
    noticeText: $('notice-text'),
    noticeDismiss: $('notice-dismiss'),
    npTitle: $('np-title'),
    npChannel: $('np-channel'),
    npPosition: $('np-position'),
    npDuration: $('np-duration'),
    seek: $('seek'),
    volume: $('volume'),
    toggle: $('btn-toggle'),
    next: $('btn-next'),
    previous: $('btn-previous'),
    clear: $('btn-clear'),
    clearPlayed: $('btn-clear-played'),
    openWindow: $('btn-open-window'),
    language: $('language'),
    gap: $('gap'),
    settingsButton: $('btn-settings'),
    settingsDialog: $('settings-dialog'),
    settingsFields: $('settings-fields'),
    diagnosticsBody: $('diagnostics-body')
  };

  let lastState = null;
  let seeking = false;
  let adjustingVolume = false;
  let dismissedNotice = null;

  // ------------------------------------------------------------------ utils

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', 'X-App-Token': TOKEN, ...(options.headers || {}) }
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        detail = (await response.json()).detail || detail;
      } catch { /* body was not JSON */ }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  function post(path, body) {
    return api(path, { method: 'POST', body: JSON.stringify(body ?? {}) }).catch(showError);
  }

  function showError(error) {
    dismissedNotice = null;
    setNotice(String(error.message || error));
  }

  // Dismissal is tracked by `key` rather than the rendered text, so switching
  // language does not resurrect a notice the user already dismissed.
  let currentNoticeKey = null;

  function setNotice(text, key = text) {
    currentNoticeKey = key;
    if (!text || key === dismissedNotice) {
      el.notice.hidden = true;
      return;
    }
    el.noticeText.textContent = text;
    el.notice.hidden = false;
  }

  // ----------------------------------------------------------------- search

  async function runSearch(query) {
    el.searchButton.disabled = true;
    el.resultsStatus.hidden = false;
    el.resultsStatus.textContent = t('search.searching', { query });
    el.results.innerHTML = '';
    try {
      const results = await api(`/api/search?q=${encodeURIComponent(query)}&limit=20`);
      renderResults(results);
    } catch (error) {
      el.resultsStatus.textContent = t('search.failed', { message: error.message });
    } finally {
      el.searchButton.disabled = false;
    }
  }

  function renderResults(results) {
    el.results.innerHTML = '';
    if (!results.length) {
      el.resultsStatus.hidden = false;
      el.resultsStatus.textContent = t('search.none');
      return;
    }
    el.resultsStatus.hidden = true;

    const fragment = document.createDocumentFragment();
    for (const result of results) {
      const item = document.createElement('li');
      item.className = 'result';

      const thumb = document.createElement('img');
      thumb.src = result.thumbnail || '';
      thumb.alt = '';
      thumb.loading = 'lazy';

      const meta = document.createElement('div');
      meta.className = 'meta';
      const title = document.createElement('div');
      title.className = 'title';
      title.textContent = result.title;
      if (result.live) {
        const badge = document.createElement('span');
        badge.className = 'badge';
        badge.textContent = t('result.live');
        title.appendChild(badge);
      }
      const sub = document.createElement('div');
      sub.className = 'sub';
      sub.textContent = [result.channel, result.duration ? formatTime(result.duration) : null]
        .filter(Boolean)
        .join(' · ');
      meta.append(title, sub);

      const actions = document.createElement('div');
      actions.className = 'actions';
      actions.append(
        button(t('result.queue'), () => addToQueue(result, false)),
        button(t('result.playNow'), () => addToQueue(result, true), 'ghost')
      );

      item.append(thumb, meta, actions);
      fragment.appendChild(item);
    }
    el.results.appendChild(fragment);
  }

  function button(label, onClick, className = '') {
    const node = document.createElement('button');
    node.type = 'button';
    node.textContent = label;
    if (className) node.className = className;
    node.addEventListener('click', onClick);
    return node;
  }

  function addToQueue(result, playNow) {
    return post('/api/queue', {
      video_id: result.id,
      title: result.title,
      channel: result.channel,
      duration: result.duration,
      thumbnail: result.thumbnail,
      play_now: playNow
    });
  }

  // ------------------------------------------------------------------ queue

  function renderQueue(state) {
    el.queueCount.textContent = String(state.items.length);
    el.queueEmpty.hidden = state.items.length > 0;
    el.queue.innerHTML = '';

    const fragment = document.createDocumentFragment();
    state.items.forEach((item, index) => {
      const node = document.createElement('li');
      node.className = `queue-item ${item.state}`;
      if (index === state.current) node.classList.add('current');
      // Prefetch resolved this one ahead of time and it failed. It is still
      // PENDING and will be retried, but flag it now rather than letting the
      // queue stall on it later.
      const willFail = item.state === 'pending' && Boolean(item.error_code || item.error);
      if (willFail) node.classList.add('warn');
      if (item.state === 'resolved') node.classList.add('ready');
      node.draggable = true;
      node.dataset.index = String(index);

      const position = document.createElement('div');
      position.className = 'qi-index';
      position.textContent = index === state.current ? '▶' : String(index + 1);

      const meta = document.createElement('div');
      const title = document.createElement('div');
      title.className = 'qi-title';
      title.textContent = item.title;
      title.title = item.title;
      const sub = document.createElement('div');
      sub.className = 'qi-sub';
      const reason = tMessage(item.error_code, {}, item.error);
      sub.textContent = reason
        ? (willFail ? `${t('queue.willFail')} — ${reason}` : reason)
        : [item.channel, item.duration ? formatTime(item.duration) : null]
          .filter(Boolean)
          .join(' · ');
      meta.append(title, sub);

      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'qi-remove';
      remove.innerHTML = '&times;';
      remove.title = t('queue.remove');
      remove.addEventListener('click', (event) => {
        event.stopPropagation();
        api(`/api/queue/${index}`, { method: 'DELETE' }).catch(showError);
      });

      node.addEventListener('dblclick', () => post(`/api/queue/play/${index}`));
      node.append(position, meta, remove);
      fragment.appendChild(node);
    });
    el.queue.appendChild(fragment);
  }

  // Drag-to-reorder. Indices come from the rendered list, and the reorder is
  // applied server-side so the queue stays the single source of truth.
  let dragIndex = null;

  el.queue.addEventListener('dragstart', (event) => {
    const item = event.target.closest('.queue-item');
    if (!item) return;
    dragIndex = Number(item.dataset.index);
    item.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
  });

  el.queue.addEventListener('dragover', (event) => {
    event.preventDefault();
    const item = event.target.closest('.queue-item');
    for (const node of el.queue.children) node.classList.remove('drop-target');
    if (item) item.classList.add('drop-target');
  });

  el.queue.addEventListener('drop', (event) => {
    event.preventDefault();
    const item = event.target.closest('.queue-item');
    for (const node of el.queue.children) node.classList.remove('drop-target', 'dragging');
    if (!item || dragIndex === null) return;
    const target = Number(item.dataset.index);
    if (target !== dragIndex) post('/api/queue/reorder', { from: dragIndex, to: target });
    dragIndex = null;
  });

  el.queue.addEventListener('dragend', () => {
    for (const node of el.queue.children) node.classList.remove('drop-target', 'dragging');
    dragIndex = null;
  });

  // ------------------------------------------------------------ now playing

  function renderPlayer(state) {
    const current = state.current >= 0 ? state.items[state.current] : null;
    const player = state.player;

    el.npTitle.textContent = current ? current.title : t('player.nothing');
    el.npChannel.textContent = current ? (current.channel || '') : '';

    const duration = player.duration || (current && current.duration) || 0;
    el.npDuration.textContent = formatTime(duration);

    if (!seeking) {
      el.npPosition.textContent = formatTime(player.position);
      el.seek.value = duration > 0 ? String(Math.round((player.position / duration) * 1000)) : '0';
    }
    el.seek.disabled = !duration;

    if (!adjustingVolume) el.volume.value = String(player.volume);

    el.toggle.textContent = player.state === 'playing' ? '⏸' : '▶';
    el.toggle.title = player.state === 'playing' ? t('player.pause') : t('player.play');
    el.openWindow.hidden = player.window_open || player.state !== 'idle';
  }

  function render(state) {
    lastState = state;
    renderQueue(state);
    renderPlayer(state);
    // Backend notices arrive as a code plus params; the English text it also
    // sends is the fallback for a code this build does not know. A `*_code`
    // param carries a nested key (the resolver's reason), which has to be
    // translated too or the notice comes out half in English.
    const params = { ...(state.notice_params || {}) };
    for (const [name, value] of Object.entries(params)) {
      if (name.endsWith('_code') && value) params[name.slice(0, -5)] = t(value);
    }
    setNotice(
      tMessage(state.notice_code, params, state.notice),
      state.notice_code || state.notice
    );
  }

  // -------------------------------------------------------------- websocket

  let connectionKey = 'connection.connecting';

  function setConnection(status, key) {
    connectionKey = key;
    el.connection.className = `status ${status}`;
    el.connection.querySelector('.label').textContent = t(key);
  }

  let socket = null;
  let reconnectDelay = 500;

  function connect() {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(`${protocol}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);

    socket.addEventListener('open', () => {
      reconnectDelay = 500;
      setConnection('online', 'connection.connected');
    });

    socket.addEventListener('message', (event) => {
      try {
        render(JSON.parse(event.data));
      } catch (error) {
        console.warn('bad state payload', error);
      }
    });

    socket.addEventListener('close', () => {
      setConnection('offline', 'connection.reconnecting');
      // Back off so a backend that is down does not get hammered.
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    });

    socket.addEventListener('error', () => socket.close());
  }

  // ----------------------------------------------------------------- events

  el.form.addEventListener('submit', (event) => {
    event.preventDefault();
    const query = el.input.value.trim();
    if (query) runSearch(query);
  });

  el.toggle.addEventListener('click', () => post('/api/player/toggle'));
  el.next.addEventListener('click', () => post('/api/player/next'));
  el.previous.addEventListener('click', () => post('/api/player/previous'));
  el.clear.addEventListener('click', () => post('/api/queue/clear'));
  el.clearPlayed.addEventListener('click', () => post('/api/queue/clear-played'));
  el.openWindow.addEventListener('click', () => post('/api/player/open'));

  el.noticeDismiss.addEventListener('click', () => {
    dismissedNotice = currentNoticeKey;
    el.notice.hidden = true;
  });

  // --------------------------------------------------------------- settings

  // Declarative so a new backend setting is one entry here, not a control in
  // the HTML plus a wired-up listener. `label`/`help` are i18n keys. Advanced,
  // playback-breaking knobs (player_clients, potoken_clients, invidious) are
  // deliberately left to settings.json -- see the note in the dialog.
  const SETTINGS_FIELDS = [
    { key: 'show_upcoming_overlay', type: 'toggle', group: 'settings.group.overlay' },
    { key: 'gap_seconds', type: 'number', min: 0, max: 3600, step: 1, group: 'settings.group.playback' },
    { key: 'max_height', type: 'select', options: [360, 480, 720, 1080, 1440, 2160], group: 'settings.group.playback' },
    { key: 'clear_queue_on_exit', type: 'toggle', group: 'settings.group.playback' },
    { key: 'ytdlp_channel', type: 'select', options: ['stable', 'nightly'], group: 'settings.group.reliability' },
    { key: 'offline_cache', type: 'toggle', group: 'settings.group.reliability' },
    { key: 'cache_budget_gb', type: 'number', min: 0, max: 1000, step: 1, group: 'settings.group.reliability' },
    { key: 'cookies_from_browser', type: 'text', placeholder: 'firefox', group: 'settings.group.reliability' }
  ];

  /** Coerce a control's value to the type the backend expects for `field`. */
  function settingValue(field, control) {
    if (field.type === 'toggle') return control.checked;
    if (field.type === 'number') return Math.min(field.max, Math.max(field.min, Math.round(Number(control.value) || 0)));
    if (field.type === 'select' && typeof field.options[0] === 'number') return Number(control.value);
    if (field.type === 'text') return control.value.trim() || null; // empty means "unset"
    return control.value;
  }

  function saveSetting(key, value) {
    return api('/api/settings', { method: 'PUT', body: JSON.stringify({ [key]: value }) })
      .then(() => { if (key === 'gap_seconds') el.gap.value = String(value); })
      .catch(showError);
  }

  /** Build the dialog's controls from SETTINGS_FIELDS, filled from `saved`. */
  function renderSettings(saved) {
    el.settingsFields.textContent = '';
    let currentGroup = null;
    for (const field of SETTINGS_FIELDS) {
      if (field.group !== currentGroup) {
        currentGroup = field.group;
        const heading = document.createElement('h3');
        heading.textContent = t(field.group);
        el.settingsFields.appendChild(heading);
      }

      const row = document.createElement('label');
      row.className = 'settings-row';
      const caption = document.createElement('span');
      caption.className = 'settings-label';
      caption.textContent = t(`settings.${field.key}`);

      let control;
      if (field.type === 'select') {
        control = document.createElement('select');
        for (const opt of field.options) {
          const option = document.createElement('option');
          option.value = String(opt);
          option.textContent = String(opt);
          control.appendChild(option);
        }
        control.value = String(saved[field.key]);
      } else if (field.type === 'toggle') {
        control = document.createElement('input');
        control.type = 'checkbox';
        control.checked = Boolean(saved[field.key]);
      } else {
        control = document.createElement('input');
        control.type = field.type === 'number' ? 'number' : 'text';
        if (field.type === 'number') Object.assign(control, { min: field.min, max: field.max, step: field.step });
        if (field.placeholder) control.placeholder = field.placeholder;
        control.value = saved[field.key] == null ? '' : String(saved[field.key]);
      }

      control.addEventListener('change', () => saveSetting(field.key, settingValue(field, control)));
      row.append(caption, control);
      el.settingsFields.appendChild(row);
    }
  }

  function renderDiagnostics(diag) {
    el.diagnosticsBody.textContent = '';
    if (!diag) return;
    // A curated, human-readable subset of /api/diagnostics — the same fields
    // docs/reliability.md says to check first when playback stops.
    const potoken = diag.potoken?.have_token ? t('settings.diag.held') : t('settings.diag.none');
    const rows = [
      ['settings.diag.ytdlp', diag.ytdlp_version || '—'],
      ['settings.diag.jsRuntime', diag.js_runtime || t('settings.diag.missing')],
      ['settings.diag.lastTier', diag.last_successful_tier || '—'],
      ['settings.diag.potoken', potoken]
    ];
    for (const [key, value] of rows) {
      const dt = document.createElement('dt');
      dt.textContent = t(key);
      const dd = document.createElement('dd');
      dd.textContent = String(value);
      el.diagnosticsBody.append(dt, dd);
    }
    // Tier health, when anything has been tried, as "tier: status" lines.
    for (const [tier, status] of Object.entries(diag.tier_health || {})) {
      const dt = document.createElement('dt');
      dt.textContent = tier;
      const dd = document.createElement('dd');
      dd.textContent = String(status);
      el.diagnosticsBody.append(dt, dd);
    }
  }

  async function openSettings() {
    try {
      const [saved, diag] = await Promise.all([
        api('/api/settings'),
        api('/api/diagnostics').catch(() => null) // diagnostics are best-effort
      ]);
      renderSettings(saved || {});
      renderDiagnostics(diag);
    } catch (error) {
      showError(error);
      return;
    }
    el.settingsDialog.showModal();
  }

  el.settingsButton.addEventListener('click', openSettings);

  // ----------------------------------------------------------------- locale

  /** Re-render everything that holds already-translated text. */
  function applyLocale(next) {
    setLocale(next);
    applyStatic();
    el.language.value = window.i18n.getLocale();
    setConnection(el.connection.className.replace('status', '').trim(), connectionKey);
    if (lastState) render(lastState);
  }

  el.language.addEventListener('change', () => {
    applyLocale(el.language.value);
    // Persisted server-side through the existing settings endpoint, so the
    // choice survives a restart and both windows agree on it.
    api('/api/settings', { method: 'PUT', body: JSON.stringify({ locale: el.language.value }) })
      .catch(showError);
  });

  // `change` rather than `input`, so typing "10" does not PUT 1 then 10.
  el.gap.addEventListener('change', () => {
    // A typed field can hold anything: empty, negative, 1e9, "abc". Settle on
    // a sane number here so the backend is never asked to sleep on garbage.
    const seconds = Math.min(3600, Math.max(0, Math.round(Number(el.gap.value) || 0)));
    el.gap.value = String(seconds);
    api('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ gap_seconds: seconds })
    }).catch(showError);
  });

  el.seek.addEventListener('input', () => {
    seeking = true;
    const duration = (lastState?.player.duration) || 0;
    el.npPosition.textContent = formatTime((Number(el.seek.value) / 1000) * duration);
  });

  el.seek.addEventListener('change', () => {
    const duration = (lastState?.player.duration) || 0;
    seeking = false;
    if (duration > 0) {
      post('/api/player/seek', { seconds: (Number(el.seek.value) / 1000) * duration, absolute: true });
    }
  });

  el.volume.addEventListener('input', () => { adjustingVolume = true; });
  el.volume.addEventListener('change', () => {
    adjustingVolume = false;
    post('/api/player/volume', { level: Number(el.volume.value) });
  });

  // Space toggles playback unless the user is typing.
  document.addEventListener('keydown', (event) => {
    if (event.code !== 'Space' || event.target.matches('input, textarea, button')) return;
    event.preventDefault();
    post('/api/player/toggle');
  });

  applyLocale('en');
  setConnection('', 'connection.connecting');
  connect();
  el.input.focus();

  // Adopt the saved locale once settings arrive. Starting in English and
  // switching is better than blocking first paint on a round-trip.
  api('/api/settings')
    .then((saved) => {
      if (!saved) return;
      if (saved.locale && saved.locale !== window.i18n.getLocale()) applyLocale(saved.locale);
      if (saved.gap_seconds != null) el.gap.value = String(Number(saved.gap_seconds));
    })
    .catch(() => { /* defaults are fine */ });
})();
