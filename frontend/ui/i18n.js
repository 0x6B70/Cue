/**
 * Translations for the search/queue UI.
 *
 * Loaded before app.js and exposed as window.i18n. Backend messages arrive as
 * a `notice_code` plus params rather than prose, so an error raised in Python
 * still renders in the active locale — see backend/engine.py and the
 * `resolve.*` / `error.*` keys below.
 *
 * A missing key falls back to English, and then to the key itself, so a half
 * translated locale degrades instead of showing blanks.
 */

(() => {
  'use strict';

  const STRINGS = {
    en: {
      'app.title': 'Cue — Search',
      'search.placeholder': 'Search YouTube…',
      'search.label': 'Search YouTube',
      'search.submit': 'Search',
      'search.searching': 'Searching for “{query}”…',
      'search.failed': 'Search failed: {message}',
      'search.none': 'No results.',
      'search.empty': 'Search for something to get started.',
      'results.label': 'Search results',
      'result.queue': 'Queue',
      'result.playNow': 'Play now',
      'result.live': 'live',
      'connection.title': 'Backend connection',
      'connection.connecting': 'connecting…',
      'connection.connected': 'connected',
      'connection.reconnecting': 'reconnecting…',
      'notice.dismiss': 'Dismiss',
      'player.nothing': 'Nothing playing',
      'player.seek': 'Seek',
      'player.volume': 'Volume',
      'player.previous': 'Previous',
      'player.next': 'Next',
      'player.toggle': 'Play/Pause',
      'player.play': 'Play',
      'player.pause': 'Pause',
      'player.reopen': 'Reopen player window',
      'queue.label': 'Play queue',
      'queue.heading': 'Queue',
      'queue.clearPlayed': 'Clear played',
      'queue.clearAll': 'Clear all',
      'queue.remove': 'Remove',
      'queue.empty': 'Queue is empty. Add something from the results.',
      'language.label': 'Language',
      'gap.label': 'Pause between videos, in seconds',
      'gap.unit': 's',
      'queue.ready': 'Ready',
      'queue.willFail': 'Will not play',
      // Settings panel (app.js)
      'settings.open': 'Settings',
      'settings.title': 'Settings',
      'settings.close': 'Close',
      'settings.group.overlay': 'Overlay',
      'settings.group.playback': 'Playback',
      'settings.group.reliability': 'Reliability',
      'settings.show_upcoming_overlay': 'Show “up next” overlay in the video',
      'settings.gap_seconds': 'Pause between videos (seconds)',
      'settings.max_height': 'Maximum video height',
      'settings.clear_queue_on_exit': 'Clear the queue when the app exits',
      'settings.ytdlp_channel': 'yt-dlp update channel',
      'settings.offline_cache': 'Cache upcoming videos to disk',
      'settings.cache_budget_gb': 'Cache size limit (GB)',
      'settings.cookies_from_browser': 'Cookies from browser',
      'settings.diagnostics': 'Diagnostics',
      'settings.advanced':
        'Advanced options (resolver clients, Invidious instances) live in settings.json.',
      'settings.diag.ytdlp': 'yt-dlp version',
      'settings.diag.jsRuntime': 'JavaScript runtime',
      'settings.diag.lastTier': 'Last working method',
      'settings.diag.potoken': 'PO token',
      'settings.diag.missing': 'missing',
      'settings.diag.held': 'held',
      'settings.diag.none': 'none',
      // Backend notices (engine.py)
      'notice.gap': 'Next video in {seconds}s',
      'notice.resolve_failed': '{title}: {reason}',
      'notice.playback_failed': '{title}: playback failed, skipping.',
      'notice.player_unavailable': '{detail}',
      'notice.window_closed': 'Player window closed.',
      'notice.segment_skipped': 'Skipped {category}',
      'notice.js_runtime_missing':
        'No JavaScript runtime found (install Deno). Playback may be unreliable until it is set up.',
      'notice.ytdlp_updated': 'Updated yt-dlp — try that video again.',
      // Resolver verdicts (resolver.py)
      'resolve.bot_check':
        "YouTube's bot check blocked every method. Try again shortly, or add cookies in Settings.",
      'resolve.age_restricted': 'Age-restricted — add browser cookies in Settings to play this.',
      'resolve.network': 'Could not reach YouTube. Check your connection.',
      'resolve.not_found': 'yt-dlp is missing — reinstall the app.',
      'resolve.drm': 'This video is DRM protected and cannot be played.',
      'resolve.no_formats': 'No playable format was offered for this video.',
      'resolve.unavailable': 'This video is unavailable.',
      'resolve.bad_option':
        'yt-dlp rejected one of our options — it may be mid-update. This usually clears once yt-dlp updates.',
      'resolve.other': 'Could not find a playable stream for this video.',
      // Item errors (ytdlp.py)
      'error.bot_check': "YouTube asked us to confirm we're not a bot.",
      'error.age_restricted': 'This video is age-restricted and needs a signed-in account.',
      'error.unavailable': 'This video is unavailable.',
      'error.network': 'Network problem reaching YouTube.',
      'error.not_found': 'yt-dlp is missing.',
      'error.drm': 'This video is DRM protected.',
      'error.no_formats': 'No playable format was offered for this video.',
      'error.bad_option': 'yt-dlp rejected an option; it may be mid-update.',
      'error.other': 'Could not load this video.',
      'error.playback_failed': 'Playback failed.'
    },

    el: {
      'app.title': 'Cue — Αναζήτηση',
      'search.placeholder': 'Αναζήτηση στο YouTube…',
      'search.label': 'Αναζήτηση στο YouTube',
      'search.submit': 'Αναζήτηση',
      'search.searching': 'Αναζήτηση για «{query}»…',
      'search.failed': 'Η αναζήτηση απέτυχε: {message}',
      'search.none': 'Κανένα αποτέλεσμα.',
      'search.empty': 'Αναζητήστε κάτι για να ξεκινήσετε.',
      'results.label': 'Αποτελέσματα αναζήτησης',
      'result.queue': 'Στη λίστα',
      'result.playNow': 'Αναπαραγωγή τώρα',
      'result.live': 'ζωντανά',
      'connection.title': 'Σύνδεση με το backend',
      'connection.connecting': 'σύνδεση…',
      'connection.connected': 'συνδεδεμένο',
      'connection.reconnecting': 'επανασύνδεση…',
      'notice.dismiss': 'Απόρριψη',
      'player.nothing': 'Καμία αναπαραγωγή',
      'player.seek': 'Μετάβαση',
      'player.volume': 'Ένταση',
      'player.previous': 'Προηγούμενο',
      'player.next': 'Επόμενο',
      'player.toggle': 'Αναπαραγωγή/Παύση',
      'player.play': 'Αναπαραγωγή',
      'player.pause': 'Παύση',
      'player.reopen': 'Άνοιγμα ξανά του παραθύρου αναπαραγωγής',
      'queue.label': 'Λίστα αναπαραγωγής',
      'queue.heading': 'Λίστα',
      'queue.clearPlayed': 'Εκκαθάριση όσων παίχτηκαν',
      'queue.clearAll': 'Εκκαθάριση όλων',
      'queue.remove': 'Αφαίρεση',
      'queue.empty': 'Η λίστα είναι άδεια. Προσθέστε κάτι από τα αποτελέσματα.',
      'language.label': 'Γλώσσα',
      'gap.label': 'Παύση μεταξύ βίντεο, σε δευτερόλεπτα',
      'gap.unit': 'δευτ.',
      'queue.ready': 'Έτοιμο',
      'queue.willFail': 'Δεν θα παίξει',
      'settings.open': 'Ρυθμίσεις',
      'settings.title': 'Ρυθμίσεις',
      'settings.close': 'Κλείσιμο',
      'settings.group.overlay': 'Επικάλυψη',
      'settings.group.playback': 'Αναπαραγωγή',
      'settings.group.reliability': 'Αξιοπιστία',
      'settings.show_upcoming_overlay': 'Εμφάνιση επικάλυψης «επόμενα» στο βίντεο',
      'settings.gap_seconds': 'Παύση μεταξύ βίντεο (δευτερόλεπτα)',
      'settings.max_height': 'Μέγιστο ύψος βίντεο',
      'settings.clear_queue_on_exit': 'Εκκαθάριση λίστας κατά την έξοδο',
      'settings.ytdlp_channel': 'Κανάλι ενημερώσεων yt-dlp',
      'settings.offline_cache': 'Αποθήκευση επόμενων βίντεο στον δίσκο',
      'settings.cache_budget_gb': 'Όριο μεγέθους cache (GB)',
      'settings.cookies_from_browser': 'Cookies από πρόγραμμα περιήγησης',
      'settings.diagnostics': 'Διαγνωστικά',
      'settings.advanced':
        'Οι προχωρημένες επιλογές (πελάτες resolver, instances Invidious) βρίσκονται στο settings.json.',
      'settings.diag.ytdlp': 'Έκδοση yt-dlp',
      'settings.diag.jsRuntime': 'Περιβάλλον JavaScript',
      'settings.diag.lastTier': 'Τελευταία μέθοδος που δούλεψε',
      'settings.diag.potoken': 'PO token',
      'settings.diag.missing': 'λείπει',
      'settings.diag.held': 'διαθέσιμο',
      'settings.diag.none': 'κανένα',
      'notice.gap': 'Επόμενο βίντεο σε {seconds} δευτ.',
      'notice.resolve_failed': '{title}: {reason}',
      'notice.playback_failed': '{title}: η αναπαραγωγή απέτυχε, παράλειψη.',
      'notice.player_unavailable': '{detail}',
      'notice.window_closed': 'Το παράθυρο αναπαραγωγής έκλεισε.',
      'notice.segment_skipped': 'Παραλείφθηκε: {category}',
      'notice.js_runtime_missing':
        'Δεν βρέθηκε περιβάλλον JavaScript (εγκαταστήστε το Deno). Η αναπαραγωγή ίσως είναι ασταθής μέχρι να ρυθμιστεί.',
      'notice.ytdlp_updated': 'Το yt-dlp ενημερώθηκε — δοκιμάστε ξανά αυτό το βίντεο.',
      'resolve.bot_check':
        'Ο έλεγχος bot του YouTube απέκλεισε κάθε μέθοδο. Δοκιμάστε ξανά σύντομα ή προσθέστε cookies στις Ρυθμίσεις.',
      'resolve.age_restricted':
        'Περιορισμός ηλικίας — προσθέστε cookies του προγράμματος περιήγησης στις Ρυθμίσεις.',
      'resolve.network': 'Δεν ήταν δυνατή η σύνδεση με το YouTube. Ελέγξτε τη σύνδεσή σας.',
      'resolve.not_found': 'Το yt-dlp λείπει — επανεγκαταστήστε την εφαρμογή.',
      'resolve.drm': 'Αυτό το βίντεο προστατεύεται με DRM και δεν μπορεί να αναπαραχθεί.',
      'resolve.no_formats': 'Δεν προσφέρθηκε αναπαραγώγιμη μορφή για αυτό το βίντεο.',
      'resolve.unavailable': 'Αυτό το βίντεο δεν είναι διαθέσιμο.',
      'resolve.bad_option':
        'Το yt-dlp απέρριψε μία από τις επιλογές μας — ίσως ενημερώνεται. Συνήθως διορθώνεται μόλις ενημερωθεί το yt-dlp.',
      'resolve.other': 'Δεν βρέθηκε αναπαραγώγιμη ροή για αυτό το βίντεο.',
      'error.bot_check': 'Το YouTube ζήτησε να επιβεβαιώσουμε ότι δεν είμαστε bot.',
      'error.age_restricted':
        'Αυτό το βίντεο έχει περιορισμό ηλικίας και απαιτεί συνδεδεμένο λογαριασμό.',
      'error.unavailable': 'Αυτό το βίντεο δεν είναι διαθέσιμο.',
      'error.network': 'Πρόβλημα δικτύου κατά τη σύνδεση με το YouTube.',
      'error.not_found': 'Το yt-dlp λείπει.',
      'error.drm': 'Αυτό το βίντεο προστατεύεται με DRM.',
      'error.no_formats': 'Δεν προσφέρθηκε αναπαραγώγιμη μορφή για αυτό το βίντεο.',
      'error.bad_option': 'Το yt-dlp απέρριψε μια επιλογή· ίσως ενημερώνεται.',
      'error.other': 'Δεν ήταν δυνατή η φόρτωση αυτού του βίντεο.',
      'error.playback_failed': 'Η αναπαραγωγή απέτυχε.'
    }
  };

  const FALLBACK = 'en';
  let locale = FALLBACK;

  function interpolate(template, params) {
    return template.replace(/\{(\w+)\}/g, (match, key) =>
      Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : match
    );
  }

  /** Translate `key`, substituting {placeholders} from `params`. */
  function t(key, params = {}) {
    const table = STRINGS[locale] || STRINGS[FALLBACK];
    const template = table[key] ?? STRINGS[FALLBACK][key] ?? key;
    return interpolate(template, params);
  }

  /**
   * Render a backend message: prefer the translated code, fall back to the
   * English text the backend also sent. An unknown code must never render as
   * a raw key in front of the user.
   */
  function tMessage(code, params, fallbackText) {
    if (!code) return fallbackText || '';
    const table = STRINGS[locale] || STRINGS[FALLBACK];
    const template = table[code] ?? STRINGS[FALLBACK][code];
    return template ? interpolate(template, params || {}) : fallbackText || '';
  }

  function setLocale(next) {
    locale = STRINGS[next] ? next : FALLBACK;
    document.documentElement.lang = locale;
    return locale;
  }

  const getLocale = () => locale;
  const locales = () => Object.keys(STRINGS);

  /** Apply translations to everything marked up in index.html. */
  function applyStatic(root = document) {
    root.querySelectorAll('[data-i18n]').forEach((node) => {
      node.textContent = t(node.dataset.i18n);
    });
    const attrs = [
      ['data-i18n-placeholder', 'placeholder'],
      ['data-i18n-title', 'title'],
      ['data-i18n-aria-label', 'aria-label']
    ];
    for (const [dataAttr, attr] of attrs) {
      root.querySelectorAll(`[${dataAttr}]`).forEach((node) => {
        node.setAttribute(attr, t(node.getAttribute(dataAttr)));
      });
    }
    const title = document.querySelector('title');
    if (title) title.textContent = t('app.title');
  }

  window.i18n = { t, tMessage, setLocale, getLocale, locales, applyStatic, STRINGS };
})();
