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
      'stats.details': 'This song\'s statistics',
      'stats.loading': 'Loading…',
      'history.toTheEnd': 'played to the end',
      'history.nowPlaying': 'playing now',
      'history.listened': 'listened {time}',
      'history.fullPlays': '{n}× to the end',
      'history.singers': '{n} singers',
      'history.since': 'since {date}',
      'history.completion': 'Played to the end',
      'history.lastPlayed': 'Last played',
      'local.heading': 'Your folders',
      'local.rescan': 'Rescan',
      'local.none': 'No videos in your folders yet. Add folders in Settings → Downloads.',
      'suggest.file': 'A file in your folders',
      'settings.library_folders': 'Library folders',
      'settings.library_folders.add': 'Folder path, or …',
      'notice.file_missing': '{title}: the file is no longer there.',
      'error.file_missing': 'The file is no longer there.',
      'save.add': 'Save to your download folder',
      'save.saving': 'Saving…',
      'save.done': 'Saved to your folder',
      'save.failed': 'Saving failed: {error}',
      'saved.heading': 'Saved to your folder',
      'saved.openFolder': 'Open folder',
      'saved.none': 'Nothing saved yet. Press ⬇ on any song.',
      'saved.noFfmpeg': 'ffmpeg was not found, so videos are saved in YouTube\'s single-file format (usually 360p). Install it (winget install Gyan.FFmpeg) for full quality.',
      'cache.heading': 'Offline cache',
      'settings.group.downloads': 'Downloads',
      'settings.download_dir': 'Download folder',
      'settings.download_dir.default': 'Videos\\Cue (default)',
      'settings.download_format': 'Save as',
      'settings.download_format.video': 'Video',
      'settings.download_format.audio': 'Audio only',
      'settings.download_played': 'Save every song after it plays',
      'settings.ffmpeg_path': 'ffmpeg location',
      'settings.auto': 'found automatically',
      'settings.overlay_logo_size': 'Logo image',
      'settings.logo.choose': 'Choose image…',
      'settings.logo.replace': 'Replace…',
      'settings.logo.remove': 'Remove',
      'settings.diag.ffmpeg': 'ffmpeg',
      'singer.label': 'Singer',
      'singer.placeholder': 'Singer (optional)',
      'singer.help': 'Name added to the songs you queue, shown in the player overlay',
      'search.fromLibrary': 'From your library',
      'search.fromYoutube': 'From YouTube',
      'suggest.history': 'From your searches and plays',
      'suggest.youtube': 'YouTube suggestion',
      'result.saved': 'saved',
      'result.savedHelp': 'Saved to your download folder',
      'result.cached': 'cache',
      'result.cachedHelp': 'Kept temporarily in the offline cache because it was queued; not in your download folder',
      'fav.add': 'Add to favourites',
      'fav.remove': 'Remove from favourites',
      'tab.search': 'Search',
      'tab.favourites': 'Favourites',
      'tab.history': 'History',
      'tab.downloads': 'Downloads',
      'tab.stats': 'Stats',
      'library.filter': 'Filter…',
      'library.noFavourites': 'No favourites yet. Star a song to keep it here.',
      'library.noHistory': 'Nothing played yet.',
      'library.noDownloads': 'No downloaded videos yet. Upcoming songs are saved here as they are cached.',
      'library.plays': '{n} plays',
      'downloads.summary': '{n} videos · {size}',
      'downloads.delete': 'Delete',
      'queue.editSinger': 'Set singer',
      'stats.plays': 'Plays',
      'stats.listened': 'Listened',
      'stats.tracks': 'Different songs',
      'stats.favourites': 'Favourites',
      'stats.perDay': 'Plays per day',
      'stats.topTracks': 'Most played',
      'stats.topChannels': 'Top channels',
      'stats.topSingers': 'Top singers',
      'stats.minutes': '{n} min',
      'stats.hours': '{h}h {m}m',
      'settings.group.look': 'Overlay look',
      'settings.group.intermission': 'Between songs',
      'settings.default': 'default',
      'settings.overlay_style': 'Overlay style',
      'settings.overlay_style.bar': 'Bar with boxes',
      'settings.overlay_style.list': 'Corner list',
      'settings.overlay_label': 'Show for upcoming songs',
      'settings.overlay_label.both': 'Singer and title',
      'settings.overlay_label.singer': 'Singer names',
      'settings.overlay_label.title': 'Song titles',
      'settings.overlay_boxes': 'Number of boxes',
      'settings.overlay_show_timer': 'Show the time left in the bar',
      'settings.overlay_brand': 'Name shown on the bar and the intermission',
      'settings.overlay_bar_heading': 'Bar heading',
      'settings.overlay_stage_heading': 'Intermission heading',
      'settings.overlay_accent_color': 'Accent colour',
      'settings.overlay_bar_color': 'Bar colour',
      'settings.overlay_box_color': 'Box colour',
      'settings.show_intermission': 'Show “next on stage” card during the pause',
      'settings.intermission_color': 'Intermission background',
      // Settings panel (app.js)
      'settings.open': 'Settings',
      'settings.title': 'Settings',
      'settings.close': 'Close',
      'settings.group.overlay': 'Overlay',
      'settings.group.playback': 'Playback',
      'settings.group.reliability': 'Reliability',
      'settings.show_upcoming_overlay': 'Show “up next” overlay in the video',
      'settings.overlay_position': 'Overlay position',
      'settings.overlay_position.top-left': 'Top left',
      'settings.overlay_position.top-center': 'Top center',
      'settings.overlay_position.top-right': 'Top right',
      'settings.overlay_position.bottom-left': 'Bottom left',
      'settings.overlay_position.bottom-center': 'Bottom center',
      'settings.overlay_position.bottom-right': 'Bottom right',
      'settings.overlay_font_size': 'Overlay font size',
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
      'notice.stalled':
        'Playback paused — YouTube is not responding ({reason}). Retrying in {seconds}s, or pick a song to try now.',
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
      'stats.details': 'Στατιστικά αυτού του τραγουδιού',
      'stats.loading': 'Φόρτωση…',
      'history.toTheEnd': 'μέχρι το τέλος',
      'history.nowPlaying': 'παίζει τώρα',
      'history.listened': 'ακούστηκε {time}',
      'history.fullPlays': '{n}× μέχρι το τέλος',
      'history.singers': '{n} τραγουδιστές',
      'history.since': 'από {date}',
      'history.completion': 'Μέχρι το τέλος',
      'history.lastPlayed': 'Τελευταία φορά',
      'local.heading': 'Οι φάκελοί σας',
      'local.rescan': 'Νέα σάρωση',
      'local.none': 'Δεν υπάρχουν βίντεο στους φακέλους σας ακόμα. Προσθέστε φακέλους στις Ρυθμίσεις → Λήψεις.',
      'suggest.file': 'Αρχείο στους φακέλους σας',
      'settings.library_folders': 'Φάκελοι βιβλιοθήκης',
      'settings.library_folders.add': 'Διαδρομή φακέλου ή …',
      'notice.file_missing': '{title}: το αρχείο δεν υπάρχει πια.',
      'error.file_missing': 'Το αρχείο δεν υπάρχει πια.',
      'save.add': 'Αποθήκευση στον φάκελο λήψεων',
      'save.saving': 'Αποθήκευση…',
      'save.done': 'Αποθηκεύτηκε στον φάκελό σας',
      'save.failed': 'Η αποθήκευση απέτυχε: {error}',
      'saved.heading': 'Αποθηκευμένα στον φάκελό σας',
      'saved.openFolder': 'Άνοιγμα φακέλου',
      'saved.none': 'Δεν έχει αποθηκευτεί τίποτα ακόμα. Πατήστε ⬇ σε οποιοδήποτε τραγούδι.',
      'saved.noFfmpeg': 'Δεν βρέθηκε το ffmpeg, οπότε τα βίντεο αποθηκεύονται στη μορφή ενός αρχείου του YouTube (συνήθως 360p). Εγκαταστήστε το (winget install Gyan.FFmpeg) για πλήρη ποιότητα.',
      'cache.heading': 'Προσωρινή μνήμη εκτός σύνδεσης',
      'settings.group.downloads': 'Λήψεις',
      'settings.download_dir': 'Φάκελος λήψεων',
      'settings.download_dir.default': 'Videos\\Cue (προεπιλογή)',
      'settings.download_format': 'Αποθήκευση ως',
      'settings.download_format.video': 'Βίντεο',
      'settings.download_format.audio': 'Μόνο ήχος',
      'settings.download_played': 'Αποθήκευση κάθε τραγουδιού αφού παίξει',
      'settings.ffmpeg_path': 'Θέση ffmpeg',
      'settings.auto': 'αυτόματος εντοπισμός',
      'settings.overlay_logo_size': 'Εικόνα λογότυπου',
      'settings.logo.choose': 'Επιλογή εικόνας…',
      'settings.logo.replace': 'Αντικατάσταση…',
      'settings.logo.remove': 'Αφαίρεση',
      'settings.diag.ffmpeg': 'ffmpeg',
      'singer.label': 'Τραγουδιστής',
      'singer.placeholder': 'Τραγουδιστής (προαιρετικό)',
      'singer.help': 'Όνομα που προστίθεται στα τραγούδια που βάζετε στη λίστα και εμφανίζεται στο βίντεο',
      'search.fromLibrary': 'Από τη βιβλιοθήκη σας',
      'search.fromYoutube': 'Από το YouTube',
      'suggest.history': 'Από τις αναζητήσεις και αναπαραγωγές σας',
      'suggest.youtube': 'Πρόταση YouTube',
      'result.saved': 'αποθηκευμένο',
      'result.savedHelp': 'Αποθηκευμένο στον φάκελο λήψεων',
      'result.cached': 'προσωρινό',
      'result.cachedHelp': 'Προσωρινό αντίγραφο στην cache επειδή μπήκε στη λίστα· δεν είναι στον φάκελο λήψεων',
      'fav.add': 'Προσθήκη στα αγαπημένα',
      'fav.remove': 'Αφαίρεση από τα αγαπημένα',
      'tab.search': 'Αναζήτηση',
      'tab.favourites': 'Αγαπημένα',
      'tab.history': 'Ιστορικό',
      'tab.downloads': 'Λήψεις',
      'tab.stats': 'Στατιστικά',
      'library.filter': 'Φίλτρο…',
      'library.noFavourites': 'Δεν υπάρχουν αγαπημένα ακόμα. Πατήστε το αστέρι σε ένα τραγούδι για να μπει εδώ.',
      'library.noHistory': 'Δεν έχει παίξει τίποτα ακόμα.',
      'library.noDownloads': 'Δεν υπάρχουν αποθηκευμένα βίντεο ακόμα. Τα επόμενα τραγούδια αποθηκεύονται εδώ καθώς κατεβαίνουν.',
      'library.plays': '{n} αναπαραγωγές',
      'downloads.summary': '{n} βίντεο · {size}',
      'downloads.delete': 'Διαγραφή',
      'queue.editSinger': 'Ορισμός τραγουδιστή',
      'stats.plays': 'Αναπαραγωγές',
      'stats.listened': 'Χρόνος ακρόασης',
      'stats.tracks': 'Διαφορετικά τραγούδια',
      'stats.favourites': 'Αγαπημένα',
      'stats.perDay': 'Αναπαραγωγές ανά ημέρα',
      'stats.topTracks': 'Πιο δημοφιλή',
      'stats.topChannels': 'Κορυφαία κανάλια',
      'stats.topSingers': 'Κορυφαίοι τραγουδιστές',
      'stats.minutes': '{n} λεπτά',
      'stats.hours': '{h}ώ {m}λ',
      'settings.group.look': 'Εμφάνιση επικάλυψης',
      'settings.group.intermission': 'Μεταξύ τραγουδιών',
      'settings.default': 'προεπιλογή',
      'settings.overlay_style': 'Στυλ επικάλυψης',
      'settings.overlay_style.bar': 'Μπάρα με κουτιά',
      'settings.overlay_style.list': 'Λίστα στη γωνία',
      'settings.overlay_label': 'Εμφάνιση για τα επόμενα',
      'settings.overlay_label.both': 'Τραγουδιστής και τίτλος',
      'settings.overlay_label.singer': 'Ονόματα τραγουδιστών',
      'settings.overlay_label.title': 'Τίτλοι τραγουδιών',
      'settings.overlay_boxes': 'Αριθμός κουτιών',
      'settings.overlay_show_timer': 'Εμφάνιση υπολειπόμενου χρόνου στη μπάρα',
      'settings.overlay_brand': 'Όνομα στη μπάρα και στο διάλειμμα',
      'settings.overlay_bar_heading': 'Τίτλος μπάρας',
      'settings.overlay_stage_heading': 'Τίτλος διαλείμματος',
      'settings.overlay_accent_color': 'Χρώμα έμφασης',
      'settings.overlay_bar_color': 'Χρώμα μπάρας',
      'settings.overlay_box_color': 'Χρώμα κουτιών',
      'settings.show_intermission': 'Κάρτα «επόμενος στη σκηνή» κατά την παύση',
      'settings.intermission_color': 'Φόντο διαλείμματος',
      'settings.open': 'Ρυθμίσεις',
      'settings.title': 'Ρυθμίσεις',
      'settings.close': 'Κλείσιμο',
      'settings.group.overlay': 'Επικάλυψη',
      'settings.group.playback': 'Αναπαραγωγή',
      'settings.group.reliability': 'Αξιοπιστία',
      'settings.show_upcoming_overlay': 'Εμφάνιση επικάλυψης «επόμενα» στο βίντεο',
      'settings.overlay_position': 'Θέση επικάλυψης',
      'settings.overlay_position.top-left': 'Πάνω αριστερά',
      'settings.overlay_position.top-center': 'Πάνω κέντρο',
      'settings.overlay_position.top-right': 'Πάνω δεξιά',
      'settings.overlay_position.bottom-left': 'Κάτω αριστερά',
      'settings.overlay_position.bottom-center': 'Κάτω κέντρο',
      'settings.overlay_position.bottom-right': 'Κάτω δεξιά',
      'settings.overlay_font_size': 'Μέγεθος γραμματοσειράς επικάλυψης',
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
      'notice.stalled':
        'Η αναπαραγωγή σταμάτησε — το YouTube δεν αποκρίνεται ({reason}). Νέα προσπάθεια σε {seconds} δευτ. ή επιλέξτε ένα τραγούδι για να δοκιμάσετε τώρα.',
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
