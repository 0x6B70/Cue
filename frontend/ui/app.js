/**
 * Search + queue UI.
 *
 * State is never computed locally: the backend pushes the whole AppState over
 * the WebSocket and this renders it. Actions are fire-and-forget POSTs whose
 * effects come back through the same channel, so the two windows can never
 * disagree about what is playing.
 *
 * The library tabs (favourites, history, downloads, stats) are fetched on
 * demand instead -- they change rarely and can be large, so they stay out of
 * the broadcast. The broadcast carries only the favourite and download ids,
 * which is enough to keep every star and "on disk" badge current.
 */

(() => {
  'use strict';

  const TOKEN = window.__APP_TOKEN__ || '';
  const $ = (id) => document.getElementById(id);
  const { t, tMessage, setLocale, applyStatic } = window.i18n;

  const el = {
    form: $('search-form'),
    input: $('search-input'),
    ghost: $('search-ghost'),
    suggestions: $('suggestions'),
    searchButton: $('search-button'),
    singer: $('singer-input'),
    results: $('results'),
    resultsStatus: $('results-status'),
    libraryMatches: $('library-matches'),
    libraryResults: $('library-results'),
    queue: $('queue'),
    queueEmpty: $('queue-empty'),
    queueCount: $('queue-count'),
    connection: $('connection'),
    notice: $('notice'),
    noticeText: $('notice-text'),
    noticeDismiss: $('notice-dismiss'),
    npTitle: $('np-title'),
    npStar: $('np-star'),
    npSave: $('np-save'),
    savedDir: $('saved-dir'),
    savedFfmpeg: $('saved-ffmpeg'),
    openFolder: $('btn-open-folder'),
    rescan: $('btn-rescan'),
    localFolders: $('local-folders'),
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
    diagnosticsBody: $('diagnostics-body'),
    stats: $('stats'),
    downloadsSummary: $('downloads-summary')
  };

  let lastState = null;
  let seeking = false;
  let adjustingVolume = false;
  let dismissedNotice = null;
  let favourites = new Set();
  let downloads = new Set();
  let saved = new Set();
  let saving = {};

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

  /** "3h 12m" for listening totals; seconds would be noise at that scale. */
  function formatDuration(seconds) {
    const minutes = Math.round((seconds || 0) / 60);
    if (minutes < 60) return t('stats.minutes', { n: minutes });
    return t('stats.hours', { h: Math.floor(minutes / 60), m: minutes % 60 });
  }

  function formatSize(bytes) {
    if (!bytes) return '0 MB';
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    return `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`;
  }

  function formatWhen(epochSeconds) {
    if (!epochSeconds) return '';
    const date = new Date(epochSeconds * 1000);
    const locale = window.i18n.getLocale();
    const sameDay = date.toDateString() === new Date().toDateString();
    return sameDay
      ? date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })
      : date.toLocaleDateString(locale, { day: 'numeric', month: 'short' });
  }

  /** Case- and accent-insensitive form, matching backend/library.py's fold(). */
  function fold(text) {
    return String(text || '').normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
  }

  const store = {
    get(key) {
      try { return localStorage.getItem(key); } catch { return null; }
    },
    set(key, value) {
      try { localStorage.setItem(key, value); } catch { /* private mode: fine */ }
    }
  };

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

  // ----------------------------------------------------------------- tracks

  function button(label, onClick, className = '') {
    const node = document.createElement('button');
    node.type = 'button';
    node.textContent = label;
    if (className) node.className = className;
    node.addEventListener('click', onClick);
    return node;
  }

  /** The star toggle. Kept in sync with the broadcast by syncMarks(). */
  function starButton(track) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'star';
    node.dataset.favId = track.id;
    paintStar(node, favourites.has(track.id));
    node.addEventListener('click', (event) => {
      event.stopPropagation();
      toggleFavourite(track);
    });
    return node;
  }

  function paintStar(node, on) {
    node.classList.toggle('on', on);
    node.textContent = on ? '★' : '☆';
    node.title = on ? t('fav.remove') : t('fav.add');
  }

  /** The "save to folder" button. Kept in sync with the broadcast by syncMarks(). */
  function saveButton(track) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'save-btn';
    node.dataset.saveId = track.id;
    paintSave(node);
    node.addEventListener('click', (event) => {
      event.stopPropagation();
      saveTrack(track);
    });
    return node;
  }

  function paintSave(node) {
    const id = node.dataset.saveId;
    const job = saving[id];
    node.classList.remove('done', 'busy', 'failed');
    if (saved.has(id) || job?.state === 'done') {
      node.classList.add('done');
      node.textContent = '✓';
      node.title = t('save.done');
    } else if (job && (job.state === 'saving' || job.state === 'queued')) {
      node.classList.add('busy');
      node.textContent = job.state === 'queued' ? '…' : `${Math.round((job.progress || 0) * 100)}%`;
      node.title = t('save.saving');
    } else if (job?.state === 'failed') {
      node.classList.add('failed');
      node.textContent = '⚠';
      node.title = t('save.failed', { error: job.error || '' });
    } else {
      node.textContent = '⬇';
      node.title = t('save.add');
    }
  }

  function saveTrack(track) {
    if (saved.has(track.id)) return;
    const meta = { title: track.title, channel: track.channel, duration: track.duration, thumbnail: track.thumbnail };
    saving[track.id] = { state: 'queued', progress: 0 };
    syncMarks();
    api(`/api/library/save/${encodeURIComponent(track.id)}`, { method: 'POST', body: JSON.stringify(meta) })
      .catch(showError);
  }

  function toggleFavourite(track) {
    const on = favourites.has(track.id);
    // Optimistic: the broadcast confirms within a quarter second anyway.
    if (on) favourites.delete(track.id); else favourites.add(track.id);
    syncMarks();
    const meta = { title: track.title, channel: track.channel, duration: track.duration, thumbnail: track.thumbnail };
    api(`/api/library/favourites/${encodeURIComponent(track.id)}`, {
      method: on ? 'DELETE' : 'PUT',
      body: on ? undefined : JSON.stringify(meta)
    })
      .then(() => { if (activeTab === 'favourites') loadTab('favourites'); })
      .catch(showError);
  }

  /**
   * One row for a song anywhere in the UI: search results, library matches and
   * every library tab. `extra` adds tab-specific text and buttons.
   */
  function trackRow(track, extra = {}) {
    const item = document.createElement('li');
    item.className = 'result';

    const local = isLocal(track);
    let thumb;
    if (local && !track.thumbnail) {
      // A file of the user's own: no YouTube thumbnail to show.
      thumb = document.createElement('div');
      thumb.className = 'thumb-local';
      thumb.textContent = /\.(mp3|m4a|flac|wav|ogg|opus|aac|wma)$/i.test(track.name || '') ? '♪' : '🎞';
    } else {
      thumb = document.createElement('img');
      thumb.src = track.thumbnail || `https://i.ytimg.com/vi/${track.id}/mqdefault.jpg`;
      thumb.alt = '';
      thumb.loading = 'lazy';
    }

    const meta = document.createElement('div');
    meta.className = 'meta';
    const title = document.createElement('div');
    title.className = 'title';
    title.textContent = track.title;
    if (track.live) {
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = t('result.live');
      title.appendChild(badge);
    }
    // Two different things, kept apart: "saved" is a file in the user's own
    // folder; "cache" is the app's temporary copy of a song that was queued.
    const disk = document.createElement('span');
    disk.dataset.diskId = track.id;
    paintDisk(disk);
    title.appendChild(disk);

    const sub = document.createElement('div');
    sub.className = 'sub';
    sub.textContent = [track.channel, track.duration ? formatTime(track.duration) : null, ...(extra.sub || [])]
      .filter(Boolean)
      .join(' · ');
    meta.append(title, sub);
    if (extra.stats?.length) {
      const stats = document.createElement('div');
      stats.className = 'stats-line';
      stats.textContent = extra.stats.filter(Boolean).join(' · ');
      meta.appendChild(stats);
    }

    const actions = document.createElement('div');
    actions.className = 'actions';
    actions.append(
      button(t('result.queue'), () => addToQueue(track, false)),
      button(t('result.playNow'), () => addToQueue(track, true), 'ghost'),
      ...(extra.actions || [])
    );

    // Already a file on disk: nothing to save.
    const save = local ? document.createElement('span') : saveButton(track);
    item.append(thumb, meta, save, starButton(track), actions);
    if (extra.details) {
      // Expandable panel under the row, filled on first open.
      const panel = document.createElement('div');
      panel.className = 'track-details';
      panel.hidden = true;
      const toggle = button('📊', async () => {
        panel.hidden = !panel.hidden;
        toggle.classList.toggle('active', !panel.hidden);
        if (!panel.hidden && !panel.childElementCount) {
          panel.textContent = t('stats.loading');
          try {
            panel.replaceChildren(await extra.details());
          } catch (error) {
            panel.textContent = String(error.message || error);
          }
        }
      }, 'ghost stats-toggle');
      toggle.title = t('stats.details');
      actions.appendChild(toggle);
      item.appendChild(panel);
    }
    return item;
  }

  /** A file from the library folders (its id says so, or the listing did). */
  function isLocal(track) {
    return Boolean(track.local) || String(track.id || '').startsWith('local-');
  }

  function renderTracks(list, tracks, extra = () => ({})) {
    list.innerHTML = '';
    const fragment = document.createDocumentFragment();
    for (const track of tracks) fragment.appendChild(trackRow(track, extra(track)));
    list.appendChild(fragment);
  }

  function paintDisk(node) {
    const id = node.dataset.diskId;
    if (saved.has(id)) {
      node.className = 'badge disk';
      node.textContent = t('result.saved');
      node.title = t('result.savedHelp');
      node.hidden = false;
    } else if (downloads.has(id)) {
      node.className = 'badge cached';
      node.textContent = t('result.cached');
      node.title = t('result.cachedHelp');
      node.hidden = false;
    } else {
      node.hidden = true;
    }
  }

  /** Repaint stars and "on disk" badges after the id sets change. */
  function syncMarks() {
    document.querySelectorAll('[data-fav-id]').forEach((node) => {
      paintStar(node, favourites.has(node.dataset.favId));
    });
    document.querySelectorAll('[data-disk-id]').forEach(paintDisk);
    document.querySelectorAll('[data-save-id]').forEach(paintSave);
  }

  function addToQueue(track, playNow) {
    const singer = el.singer.value.trim();
    return post('/api/queue', {
      video_id: track.id,
      title: track.title,
      channel: track.channel,
      duration: track.duration,
      thumbnail: track.thumbnail,
      singer: singer || null,
      play_now: playNow
    });
  }

  // ----------------------------------------------------------------- search

  let searchSeq = 0;

  async function runSearch(query) {
    const seq = ++searchSeq;
    closeSuggestions();
    selectTab('search');
    el.searchButton.disabled = true;
    el.resultsStatus.hidden = false;
    el.resultsStatus.textContent = t('search.searching', { query });
    el.results.innerHTML = '';

    // The library answers instantly; show it while YouTube is still thinking.
    api(`/api/library/search?q=${encodeURIComponent(query)}`)
      .then((matches) => {
        if (seq !== searchSeq) return;
        el.libraryMatches.hidden = !matches.length;
        renderTracks(el.libraryResults, matches.slice(0, 6), (track) => ({
          sub: track.plays ? [t('library.plays', { n: track.plays })] : []
        }));
      })
      .catch(() => { el.libraryMatches.hidden = true; });

    try {
      const results = await api(`/api/search?q=${encodeURIComponent(query)}&limit=20`);
      if (seq === searchSeq) renderResults(results);
    } catch (error) {
      if (seq === searchSeq) el.resultsStatus.textContent = t('search.failed', { message: error.message });
    } finally {
      if (seq === searchSeq) el.searchButton.disabled = false;
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
    renderTracks(el.results, results);
  }

  // ----------------------------------------------------------- autocomplete

  // Suggestions come from the user's own searches and plays first, then
  // YouTube's. The top one that extends what was typed is also drawn inline
  // as grey "ghost" text; Tab or → accepts it, like a browser's address bar.
  let suggestionItems = [];
  let highlighted = -1;
  let suggestTimer = null;
  let suggestAbort = null;
  let typedValue = '';

  function closeSuggestions() {
    // Drop anything still in flight too, or an answer landing just after a
    // search was submitted would pop the list back open over the results.
    clearTimeout(suggestTimer);
    if (suggestAbort) suggestAbort.abort();
    suggestAbort = null;
    el.suggestions.hidden = true;
    el.input.setAttribute('aria-expanded', 'false');
    suggestionItems = [];
    highlighted = -1;
    setGhost('');
  }

  function setGhost(completion) {
    const value = el.input.value;
    const show = completion && value && fold(completion).startsWith(fold(value)) && completion.length > value.length;
    el.ghost.querySelector('.typed').textContent = show ? value : '';
    el.ghost.querySelector('.rest').textContent = show ? completion.slice(value.length) : '';
    el.ghost.dataset.completion = show ? completion : '';
  }

  function renderSuggestions(items) {
    suggestionItems = items;
    highlighted = -1;
    el.suggestions.innerHTML = '';
    if (!items.length || document.activeElement !== el.input) {
      closeSuggestions();
      return;
    }
    items.forEach((item, index) => {
      const li = document.createElement('li');
      li.className = `suggestion ${item.source}`;
      li.setAttribute('role', 'option');
      li.dataset.index = String(index);
      const icon = document.createElement('span');
      icon.className = 'icon';
      icon.textContent = { history: '🕘', file: '📁' }[item.source] || '🔍';
      icon.title = t(`suggest.${item.source}`);
      const text = document.createElement('span');
      text.className = 'text';
      text.textContent = item.text;
      li.append(icon, text);
      // mousedown, not click: click would land after the input's blur.
      li.addEventListener('mousedown', (event) => {
        event.preventDefault();
        el.input.value = item.text;
        runSearch(item.text);
      });
      el.suggestions.appendChild(li);
    });
    el.suggestions.hidden = false;
    el.input.setAttribute('aria-expanded', 'true');
    setGhost(items[0].text);
  }

  function highlight(index) {
    const nodes = el.suggestions.children;
    if (!nodes.length) return;
    // Wraps through -1, which means "back to what was typed".
    const slots = nodes.length + 1;
    highlighted = ((((index + 1) % slots) + slots) % slots) - 1;
    for (const node of nodes) node.classList.toggle('active', Number(node.dataset.index) === highlighted);
    el.input.value = highlighted >= 0 ? suggestionItems[highlighted].text : typedValue;
    setGhost(highlighted >= 0 ? '' : suggestionItems[0]?.text);
  }

  function requestSuggestions() {
    clearTimeout(suggestTimer);
    const query = el.input.value.trim();
    if (!query) {
      closeSuggestions();
      return;
    }
    suggestTimer = setTimeout(async () => {
      if (suggestAbort) suggestAbort.abort();
      const abort = new AbortController();
      suggestAbort = abort;
      try {
        const items = await api(`/api/suggest?q=${encodeURIComponent(query)}`, { signal: abort.signal });
        if (suggestAbort === abort && el.input.value.trim() === query) renderSuggestions(items || []);
      } catch { /* aborted or offline: no suggestions is fine */ }
    }, 140);
  }

  el.input.addEventListener('input', () => {
    typedValue = el.input.value;
    // Keep the ghost aligned while the next answer is on its way.
    setGhost(el.ghost.dataset.completion);
    requestSuggestions();
  });

  el.input.addEventListener('keydown', (event) => {
    const completion = el.ghost.dataset.completion;
    const atEnd = el.input.selectionStart === el.input.value.length;
    if ((event.key === 'Tab' || (event.key === 'ArrowRight' && atEnd)) && completion) {
      event.preventDefault();
      el.input.value = completion;
      typedValue = completion;
      setGhost('');
      requestSuggestions();
    } else if (event.key === 'ArrowDown' && !el.suggestions.hidden) {
      event.preventDefault();
      highlight(highlighted + 1);
    } else if (event.key === 'ArrowUp' && !el.suggestions.hidden) {
      event.preventDefault();
      highlight(highlighted - 1);
    } else if (event.key === 'Escape') {
      if (!el.suggestions.hidden) {
        event.preventDefault();
        el.input.value = typedValue || el.input.value;
        closeSuggestions();
      }
    }
  });

  el.input.addEventListener('blur', () => setTimeout(closeSuggestions, 100));
  el.input.addEventListener('scroll', () => setGhost(''));

  el.form.addEventListener('submit', (event) => {
    event.preventDefault();
    const query = el.input.value.trim();
    if (query) runSearch(query);
  });

  // ------------------------------------------------------------------- tabs

  let activeTab = 'search';

  function selectTab(name) {
    activeTab = name;
    document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.tab === name));
    document.querySelectorAll('[data-panel]').forEach((panel) => { panel.hidden = panel.dataset.panel !== name; });
    if (name !== 'search') loadTab(name);
  }

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => selectTab(tab.dataset.tab));
  });

  function filterValue(name) {
    const input = document.querySelector(`[data-filter="${name}"]`);
    return input ? input.value.trim() : '';
  }

  document.querySelectorAll('[data-filter]').forEach((input) => {
    let timer = null;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => loadTab(input.dataset.filter), 150);
    });
  });

  let tabSeq = 0;

  async function loadTab(name) {
    const seq = ++tabSeq;
    const q = encodeURIComponent(filterValue(name));
    try {
      if (name === 'stats') {
        const stats = await api('/api/library/stats');
        if (seq === tabSeq) renderStats(stats);
        return;
      }
      const data = await api(`/api/library/${name}?q=${q}`);
      if (seq !== tabSeq) return;
      const list = document.querySelector(`[data-list="${name}"]`);
      document.querySelector(`[data-empty="${name}"]`).hidden = data.length > 0;
      if (name === 'history') {
        renderTracks(list, data, (entry) => ({
          sub: [formatWhen(entry.played_at), entry.singer ? `🎤 ${entry.singer}` : null, thisPlay(entry)],
          stats: songStats(entry),
          details: () => trackDetails(entry.id)
        }));
      } else if (name === 'downloads') {
        const [mine, kept] = await Promise.all([
          api(`/api/library/local?q=${q}${rescanNext ? '&refresh=true' : ''}`),
          api(`/api/library/saved?q=${q}`)
        ]);
        rescanNext = false;
        if (seq !== tabSeq) return;
        el.localFolders.textContent = mine.folders.join(' · ');
        document.querySelector('[data-empty="local"]').hidden = mine.items.length > 0;
        renderTracks(document.querySelector('[data-list="local"]'), mine.items, (entry) => ({
          sub: [formatSize(entry.size), entry.name]
        }));
        if (seq !== tabSeq) return;
        el.savedDir.textContent = kept.directory;
        el.savedFfmpeg.hidden = kept.ffmpeg;
        document.querySelector('[data-empty="saved"]').hidden = kept.items.length > 0;
        renderTracks(document.querySelector('[data-list="saved"]'), kept.items, (entry) => ({
          sub: [formatSize(entry.size), entry.path.split(/[\\/]/).pop()],
          actions: [button(t('downloads.delete'), () => deleteSaved(entry), 'ghost danger')]
        }));
        const total = data.reduce((sum, entry) => sum + (entry.size || 0), 0);
        el.downloadsSummary.textContent = data.length ? t('downloads.summary', { n: data.length, size: formatSize(total) }) : '';
        renderTracks(list, data, (entry) => ({
          sub: [formatSize(entry.size)],
          actions: [button(t('downloads.delete'), () => deleteDownload(entry), 'ghost danger')]
        }));
      } else {
        renderTracks(list, data, (entry) => ({
          sub: entry.plays ? [t('library.plays', { n: entry.plays })] : []
        }));
      }
    } catch (error) {
      showError(error);
    }
  }

  /** How much of one particular play was heard. */
  function thisPlay(entry) {
    if (entry.completed) return t('history.toTheEnd');
    if (!entry.listened) return t('history.nowPlaying');
    const length = entry.duration ? ` / ${formatTime(entry.duration)}` : '';
    return t('history.listened', { time: `${formatTime(entry.listened)}${length}` });
  }

  /** The song's totals, on every one of its History rows. */
  function songStats(entry) {
    if (!entry.plays) return [];
    return [
      t('library.plays', { n: entry.plays }),
      formatDuration(entry.listened_seconds),
      entry.completed_count ? t('history.fullPlays', { n: entry.completed_count }) : null,
      entry.singer_count > 1 ? t('history.singers', { n: entry.singer_count }) : null,
      entry.first_played ? t('history.since', { date: formatDate(entry.first_played) }) : null
    ];
  }

  function formatDate(epochSeconds) {
    return new Date(epochSeconds * 1000).toLocaleDateString(window.i18n.getLocale(), {
      day: 'numeric', month: 'short', year: 'numeric'
    });
  }

  function formatDateTime(epochSeconds) {
    return new Date(epochSeconds * 1000).toLocaleString(window.i18n.getLocale(), {
      day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'
    });
  }

  /** The expanded panel: who sang it, and its recent plays. */
  async function trackDetails(videoId) {
    const stats = await api(`/api/library/track/${encodeURIComponent(videoId)}`);
    const box = document.createElement('div');
    box.className = 'details-grid';

    const tiles = document.createElement('div');
    tiles.className = 'details-tiles';
    const rate = stats.plays ? Math.round((stats.completed_count / stats.plays) * 100) : 0;
    for (const [label, value] of [
      ['stats.plays', String(stats.plays)],
      ['stats.listened', formatDuration(stats.listened_seconds)],
      ['history.completion', `${rate}%`],
      ['history.lastPlayed', stats.last_played ? formatDateTime(stats.last_played) : '—']
    ]) {
      const tile = document.createElement('div');
      tile.className = 'mini-tile';
      const v = document.createElement('div');
      v.className = 'value';
      v.textContent = value;
      const l = document.createElement('div');
      l.className = 'label';
      l.textContent = t(label);
      tile.append(v, l);
      tiles.appendChild(tile);
    }
    box.appendChild(tiles);

    if (stats.singers.length) {
      const singers = document.createElement('div');
      singers.className = 'details-singers';
      for (const { singer, plays } of stats.singers) {
        const chip = document.createElement('span');
        chip.className = 'qi-singer';
        chip.textContent = `🎤 ${singer} ×${plays}`;
        singers.appendChild(chip);
      }
      box.appendChild(singers);
    }

    const recent = document.createElement('ol');
    recent.className = 'details-plays';
    for (const play of stats.recent) {
      const li = document.createElement('li');
      li.textContent = [
        formatDateTime(play.played_at),
        play.singer ? `🎤 ${play.singer}` : null,
        thisPlay({ ...play, duration: stats.duration })
      ].filter(Boolean).join(' · ');
      recent.appendChild(li);
    }
    box.appendChild(recent);
    return box;
  }

  function deleteSaved(entry) {
    api(`/api/library/saved/${encodeURIComponent(entry.id)}`, { method: 'DELETE' })
      .then(() => loadTab('downloads'))
      .catch(showError);
  }

  el.openFolder.addEventListener('click', () => post('/api/library/open-folder'));

  // The folder scan is reused for half a minute; Rescan picks up a file
  // copied in just now.
  let rescanNext = false;
  el.rescan.addEventListener('click', () => {
    rescanNext = true;
    loadTab('downloads');
  });

  function deleteDownload(entry) {
    api(`/api/library/downloads/${encodeURIComponent(entry.id)}`, { method: 'DELETE' })
      .then(() => loadTab('downloads'))
      .catch(showError);
  }

  // ------------------------------------------------------------------ stats

  function renderStats(stats) {
    el.stats.textContent = '';
    const tiles = document.createElement('div');
    tiles.className = 'stat-tiles';
    for (const [label, value] of [
      ['stats.plays', String(stats.plays)],
      ['stats.listened', formatDuration(stats.listened_seconds)],
      ['stats.tracks', String(stats.tracks)],
      ['stats.favourites', String(stats.favourites)]
    ]) {
      const tile = document.createElement('div');
      tile.className = 'stat-tile';
      const v = document.createElement('div');
      v.className = 'value';
      v.textContent = value;
      const l = document.createElement('div');
      l.className = 'label';
      l.textContent = t(label);
      tile.append(v, l);
      tiles.appendChild(tile);
    }
    el.stats.appendChild(tiles);

    // Plays per day, last two weeks, as plain bars.
    const chart = document.createElement('div');
    chart.className = 'stat-chart';
    const heading = document.createElement('h3');
    heading.className = 'section-heading';
    heading.textContent = t('stats.perDay');
    const bars = document.createElement('div');
    bars.className = 'bars';
    const max = Math.max(1, ...stats.days.map((d) => d.plays));
    for (const day of stats.days) {
      const col = document.createElement('div');
      col.className = 'bar-col';
      col.title = `${day.day}: ${t('library.plays', { n: day.plays })}`;
      const bar = document.createElement('div');
      bar.className = 'bar';
      bar.style.height = `${(day.plays / max) * 100}%`;
      const label = document.createElement('div');
      label.className = 'bar-label';
      label.textContent = day.day.slice(8);
      col.append(bar, label);
      bars.appendChild(col);
    }
    chart.append(heading, bars);
    el.stats.appendChild(chart);

    const lists = document.createElement('div');
    lists.className = 'stat-lists';
    lists.append(
      statList('stats.topTracks', stats.top_tracks, (tr) => tr.title, (tr) => t('library.plays', { n: tr.plays })),
      statList('stats.topChannels', stats.top_channels, (c) => c.channel, (c) => t('library.plays', { n: c.plays })),
      statList('stats.topSingers', stats.top_singers, (s) => s.singer, (s) => t('library.plays', { n: s.plays }))
    );
    el.stats.appendChild(lists);
  }

  function statList(titleKey, rows, name, value) {
    const box = document.createElement('div');
    box.className = 'stat-list';
    const heading = document.createElement('h3');
    heading.className = 'section-heading';
    heading.textContent = t(titleKey);
    const ol = document.createElement('ol');
    for (const row of rows) {
      const li = document.createElement('li');
      const n = document.createElement('span');
      n.className = 'name';
      n.textContent = name(row);
      n.title = name(row);
      const v = document.createElement('span');
      v.className = 'val';
      v.textContent = value(row);
      li.append(n, v);
      ol.appendChild(li);
    }
    if (!rows.length) {
      const li = document.createElement('li');
      li.className = 'none';
      li.textContent = '—';
      ol.appendChild(li);
    }
    box.append(heading, ol);
    return box;
  }

  // ------------------------------------------------------------------ queue

  // The queue re-renders on every broadcast, several times a second; an open
  // singer editor would be wiped mid-keystroke, so rendering pauses for it.
  let editingSinger = null;

  function renderQueue(state) {
    if (editingSinger !== null) return;
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
      meta.className = 'qi-meta';
      const title = document.createElement('div');
      title.className = 'qi-title';
      if (item.singer) {
        const singer = document.createElement('span');
        singer.className = 'qi-singer';
        singer.textContent = item.singer;
        title.appendChild(singer);
      }
      title.appendChild(document.createTextNode(item.title));
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

      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'qi-btn';
      edit.textContent = '🎤';
      edit.title = t('queue.editSinger');
      edit.addEventListener('click', (event) => {
        event.stopPropagation();
        editSinger(node, meta, index, item.singer || '');
      });

      const track = {
        id: item.video_id, title: item.title, channel: item.channel, duration: item.duration, thumbnail: item.thumbnail
      };
      const star = starButton(track);
      star.classList.add('qi-btn');
      const save = isLocal(track) ? document.createElement('span') : saveButton(track);
      save.classList.add('qi-btn');

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
      node.append(position, meta, edit, save, star, remove);
      fragment.appendChild(node);
    });
    el.queue.appendChild(fragment);
  }

  function editSinger(node, meta, index, current) {
    editingSinger = index;
    node.draggable = false;
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'qi-singer-input';
    input.maxLength = 80;
    input.value = current;
    input.placeholder = t('singer.placeholder');
    meta.replaceChildren(input);
    input.focus();
    input.select();

    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      editingSinger = null;
      if (save && input.value.trim() !== current) {
        api(`/api/queue/${index}/singer`, { method: 'PUT', body: JSON.stringify({ singer: input.value.trim() || null }) })
          .catch(showError);
      }
      if (lastState) renderQueue(lastState);
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') finish(true);
      if (event.key === 'Escape') finish(false);
      event.stopPropagation();
    });
    input.addEventListener('blur', () => finish(true));
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

  let npTrack = null;

  function renderPlayer(state) {
    const current = state.current >= 0 ? state.items[state.current] : null;
    const player = state.player;

    el.npTitle.textContent = current ? current.title : t('player.nothing');
    el.npChannel.textContent = current
      ? [current.singer ? `🎤 ${current.singer}` : null, current.channel].filter(Boolean).join(' · ')
      : '';
    npTrack = current && {
      id: current.video_id, title: current.title, channel: current.channel, duration: current.duration, thumbnail: current.thumbnail
    };
    el.npStar.hidden = !current;
    el.npSave.hidden = !current || isLocal({ id: current.video_id });
    if (current) {
      el.npStar.dataset.favId = current.video_id;
      paintStar(el.npStar, favourites.has(current.video_id));
      el.npSave.dataset.saveId = current.video_id;
      paintSave(el.npSave);
    }

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

  el.npStar.addEventListener('click', () => { if (npTrack) toggleFavourite(npTrack); });
  el.npSave.addEventListener('click', () => { if (npTrack) saveTrack(npTrack); });

  let lastJobs = '{}';

  function sameSet(set, list) {
    return set.size === list.length && list.every((id) => set.has(id));
  }

  function render(state) {
    lastState = state;
    const favs = state.favourites || [];
    const disk = state.downloads || [];
    const kept = state.saved || [];
    const jobs = JSON.stringify(state.saving || {});
    const savedChanged = !sameSet(saved, kept);
    if (!sameSet(favourites, favs) || !sameSet(downloads, disk) || savedChanged || jobs !== lastJobs) {
      favourites = new Set(favs);
      downloads = new Set(disk);
      saved = new Set(kept);
      saving = state.saving || {};
      lastJobs = jobs;
      syncMarks();
      if (savedChanged && activeTab === 'downloads') loadTab('downloads');
    }
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

  // The singer field sticks between adds -- one person often queues several
  // songs -- and across restarts, as a per-window convenience.
  el.singer.value = store.get('cue.singer') || '';
  el.singer.addEventListener('change', () => store.set('cue.singer', el.singer.value.trim()));

  // --------------------------------------------------------------- settings

  // Declarative so a new backend setting is one entry here, not a control in
  // the HTML plus a wired-up listener. `label`/`help` are i18n keys. Advanced,
  // playback-breaking knobs (player_clients, potoken_clients, invidious) are
  // deliberately left to settings.json -- see the note in the dialog.
  const SETTINGS_FIELDS = [
    { key: 'show_upcoming_overlay', type: 'toggle', group: 'settings.group.overlay' },
    { key: 'overlay_style', type: 'select', options: ['bar', 'list'], optionLabels: true, group: 'settings.group.overlay' },
    { key: 'overlay_label', type: 'select', options: ['both', 'singer', 'title'], optionLabels: true, group: 'settings.group.overlay' },
    {
      key: 'overlay_position',
      type: 'select',
      options: ['top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right'],
      optionLabels: true, // localise each option via settings.<key>.<value>
      group: 'settings.group.overlay'
    },
    { key: 'overlay_boxes', type: 'number', min: 1, max: 6, step: 1, group: 'settings.group.overlay' },
    { key: 'overlay_show_timer', type: 'toggle', group: 'settings.group.overlay' },
    { key: 'overlay_font_size', type: 'number', min: 10, max: 96, step: 1, group: 'settings.group.overlay' },
    { key: 'overlay_brand', type: 'text', placeholder: 'Karaoke Night', group: 'settings.group.look' },
    { key: 'overlay_bar_heading', type: 'text', placeholderKey: 'settings.default', group: 'settings.group.look' },
    { key: 'overlay_stage_heading', type: 'text', placeholderKey: 'settings.default', group: 'settings.group.look' },
    { key: 'overlay_accent_color', type: 'color', group: 'settings.group.look' },
    { key: 'overlay_bar_color', type: 'color', group: 'settings.group.look' },
    { key: 'overlay_box_color', type: 'color', group: 'settings.group.look' },
    { key: 'overlay_logo_size', type: 'logo', group: 'settings.group.look' },
    { key: 'show_intermission', type: 'toggle', group: 'settings.group.intermission' },
    { key: 'intermission_color', type: 'color', group: 'settings.group.intermission' },
    { key: 'gap_seconds', type: 'number', min: 0, max: 3600, step: 1, group: 'settings.group.intermission' },
    { key: 'download_dir', type: 'dir', group: 'settings.group.downloads' },
    { key: 'library_folders', type: 'dirs', group: 'settings.group.downloads' },
    { key: 'download_format', type: 'select', options: ['video', 'audio'], optionLabels: true, group: 'settings.group.downloads' },
    { key: 'download_played', type: 'toggle', group: 'settings.group.downloads' },
    { key: 'ffmpeg_path', type: 'text', placeholderKey: 'settings.auto', group: 'settings.group.downloads' },
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
          option.textContent = field.optionLabels ? t(`settings.${field.key}.${opt}`) : String(opt);
          control.appendChild(option);
        }
        control.value = String(saved[field.key]);
      } else if (field.type === 'toggle') {
        control = document.createElement('input');
        control.type = 'checkbox';
        control.checked = Boolean(saved[field.key]);
      } else if (field.type === 'logo') {
        control = logoControl(Boolean(saved[field.key]));
      } else if (field.type === 'dirs') {
        control = dirsControl(field, saved);
      } else if (field.type === 'dir') {
        control = dirControl(field, saved);
      } else if (field.type === 'color') {
        control = document.createElement('input');
        control.type = 'color';
        control.value = saved[field.key] || '#000000';
      } else {
        control = document.createElement('input');
        control.type = field.type === 'number' ? 'number' : 'text';
        if (field.type === 'number') Object.assign(control, { min: field.min, max: field.max, step: field.step });
        if (field.placeholder) control.placeholder = field.placeholder;
        if (field.placeholderKey) control.placeholder = t(field.placeholderKey);
        control.value = saved[field.key] == null ? '' : String(saved[field.key]);
      }

      if (!['logo', 'dir', 'dirs'].includes(field.type)) {
        control.addEventListener('change', () => saveSetting(field.key, settingValue(field, control)));
      }
      row.append(caption, control);
      el.settingsFields.appendChild(row);
    }
  }

  /** Folder field: typed path, plus a native picker when running in Electron. */
  /** A list of folders: one row per folder with a remove button, plus an add row. */
  function dirsControl(field, saved) {
    const folders = [...(saved[field.key] || [])];
    const wrap = document.createElement('span');
    wrap.className = 'dirs-control';
    const save = () => saveSetting(field.key, folders).then(() => {
      saved[field.key] = folders;
      wrap.replaceWith(dirsControl(field, saved));
      if (activeTab === 'downloads') loadTab('downloads');
    });
    for (const [index, folder] of folders.entries()) {
      const row = document.createElement('span');
      row.className = 'dir-row';
      const name = document.createElement('span');
      name.className = 'dir-path';
      name.textContent = folder;
      name.title = folder;
      row.append(name, button('×', () => { folders.splice(index, 1); save(); }, 'ghost danger'));
      wrap.appendChild(row);
    }
    const add = document.createElement('span');
    add.className = 'dir-control';
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = t('settings.library_folders.add');
    const commit = (value) => {
      const path = (value || '').trim();
      if (!path || folders.includes(path)) return;
      folders.push(path);
      save();
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        commit(input.value);
      }
    });
    add.appendChild(input);
    add.appendChild(button('+', () => commit(input.value), 'ghost'));
    if (window.desktop?.chooseDirectory) {
      add.appendChild(button('…', async () => commit(await window.desktop.chooseDirectory()), 'ghost'));
    }
    wrap.appendChild(add);
    return wrap;
  }

  function dirControl(field, saved) {
    const wrap = document.createElement('span');
    wrap.className = 'dir-control';
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = t('settings.download_dir.default');
    input.value = saved[field.key] || '';
    input.addEventListener('change', () => saveSetting(field.key, input.value.trim() || null));
    wrap.appendChild(input);
    if (window.desktop?.chooseDirectory) {
      wrap.appendChild(button('…', async () => {
        const picked = await window.desktop.chooseDirectory(input.value || undefined);
        if (picked) {
          input.value = picked;
          saveSetting(field.key, picked);
        }
      }, 'ghost'));
    }
    return wrap;
  }

  // The logo is decoded here, by the browser, which reads any format it can
  // display. It is scaled down, converted to premultiplied BGRA -- the raw
  // layout mpv's overlay-add takes -- and uploaded as plain bytes, so the
  // backend needs no imaging library.
  const LOGO_MAX = { w: 640, h: 320 };

  function logoControl(hasLogo) {
    const wrap = document.createElement('span');
    wrap.className = 'dir-control';
    const file = document.createElement('input');
    file.type = 'file';
    file.accept = 'image/*';
    file.hidden = true;
    const pick = button(hasLogo ? t('settings.logo.replace') : t('settings.logo.choose'), () => file.click(), 'ghost');
    const remove = button(t('settings.logo.remove'), () => {
      api('/api/overlay/logo', { method: 'DELETE' }).then(openSettings).catch(showError);
    }, 'ghost danger');
    remove.hidden = !hasLogo;
    file.addEventListener('change', async () => {
      if (!file.files[0]) return;
      try {
        await uploadLogo(file.files[0]);
        openSettings();
      } catch (error) {
        showError(error);
      }
    });
    wrap.append(file, pick, remove);
    return wrap;
  }

  async function uploadLogo(blob) {
    const bitmap = await createImageBitmap(blob);
    const scale = Math.min(1, LOGO_MAX.w / bitmap.width, LOGO_MAX.h / bitmap.height);
    const w = Math.max(1, Math.round(bitmap.width * scale));
    const h = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(bitmap, 0, 0, w, h);
    const rgba = ctx.getImageData(0, 0, w, h).data;
    const bgra = new Uint8Array(rgba.length);
    for (let i = 0; i < rgba.length; i += 4) {
      const a = rgba[i + 3];
      bgra[i] = Math.round((rgba[i + 2] * a) / 255);
      bgra[i + 1] = Math.round((rgba[i + 1] * a) / 255);
      bgra[i + 2] = Math.round((rgba[i] * a) / 255);
      bgra[i + 3] = a;
    }
    await api(`/api/overlay/logo?w=${w}&h=${h}`, {
      method: 'POST',
      body: bgra,
      headers: { 'Content-Type': 'application/octet-stream' }
    });
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
      ['settings.diag.potoken', potoken],
      ['settings.diag.ffmpeg', diag.ffmpeg || t('settings.diag.missing')]
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
    // Re-run after a logo change while the dialog is already up.
    if (!el.settingsDialog.open) el.settingsDialog.showModal();
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
    syncMarks();
    if (activeTab !== 'search') loadTab(activeTab);
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
