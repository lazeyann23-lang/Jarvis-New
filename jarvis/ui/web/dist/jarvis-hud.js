/* JARVIS Chef HUD — visual desktop home, layered over the existing authorized app.
 * Runs after AuthGate/onboarding. The actual assistant still runs in the native
 * ChatsSurface, inside a same-origin solo iframe; no parallel chat backend.
 * All numeric telemetry comes from the server. Unavailable data is '–'.
 */
(() => {
  'use strict';
  const params = new URLSearchParams(location.search);
  if (params.get('jarvis_hud') === 'off' || params.get('solo') === '1' || (params.get('view') && params.get('view') !== 'chats') || params.has('onboarding') || params.has('tour')) return;
  const $ = (selector, root = document) => root.querySelector(selector);
  const navigate = (view) => { location.assign('/?view=' + encodeURIComponent(view) + '&jarvis_hud=off'); };
  const root = $('#root');
  if (!root) return;
  let panel = null;
  let started = false;
  let polling = null;
  let clock = null;
  let coreOnly = false;
  let lastVoicePhase = null;
  let voiceNoticeTimer = null;
  let intentFrame = null;
  let lastIntent = null;
  let popupSource = null;
  let popupGeneration = 0;
  let closeLivePanel = null;
  const corePreference = 'jarvis:core-only:v1';
  try { coreOnly = localStorage.getItem(corePreference) === 'true'; } catch { /* private mode */ }
  const stop = () => {
    closeLivePanel?.();
    if (polling) clearInterval(polling);
    if (clock) clearInterval(clock);
    if (voiceNoticeTimer) clearTimeout(voiceNoticeTimer);
    window.removeEventListener('message', onHudMessage);
    panel?.remove();
    panel = null;
    document.body.classList.remove('jarvis-chef-hud-active');
  };
  const safeFetch = async (url, options) => {
    const res = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  };
  function markup() {
    return `
<div class="jh-shell" role="application" aria-label="JARVIS Chef Dashboard">
  <header class="jh-top">
    <div class="jh-brand"><span class="jh-brand-title">J A R V I S</span><span>PERSONAL AI ASSISTANT</span></div>
    <div class="jh-top-centre"><span>INTELLIGENCE &nbsp; / &nbsp; ASSISTANCE &nbsp; / &nbsp; AUTOMATION &nbsp; / &nbsp; SECURITY</span><strong>KERN-VORSCHAU <i class="jh-dot"></i> <span id="jh-voice-text">BEREIT</span> <i class="jh-dot"></i> CHEF MODE</strong></div>
    <div class="jh-time"><span id="jh-date">–</span><strong id="jh-clock">–</strong><span><b id="jh-online-dot" class="jh-status-dot"></b> <span id="jh-server-status">VERBINDE</span></span></div>
  </header>
  <div class="jh-layout">
    <aside class="jh-left">
      <section class="jh-card jh-scan"><h2>GLOBAL SCAN <span>◇</span></h2><div class="jh-scan-content"><img src="/jarvis-hud-earth.webp" alt="Holografische Weltkugel, Illustration" /><div class="jh-scan-lines"><span>◎ &nbsp; NETZWERK</span><strong id="jh-network">–</strong><span>◇ &nbsp; KI-VERBINDUNG</span><strong id="jh-connection">Prüfe …</strong><span>◉ &nbsp; STANDORT</span><strong>Lokaler PC</strong></div></div><div class="jh-tabs"><button type="button" data-go="computers">GERÄTE</button><button type="button" data-go="agents">AGENTEN</button><button type="button" data-go="memory">WISSEN</button></div></section>
      <section class="jh-card jh-systems"><h2>SYSTEME <span>•••</span></h2>
        <div class="jh-meter"><span>▣ CPU</span><div><b id="jh-cpu-bar"></b></div><strong id="jh-cpu">–</strong></div>
        <div class="jh-meter"><span>▤ RAM</span><div><b id="jh-ram-bar"></b></div><strong id="jh-ram">–</strong></div>
        <div class="jh-meter"><span>◈ KI-MODELL</span><div><b id="jh-ai-bar"></b></div><strong id="jh-ai">–</strong></div>
        <div class="jh-meter"><span>✧ SPRACHE</span><div><b id="jh-voice-bar"></b></div><strong id="jh-speech">–</strong></div>
      </section>
      <section class="jh-card jh-vitals"><h2>VITALS <span>⋯</span></h2><div class="jh-vital"><span>PROZESSOR</span><strong id="jh-v-cpu">–</strong><i class="jh-spark"></i></div><div class="jh-vital"><span>ARBEITSSPEICHER</span><strong id="jh-v-ram">–</strong><i class="jh-spark"></i></div><div class="jh-vital"><span>PLATTFORM</span><strong id="jh-platform">–</strong></div><div class="jh-caption">Live-Werte nur bei aktiver Verbindung</div></section>
    </aside>
    <main class="jh-stage" aria-label="Interaktiver JARVIS-Kern">
      <div class="jh-kern-label">SYSTEM ONLINE <span>●</span> PERSONAL ASSISTANT</div>
      <div class="jh-core-wrap" id="jh-core" data-state="idle"><div class="jh-core-outer"></div><div class="jh-core-inner"></div><img class="jh-core-art" src="/jarvis-hud-core.webp" alt="Blau leuchtender futuristischer JARVIS-Kern" /><div class="jh-cross jh-cross-v"></div><div class="jh-cross jh-cross-h"></div><div class="jh-core-label"><strong>J A R V I S</strong><span id="jh-core-state">BEREIT</span></div></div>
      <div class="jh-voice-box"><div class="jh-micro-icon">◉</div><div><div class="jh-wave" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div><strong id="jh-prompt">WIE KANN ICH DIR HEUTE HELFEN?</strong></div></div>
      <div class="jh-core-dock" aria-label="Kern-Schnellzugriff"><button type="button" id="jh-core-listen">◉ &nbsp; ZUHÖREN</button><button type="button" id="jh-core-stop">■ &nbsp; STOP</button><button type="button" data-core-open="chat">◇ &nbsp; CHAT</button><button type="button" data-core-open="tasks">☷ &nbsp; AUFGABEN</button><button type="button" data-core-open="memory">▧ &nbsp; GEDÄCHTNIS</button><button type="button" data-core-open="system">◈ &nbsp; SYSTEM</button><button type="button" data-core-open="desktop">PC-STEUERUNG</button><button type="button" data-core-open="subagents">SUBAGENTEN</button></div>
      <div id="jh-core-notice" class="jh-core-notice" role="status" aria-live="polite" hidden></div>
    </main>
    <aside class="jh-center-right"><section class="jh-card jh-analysis"><h2>ANALYSE <span>LIVE</span></h2><div class="jh-analysis-label">SYSTEMZUSTAND / DIAGNOSE</div><div class="jh-chart"><svg viewBox="0 0 320 140" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="jh-l" x1="0" x2="0" y1="0" y2="1"><stop stop-color="#0fe2ff" stop-opacity="0.38"/><stop offset="1" stop-color="#078eff" stop-opacity="0"/></linearGradient></defs><path d="M0 108 L30 80 55 94 87 41 115 71 149 28 181 63 216 14 248 59 280 30 320 62 L320 140 L0 140 Z" fill="url(#jh-l)"/><path d="M0 108 L30 80 55 94 87 41 115 71 149 28 181 63 216 14 248 59 280 30 320 62" fill="none" stroke="#26baff" stroke-width="2"/><path d="M0 120 L48 110 91 87 119 100 163 53 201 78 241 39 290 69 320 54" fill="none" stroke="#006bb4" stroke-width="1.5"/></svg></div><div class="jh-kv"><span>BACKEND</span><b id="jh-backend-label">Prüfe Verbindung</b></div><div class="jh-kv"><span>SPRACHMODUS</span><b id="jh-state-label">–</b></div><div class="jh-kv"><span>DATENQUELLE</span><b>Lokaler Rechner</b></div></section>
      <section class="jh-card jh-environment"><h2>UMGEBUNG <span>●</span></h2><div class="jh-env-inner"><span class="jh-env-icon">◉</span><div><strong id="jh-env-main">JARVIS LOKAL</strong><span id="jh-env-sub">Sicher verbunden, wenn Backend online</span></div></div><div class="jh-kv"><span>BEDIENUNG</span><b>Windows / Browser</b></div><div class="jh-kv"><span>MODELLE</span><b>In Einstellungen</b></div><div class="jh-kv"><span>SPRACHE</span><b>Über Live-Chat</b></div></section>
      <section class="jh-card jh-communication"><h2>KOMMUNIKATION <span>•••</span></h2><div class="jh-orbits"></div><div class="jh-kv"><span>DIENSTE</span><b id="jh-service">Prüfe</b></div><div class="jh-kv"><span>FERNSTEUERUNG</span><b>Nur nach Einrichtung</b></div><div class="jh-kv"><span>GEDÄCHTNIS</span><button type="button" data-go="memory">ÖFFNEN →</button></div></section></aside>
    <aside class="jh-right" aria-label="JARVIS Steuerung"><div class="jh-right-brand"><h1>J A R V I S</h1><p>INTELLIGENCE &nbsp; / &nbsp; CHEF EDITION</p><div class="jh-right-state"><span><i class="jh-status-dot"></i> KERN-VORSCHAU</span><span>● ICH SPRECHE</span><span>◇ CHEF</span></div></div>
      <div class="jh-stat-row"><div><span>CPU</span><strong id="jh-cpu-small">–</strong><small>▂▅▃▆▂▃▅</small></div><div><span>RAM</span><strong id="jh-ram-small">–</strong><small>▂▃▅▂▃▆▅</small></div><div><span>AUFGABEN</span><strong id="jh-task-small">–</strong><small>▁▃▂▆▄▂▃</small></div></div>
      <nav class="jh-right-tabs" aria-label="Steuerungsbereich"><button type="button" class="selected" data-tab="chat">CHAT</button><button type="button" data-tab="desktop">PC-STEUERUNG</button><button type="button" data-tab="subagents">SUBAGENTEN</button><button type="button" data-tab="tasks">AUFGABEN</button><button type="button" data-tab="memory">GEDÄCHTNIS</button><button type="button" data-tab="alerts">MELDUNGEN</button></nav>
      <div class="jh-right-content"><div id="jh-chat-area" class="jh-chat-area"><iframe id="jh-chat-frame" title="Echter JARVIS-Chat mit Sprache" src="/?view=chats&solo=1&jarvis_hud=off"></iframe></div><div id="jh-information" class="jh-information" hidden></div></div>
      <div class="jh-right-actions"><button type="button" id="jh-full-chat">↗ CHAT IM VOLLBILD</button><div class="jh-controls"><button type="button" id="jh-mic">♬ &nbsp; ZUHÖREN</button><button type="button" id="jh-stop">■ &nbsp; STOP</button><button type="button" data-go="telephony">▯ &nbsp; HANDY</button></div><div class="jh-controls jh-secondary"><button type="button" id="jh-hide">◎ &nbsp; KLASSISCHE ANSICHT</button><button type="button" data-go="settings">⚙ &nbsp; EINSTELLUNGEN</button></div></div>
      <footer class="jh-right-foot">DESKTOP-OBERFLÄCHE · LIVE-CHAT<br/>Systemwerte sind echt, wenn verfügbar. Die Grafiken sind dekorativ.</footer>
    </aside>
  </div>
  <section id="jh-core-popup" class="jh-core-popup" role="dialog" aria-modal="false" aria-labelledby="jh-popup-title" hidden><header><strong id="jh-popup-title">JARVIS</strong><button type="button" id="jh-popup-close" aria-label="Fenster schliessen">✕</button></header><div id="jh-popup-body" class="jh-popup-body"></div></section>
  <nav class="jh-bottom" aria-label="JARVIS Navigation"><button type="button" class="active" data-tab="chat">◈ <span>DASHBOARD</span></button><button type="button" data-go="docs">⌕ <span>RECHERCHE</span></button><button type="button" data-tab="tasks">☷ <span>AUFGABEN</span></button><button type="button" data-go="sessions">▦ <span>VERLÄUFE</span></button><button type="button" data-go="computers">▣ <span>GERÄTE</span></button><button type="button" data-go="memory">◇ <span>GEDÄCHTNIS</span></button><button type="button" data-go="agents">✧ <span>AGENTEN</span></button><button type="button" data-go="settings">⚙ <span>SYSTEM</span></button><button type="button" id="jh-mode-switch" aria-pressed="false">◎ <span>NUR KERN</span></button></nav>
</div>`;
  }
  function value(id, text) { const el = $('#' + id, panel); if (el) el.textContent = text; }
  function percent(id, n) { const el = $('#' + id, panel); if (el) el.style.width = n === null ? '0%' : Math.max(0, Math.min(100, n)) + '%'; }
  function notice(message, duration = 4600) {
    if (!panel || !coreOnly) return;
    const toast = $('#jh-core-notice', panel);
    if (!toast) return;
    if (voiceNoticeTimer) clearTimeout(voiceNoticeTimer);
    toast.textContent = message;
    toast.hidden = false;
    voiceNoticeTimer = setTimeout(() => { if (toast) toast.hidden = true; }, duration);
  }
  function closeCorePopup() {
    if (!panel) return;
    if (closeLivePanel && $('#jh-popup-body .jh-control-panel', panel)) {
      closeLivePanel();
    }
    popupGeneration += 1;
    popupSource = null;
    const popup = $('#jh-core-popup', panel);
    panel.classList.remove('jh-core-chat-open');
    popup?.classList.remove('jh-chat-popup');
    if (popup) popup.hidden = true;
    $('#jh-popup-body', panel)?.replaceChildren();
  }
  function setCoreMode(enabled) {
    if (!panel) return;
    if (!enabled) closeCorePopup();
    if (enabled && $('#jh-information .jh-control-panel', panel)) showTab('chat');
    coreOnly = enabled;
    panel.classList.toggle('jh-core-only', enabled);
    const mode = $('#jh-mode-switch', panel);
    if (mode) {
      mode.setAttribute('aria-pressed', String(enabled));
      const title = $('span', mode);
      if (title) title.textContent = enabled ? 'DASHBOARD' : 'NUR KERN';
    }
    const toast = $('#jh-core-notice', panel);
    if (toast && !enabled) toast.hidden = true;
    try { localStorage.setItem(corePreference, String(enabled)); } catch { /* private mode */ }
  }
  function textLine(parent, className, content) {
    const el = document.createElement('div');
    el.className = className;
    el.textContent = content;
    parent.appendChild(el);
    return el;
  }
  function extractItems(data, field) {
    if (Array.isArray(data)) return data;
    if (!data || typeof data !== 'object') return [];
    return Array.isArray(data[field]) ? data[field] : Array.isArray(data.items) ? data.items : [];
  }
  async function openCorePopup(kind, source = 'manual') {
    if (!panel || !coreOnly) return;
    if (source === 'auto' && popupSource === 'manual' && !$('#jh-core-popup', panel)?.hidden) return;
    closeCorePopup();
    popupSource = source;
    const generation = popupGeneration;
    const popup = $('#jh-core-popup', panel);
    const body = $('#jh-popup-body', panel);
    if (!popup || !body) return;
    const labels = {chat:'LIVE-CHAT',tasks:'MEINE AUFGABEN',memory:'GEDÄCHTNIS',system:'SYSTEMSTATUS',desktop:'PC-STEUERUNG',subagents:'SUBAGENTEN'};
    value('jh-popup-title', labels[kind] || 'JARVIS');
    popup.hidden = false;
    if (kind === 'desktop') {
      mountDesktopPanel(body);
      return;
    }
    if (kind === 'subagents') {
      mountSubagentsPanel(body);
      return;
    }
    if (kind === 'chat') {
      showTab('chat');
      const chat = $('#jh-chat-area', panel);
      if (chat) {
        chat.hidden = false;
        // CSS positions the existing iframe as a pop-out without reparenting it.
        // Moving an iframe DOM node silently reloads it and loses chat state.
        panel.classList.add('jh-core-chat-open');
        popup.classList.add('jh-chat-popup');
      } else textLine(body, 'jh-popup-info', 'Der Chat ist momentan nicht verfügbar.');
      return;
    }
    textLine(body, 'jh-popup-info', 'Lade aktuelle Informationen …');
    try {
      let result;
      if (kind === 'tasks') result = await safeFetch('/api/tasks?limit=8');
      else if (kind === 'memory') result = await safeFetch('/api/wiki/index');
      else result = await safeFetch('/api/hud/metrics');
      if (!panel || popup.hidden || generation !== popupGeneration || $('#jh-popup-title', panel)?.textContent !== labels[kind]) return;
      body.replaceChildren();
      if (kind === 'system') {
        const display = v => typeof v === 'number' && Number.isFinite(v) ? v + '%' : '–';
        textLine(body, 'jh-popup-row', 'CPU: ' + display(result.cpu_percent));
        textLine(body, 'jh-popup-row', 'RAM: ' + display(result.ram_percent));
        textLine(body, 'jh-popup-row', 'Plattform: ' + String(result.platform || 'Nicht verfügbar'));
        return;
      }
      if (kind === 'memory') { void addCaptureControl(body); addForgetControl(body, result.pages || []); }
      const items = extractItems(result, kind === 'tasks' ? 'tasks' : 'pages');
      if (!items.length) textLine(body, 'jh-popup-info', 'Keine Einträge verfügbar.');
      items.slice(0, 8).forEach(item => {
        const label = typeof item === 'string' ? item : String(item?.title || item?.name || item?.path || item?.id || 'Eintrag');
        textLine(body, 'jh-popup-row', label.slice(0, 240));
      });
    } catch {
      if (popup.hidden || generation !== popupGeneration) return;
      body.replaceChildren();
      textLine(body, 'jh-popup-info', 'Keine Verbindung zu diesen Daten. Öffne den regulären Chat für Details.');
    }
  }
  // React/source and the shipped runtime emit a typed, same-origin event.
  // The parent never scrapes chat DOM, reloads the iframe, or accepts messages
  // from unrelated frames. The text is used only for read-only UI routing.
  function onUserUtterance(text) {
    if (!coreOnly || !panel || !window.JarvisHudIntent) return;
    const kind = window.JarvisHudIntent.classify(text);
    if (!kind) return;
    const now = Date.now();
    if (lastIntent && lastIntent.text === text && now - lastIntent.at < 12000) return;
    lastIntent = { text, kind, at: now };
    void openCorePopup(kind, 'auto');
    notice('JARVIS · ' + ({ tasks:'AUFGABEN', memory:'GEDÄCHTNIS', system:'SYSTEM', chat:'CHAT' }[kind]) + ' ÖFFNEN');
  }
  function onHudMessage(event) {
    if (!panel || !intentFrame || event.origin !== location.origin ||
        event.source !== intentFrame.contentWindow) return;
    const payload = event.data;
    if (!payload || typeof payload !== 'object' ||
        payload.type !== 'jarvis:hud:user-intent:v1' ||
        typeof payload.text !== 'string' || payload.text.length > 800) return;
    onUserUtterance(payload.text);
  }
  function installIntentBridge() {
    intentFrame = $('#jh-chat-frame', panel);
    if (intentFrame) window.addEventListener('message', onHudMessage);
  }
  // V12: a real, persisted user choice; never guess or simulate its state.
  async function addCaptureControl(host) {
    if (!host || !host.isConnected) return;
    const box = document.createElement('div');
    box.className = 'jh-capture-control';
    const label = document.createElement('span');
    label.textContent = 'Sprach-/Chat-Fakten speichern: prüfe …';
    const button = document.createElement('button');
    button.type = 'button';
    button.disabled = true;
    button.textContent = 'LÄDT';
    const note = document.createElement('small');
    note.textContent = 'Nur bestätigte Sprach-/Chat-Erinnerungen. Bereits gespeicherte Daten bleiben bestehen.';
    box.append(label, button, note);
    host.appendChild(box);
    const render = (enabled) => {
      label.textContent = enabled ? 'Sprach-/Chat-Fakten speichern: EIN' : 'Sprach-/Chat-Fakten speichern: PAUSIERT';
      button.textContent = enabled ? 'PAUSIEREN' : 'FORTSETZEN';
      button.disabled = false;
    };
    try {
      const initial = await safeFetch('/api/wiki/capture-policy');
      if (!box.isConnected || typeof initial.enabled !== 'boolean') return;
      render(initial.enabled);
      button.addEventListener('click', async () => {
        button.disabled = true;
        const desired = button.textContent === 'PAUSIEREN' ? false : true;
        try {
          const saved = await safeFetch('/api/wiki/capture-policy', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: desired })
          });
          if (typeof saved.enabled !== 'boolean') throw new Error('Ungültige Antwort');
          if (box.isConnected) render(saved.enabled);
        } catch {
          label.textContent = 'Änderung fehlgeschlagen. Bitte erneut versuchen.';
          button.disabled = false;
        }
      });
    } catch {
      label.textContent = 'Speicher-Einstellung nicht erreichbar';
      button.textContent = 'NICHT VERFÜGBAR';
    }
  }
  // V13: one knowledge page at a time; preview and typed confirmation are
  // mandatory. This never claims to erase chat logs or external backups.
  function addForgetControl(host, pages) {
    if (!host || !host.isConnected) return;
    const eligible = Array.isArray(pages) ? pages.filter(p =>
      p && p.group === 'knowledge' && typeof p.path === 'string' &&
      /^(entities|projects|concepts)\/[^/]+\.md$/.test(p.path)) : [];
    const box = document.createElement('section');
    box.className = 'jh-forget-control';
    const title = document.createElement('strong');
    title.textContent = 'EINZELNE ERINNERUNG LÖSCHEN';
    const note = document.createElement('small');
    note.textContent = 'Nur ausgewählte Wissensseite. Chatverläufe, externe Kopien und andere Erinnerungen bleiben möglicherweise bestehen.';
    const select = document.createElement('select');
    select.setAttribute('aria-label', 'Wissensseite zum Löschen auswählen');
    eligible.forEach(page => {
      const option = document.createElement('option');
      option.value = page.path;
      option.textContent = (page.title || page.path) + ' — ' + page.path;
      select.appendChild(option);
    });
    const previewButton = document.createElement('button');
    previewButton.type = 'button';
    previewButton.textContent = 'LÖSCHUNG PRÜFEN';
    previewButton.disabled = !eligible.length;
    const details = document.createElement('p');
    details.textContent = eligible.length ? 'Zuerst eine Vorschau anfordern.' : 'Keine löschbaren Wissensseiten gefunden.';
    const confirmInput = document.createElement('input');
    confirmInput.type = 'text';
    confirmInput.placeholder = 'Bestätigung hier eingeben';
    confirmInput.setAttribute('aria-label', 'Exakte Löschbestätigung');
    confirmInput.hidden = true;
    const confirmButton = document.createElement('button');
    confirmButton.type = 'button';
    confirmButton.textContent = 'ENDGÜLTIG LÖSCHEN';
    confirmButton.hidden = true;
    confirmButton.disabled = true;
    box.append(title, note, select, previewButton, details, confirmInput, confirmButton);
    host.appendChild(box);
    let token = null;
    let required = '';
    const reset = () => {
      token = null;
      required = '';
      confirmInput.hidden = true;
      confirmInput.value = '';
      confirmButton.hidden = true;
      confirmButton.disabled = true;
    };
    select.addEventListener('change', () => { reset(); details.textContent = 'Zuerst eine neue Vorschau anfordern.'; });
    confirmInput.addEventListener('input', () => {
      confirmButton.disabled = !token || confirmInput.value !== required;
    });
    previewButton.addEventListener('click', async () => {
      reset();
      previewButton.disabled = true;
      details.textContent = 'Prüfe Index, Journal und lokale Backups …';
      try {
        const result = await safeFetch('/api/wiki/pages/forget/preview', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({path: select.value})
        });
        if (!box.isConnected || select.value !== result.path) return;
        token = result.preview_token;
        required = result.confirmation_required;
        const counts = result.index_rows || {};
        details.textContent = 'Seite: ' + result.path + ' | Suchindex: ' +
          Object.values(counts).reduce((a,b)=>a+(Number(b)||0),0) +
          ' Einträge | Journal: ' + (result.journal_candidates || 0) +
          ' | lokale Backups: ' + (result.managed_backup_archives || []).length +
          '. Andere Kopien können bestehen bleiben. Zum Bestätigen genau eingeben: ' + required;
        confirmInput.hidden = false;
        confirmButton.hidden = false;
      } catch {
        details.textContent = 'Vorschau nicht möglich. Es wurde nichts gelöscht.';
      } finally {
        previewButton.disabled = !eligible.length;
      }
    });
    confirmButton.addEventListener('click', async () => {
      if (!token || confirmInput.value !== required) return;
      confirmButton.disabled = true;
      try {
        const result = await safeFetch('/api/wiki/pages/forget', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({path: select.value, preview_token: token, confirmation: required})
        });
        if (!result.ok || !result.page_removed) throw new Error('Nicht vollständig gelöscht');
        details.textContent = 'Seite gelöscht, zugehörige aktive Index-/Journal-Einträge und ' +
          result.backup_archives_cleaned + ' lokale Backup-Archive bereinigt. ' +
          'Andere Kopien oder Gesprächsverläufe können weiterhin existieren.';
        const removed = select.selectedOptions[0];
        if (removed) removed.remove();
        if (!select.options.length) previewButton.disabled = true;
      } catch {
        details.textContent = 'Löschung nicht bestätigt. Bitte Status prüfen und neu vorschauen.';
      } finally { reset(); }
    });
  }
  function mountDesktopPanel(host) {
    closeLivePanel?.();
    const life = new AbortController();
    const card = document.createElement('section');
    card.className = 'jh-control-panel';
    card.innerHTML = `
      <form>
        <label for="jh-desktop-goal">Auftrag für diesen PC</label>
        <textarea id="jh-desktop-goal" rows="3" maxlength="2000" required disabled
          placeholder="Öffne den Taschenrechner und berechne 27 × 14"></textarea>
        <p>Beim Start darf JARVIS für diesen Auftrag den Bildschirm prüfen und Maus und Tastatur bedienen.</p>
        <div class="jh-control-actions">
          <button type="submit" disabled>Auftrag starten</button>
          <button type="button">PC-Aufträge stoppen</button>
        </div>
      </form>
      <p data-status role="status">Prüfe PC-Steuerung …</p>
      <p data-feedback role="status"></p>
      <ul aria-label="Letzte PC-Aufträge"></ul>`;
    host.appendChild(card);
    const form = $('form', card);
    const goal = $('textarea', card);
    const start = $('button[type="submit"]', card);
    const stopJobs = $('button[type="button"]', card);
    const status = $('[data-status]', card);
    const feedback = $('[data-feedback]', card);
    const history = $('ul', card);
    const endpoint = '/api/computer-use/goals';
    const statusLabels = {
      queued: 'Wartet', running: 'Läuft', finished: 'Abgeschlossen',
      cancelled: 'Gestoppt', timeout: 'Zeitlimit erreicht', error: 'Fehlgeschlagen',
    };
    let timer;
    let busy = false;
    let revision = 0;
    const request = (url, options) => safeFetch(url, {
      signal: AbortSignal.any([life.signal, AbortSignal.timeout(8000)]),
      ...options,
    });
    const updateStatus = (text) => {
      if (status.textContent !== text) status.textContent = text;
    };
    async function refreshJobs() {
      clearTimeout(timer);
      const current = ++revision;
      try {
        if (document.hidden || busy) return;
        const data = await request(endpoint + '?limit=8');
        if (life.signal.aborted || current !== revision) return;
        const available = data.available === true;
        goal.disabled = start.disabled = !available;
        updateStatus(data.active
          ? (data.active === 1 ? 'Ein PC-Auftrag ist aktiv.' : `${data.active} PC-Aufträge sind aktiv.`)
            + ' Esc stoppt die laufende Bedienung.'
          : available
            ? 'PC-Steuerung bereit.'
            : 'PC-Steuerung inaktiv. Wähle in den Einstellungen ein Modell für die Computersteuerung.');
        history.replaceChildren();
        for (const run of data.runs) {
          const item = document.createElement('li');
          textLine(item, 'jh-desktop-run', `${statusLabels[run.status] || 'Unbekannt'} · ${run.goal}`);
          if (run.result_text) textLine(item, 'jh-desktop-result', run.result_text);
          history.appendChild(item);
        }
      } catch {
        if (life.signal.aborted || current !== revision) return;
        goal.disabled = start.disabled = true;
        updateStatus('Status nicht erreichbar.');
      } finally {
        if (!life.signal.aborted && current === revision) {
          timer = setTimeout(refreshJobs, 2000);
        }
      }
    }
    async function send(url, body, success, failure) {
      if (busy) return;
      busy = true;
      revision += 1;
      start.disabled = stopJobs.disabled = true;
      feedback.textContent = '';
      try {
        const response = await request(url, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        feedback.textContent = success(response);
      } catch (error) {
        if (!life.signal.aborted) {
          feedback.textContent = error.message === 'HTTP 409'
            ? 'Dieser Auftrag ist bereits aktiv.'
            : failure;
        }
      } finally {
        busy = false;
        stopJobs.disabled = false;
        if (!life.signal.aborted) void refreshJobs();
      }
    }
    form.addEventListener('submit', event => {
      event.preventDefault();
      const text = goal.value.trim();
      if (!text || start.disabled) return;
      void send(endpoint, { goal: text }, () => {
        if (goal.value.trim() === text) goal.value = '';
        return 'Auftrag angenommen.';
      }, 'Der Start wurde nicht bestätigt. Prüfe die Auftragsliste.');
    });
    stopJobs.addEventListener('click', () => {
      void send(endpoint + '/cancel-all', {}, response => {
        if (response.cancelled === 1) return 'Stopp für einen PC-Auftrag angefordert.';
        if (response.cancelled) return `Stopp für ${response.cancelled} PC-Aufträge angefordert.`;
        return 'Keine aktiven PC-Aufträge gefunden.';
      },
      'Der Stopp wurde nicht bestätigt. Drücke Esc, um die laufende Bedienung zu beenden.');
    });
    closeLivePanel = () => {
      clearTimeout(timer);
      life.abort();
      card.remove();
      closeLivePanel = null;
    };
    void refreshJobs();
  }
  function mountSubagentsPanel(host) {
    closeLivePanel?.();
    const life = new AbortController();
    const card = document.createElement('section');
    card.className = 'jh-control-panel';
    card.innerHTML = `
      <form>
        <label for="jh-subagent-task">Auftrag für JARVIS und seine Subagenten</label>
        <textarea id="jh-subagent-task" rows="3" maxlength="2400" required
          placeholder="Vergleiche parallel Vor- und Nachteile von drei Backup-Strategien."></textarea>
        <p>JARVIS kann unabhängige Teilaufgaben delegieren. Dafür muss ein Agent eingerichtet sein.</p>
        <button type="submit" disabled>Auftrag delegieren</button>
      </form>
      <label for="jh-subagent-mission">Letzte delegierte Aufträge</label>
      <select id="jh-subagent-mission" disabled></select>
      <p data-state role="status">Verbinde mit dem Auftragsdienst …</p>
      <p data-feedback role="status"></p>
      <ul aria-label="Subagenten des ausgewählten Auftrags"></ul>
      <div class="jh-control-actions">
        <button type="button" data-cancel disabled>Auftrag abbrechen</button>
        <button type="button" data-go="agents">Agenten öffnen</button>
      </div>`;
    host.appendChild(card);
    const endpoint = '/api/missions';
    const form = $('form', card);
    const input = $('textarea', card);
    const submit = $('button[type="submit"]', card);
    const selection = $('select', card);
    const cancel = $('[data-cancel]', card);
    const state = $('[data-state]', card);
    const feedback = $('[data-feedback]', card);
    const children = $('ul', card);
    const missionStates = {
      PENDING: 'Wartet', RUNNING: 'Läuft', CRITIQUING: 'Ergebnis wird geprüft', LOOPING: 'Überarbeitung',
      APPROVED: 'Abgeschlossen', FAILED: 'Fehlgeschlagen', CANCELLED: 'Abgebrochen',
      TIMED_OUT: 'Zeitlimit erreicht', WAITING_CAPACITY: 'Wartet auf Anbieter',
    };
    const childStates = {
      running: 'Läuft', finished: 'Ergebnis liegt vor', cancelled: 'Abgebrochen',
      timed_out: 'Zeitlimit erreicht', failed: 'Fehlgeschlagen', unknown: 'Abschluss offen',
    };
    let selectedId = '';
    let timer;
    let revision = 0;
    let busy = false;
    let optionsKey = '';
    const request = (path, options) => safeFetch(path, {
      signal: AbortSignal.any([life.signal, AbortSignal.timeout(8000)]),
      ...options,
    });
    const writeStatus = (line) => {
      if (state.textContent !== line) state.textContent = line;
    };
    function updateOptions(missions) {
      const key = JSON.stringify(missions.map(({id, prompt}) => [id, prompt]));
      if (key !== optionsKey) {
        optionsKey = key;
        selection.replaceChildren(...missions.map(({id, prompt}) => new Option(prompt, id)));
      }
      if (!missions.some(mission => mission.id === selectedId)) selectedId = missions[0]?.id || '';
      selection.value = selectedId;
      selection.disabled = missions.length === 0;
    }
    async function refreshMissions() {
      clearTimeout(timer);
      const current = ++revision;
      const isCurrent = () => current === revision && !life.signal.aborted;
      try {
        if (document.hidden || busy) return;
        const {missions} = await request(endpoint + '?subagents=true&limit=12');
        if (!isCurrent()) return;
        submit.disabled = false;
        updateOptions(missions);
        if (!selectedId) {
          cancel.disabled = true;
          children.replaceChildren();
          writeStatus('Noch keine delegierten Aufträge.');
          return;
        }
        const details = await request(endpoint + '/' + encodeURIComponent(selectedId) + '/subagents');
        if (!isCurrent()) return;
        cancel.disabled = !details.can_cancel;
        writeStatus((missionStates[details.state] || details.state) +
          (details.subagents.length ? '' : ' · Noch keine Subagenten gestartet.'));
        children.replaceChildren(...details.subagents.map((child) => {
          const row = document.createElement('li');
          row.textContent = `Subagent ${child.number} · ${childStates[child.state] || 'Status offen'}`;
          return row;
        }));
      } catch {
        if (isCurrent()) {
          cancel.disabled = submit.disabled = true;
          children.replaceChildren();
          writeStatus('Auftragsstatus nicht erreichbar.');
        }
      } finally {
        if (isCurrent()) timer = setTimeout(refreshMissions, 3000);
      }
    }
    async function changeMission(path, body, confirmChange, failure) {
      if (busy) return;
      busy = true;
      revision += 1;
      submit.disabled = cancel.disabled = selection.disabled = true;
      feedback.textContent = '';
      try {
        const result = await request(path, {
          method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
        });
        if (!life.signal.aborted) feedback.textContent = confirmChange(result);
      } catch (error) {
        if (!life.signal.aborted) feedback.textContent = failure(error);
      } finally {
        busy = false;
        if (!life.signal.aborted) void refreshMissions();
      }
    }
    selection.addEventListener('change', () => {
      selectedId = selection.value;
      cancel.disabled = true;
      children.replaceChildren();
      writeStatus('Lade Auftragsstatus …');
      void refreshMissions();
    });
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const prompt = input.value.trim();
      if (!prompt || submit.disabled) return;
      void changeMission(endpoint + '/dispatch', {prompt, language:'de', use_subagents:true}, (result) => {
        selectedId = result.mission_id;
        if (input.value.trim() === prompt) input.value = '';
        return result.started === 'true' ? 'Auftrag angenommen.' : 'Auftrag gespeichert. Agentenstart nicht verfügbar.';
      }, (error) => error.message === 'HTTP 409'
        ? 'Dieser Auftrag braucht eine Bestätigung in der Agenten-Ansicht.'
        : 'Der Start wurde nicht bestätigt. Prüfe die Auftragsliste.');
    });
    cancel.addEventListener('click', () => {
      if (!selectedId || cancel.disabled) return;
      void changeMission(endpoint + '/' + encodeURIComponent(selectedId) + '/cancel', {},
        () => 'Auftrag abgebrochen.', () => 'Der Abbruch wurde nicht bestätigt.');
    });
    closeLivePanel = () => {
      life.abort();
      clearTimeout(timer);
      card.remove();
      closeLivePanel = null;
    };
    void refreshMissions();
  }
  function showTab(tab) {
    if (!panel) return;
    closeLivePanel?.();
    panel.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('selected', b.dataset.tab === tab));
    const chat = $('#jh-chat-area', panel);
    const info = $('#jh-information', panel);
    chat.hidden = tab !== 'chat';
    info.hidden = tab === 'chat';
    if (tab === 'chat') return;
    info.replaceChildren();
    const title = document.createElement('h3');
    title.textContent = { tasks: 'AUFGABEN', memory: 'GEDÄCHTNIS', alerts: 'SYSTEMMELDUNGEN', desktop: 'PC-STEUERUNG', subagents: 'SUBAGENTEN' }[tab] || 'JARVIS';
    info.appendChild(title);
    if (tab === 'desktop') {
      mountDesktopPanel(info);
      return;
    }
    if (tab === 'subagents') {
      mountSubagentsPanel(info);
      return;
    }
    if (tab === 'memory') void addCaptureControl(info);
    const loading = document.createElement('p'); loading.textContent = 'Lade Live-Daten …'; info.appendChild(loading);
    const paths = {tasks: '/api/tasks', memory: '/api/wiki/index', alerts: '/api/health'};
    safeFetch(paths[tab] || '/api/health').then(data => {
      if (!panel || info.hidden || !loading.isConnected) return;
      loading.remove();
      let items;
      if (tab === 'tasks') items = Array.isArray(data) ? data : (data.tasks || data.items || []);
      else if (tab === 'memory') items = data.pages || data.items || [];
      else items = [{title:'Backend: ' + (data.status || (data.ok ? 'online' : 'antwortet'))}];
      if (!Array.isArray(items)) items = [];
      if (tab === 'memory') addForgetControl(info, items);
      if (!items.length) { const empty=document.createElement('p'); empty.textContent='Keine Einträge verfügbar.'; info.appendChild(empty); }
      items.slice(0,12).forEach(item => {
        const row=document.createElement('div');row.className='jh-info-row';
        row.textContent= typeof item==='string' ? item : String(item.title || item.name || item.path || item.id || 'Eintrag');
        info.appendChild(row);
      });
      const action=document.createElement('button');action.type='button';action.textContent='IM JARVIS ÖFFNEN →';
      action.addEventListener('click',()=>navigate(tab === 'tasks' ? 'agents' : tab === 'memory' ? 'memory' : 'board'));
      info.appendChild(action);
    }).catch(() => { loading.textContent='Diese Daten sind derzeit nicht erreichbar. Öffne die klassische Ansicht für Details.'; });
  }
  async function refresh() {
    if (!panel || document.hidden) return;
    try {
      const data=await safeFetch('/api/hud/metrics');
      value('jh-cpu',data.cpu_percent == null?'–':data.cpu_percent+'%');
      value('jh-ram',data.ram_percent == null?'–':data.ram_percent+'%');
      value('jh-cpu-small',data.cpu_percent == null?'–':data.cpu_percent+'%');
      value('jh-ram-small',data.ram_percent == null?'–':data.ram_percent+'%');
      value('jh-v-cpu',data.cpu_percent == null?'–':data.cpu_percent+'%');
      value('jh-v-ram',data.ram_percent == null?'–':data.ram_percent+'%');
      value('jh-task-small',data.tasks == null?'–':String(data.tasks));
      value('jh-platform',data.platform || '–');
      value('jh-server-status','BACKEND ONLINE');value('jh-network','VERBUNDEN');value('jh-connection','ONLINE');
      value('jh-backend-label','ONLINE');value('jh-service','ERREICHBAR');
      percent('jh-cpu-bar',data.cpu_percent);percent('jh-ram-bar',data.ram_percent);
      $('#jh-online-dot',panel)?.classList.add('online');
    } catch {
      value('jh-server-status','KEINE VERBINDUNG');value('jh-backend-label','OFFLINE');
      $('#jh-online-dot',panel)?.classList.remove('online');
    }
    try {
      const taskList=await safeFetch('/api/tasks?limit=100');
      value('jh-task-small',typeof taskList.total === 'number' ? String(taskList.total) : '–');
    } catch { value('jh-task-small','–'); }
    try {
      const voice=await safeFetch('/api/voice/status');
      const ready=voice.ready === true;
      value('jh-speech',ready?'BEREIT':'–');percent('jh-voice-bar',ready?100:null);
      value('jh-ai','–');percent('jh-ai-bar',null); // voice readiness is not proof that a model is loaded
    } catch {value('jh-speech','–');value('jh-ai','–');}
    try {
      const state=await safeFetch('/api/voice/state');
      const status=String(state.voice_state || state.state || state.status || 'idle').toLowerCase();
      const labels={idle:'BEREIT',listening:'ICH HÖRE',thinking:'DENKE NACH',speaking:'ICH SPRECHE',error:'FEHLER'};
      const label=labels[status] || status.slice(0,24).toUpperCase();
      value('jh-voice-text',label);value('jh-core-state',label);value('jh-state-label',label);
      const core=$('#jh-core',panel);if(core)core.dataset.state=status;
      if (status !== lastVoicePhase) {
        if (lastVoicePhase !== null && ['listening','thinking','speaking'].includes(status)) notice('JARVIS · '+label);
        // When a real voice session starts replying, pop out the existing chat,
        // but never interrupt a card the user is already viewing.
        if (lastVoicePhase !== null && status === 'speaking' && coreOnly && $('#jh-core-popup', panel)?.hidden) {
          const recent = lastIntent && Date.now() - lastIntent.at < 15000 ? lastIntent.kind : 'chat';
          void openCorePopup(recent, 'auto');
        }
        lastVoicePhase = status;
      }
    }catch {/* Voice state is optional; never manufacture a state */}
  }
  function tick() { const d=new Date();value('jh-clock',new Intl.DateTimeFormat('de-CH',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d));value('jh-date',new Intl.DateTimeFormat('de-CH',{weekday:'short',day:'numeric',month:'short',year:'numeric'}).format(d).toUpperCase()); }
  async function voiceAction(url) {
    const response=await fetch(url,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:'{}'});
    if(!response.ok) throw new Error('HTTP '+response.status);
    const data=await response.json();
    if ((url.endsWith('/call') && data.armed === false) || (url.endsWith('/hangup') && data.stopped === false)) throw new Error('voice unavailable');
    return data;
  }
  function mount() {
    if(panel) return;
    panel=document.createElement('div');panel.id='jarvis-chef-hud';panel.innerHTML=markup();document.body.appendChild(panel);
    document.body.classList.add('jarvis-chef-hud-active');
    panel.addEventListener('click', async event => {
      const target=event.target instanceof Element?event.target.closest('button'):null;
      if(!target) return;
      const go=target.getAttribute('data-go');const tab=target.getAttribute('data-tab');
      const coreOpen=target.getAttribute('data-core-open');
      if(target.id==='jh-mode-switch'){setCoreMode(!coreOnly);return;}
      if(target.id==='jh-popup-close'){closeCorePopup();return;}
      if(coreOpen){await openCorePopup(coreOpen);return;}
      if(go){navigate(go);return;}if(tab){showTab(tab);return;}
      if(target.id==='jh-hide'){location.assign('/?view=chats&jarvis_hud=off');return;}
      if(target.id==='jh-full-chat'){navigate('chats');return;}
      const isStart=['jh-mic','jh-core-listen'].includes(target.id);
      if(isStart || ['jh-stop','jh-core-stop'].includes(target.id)){
        target.disabled=true;
        try{
          await voiceAction(isStart?'/api/voice/call':'/api/voice/hangup');
          if(coreOnly) notice(isStart?'SPRACHSTART ANGEFORDERT · ICH HÖRE, SOBALD VERBUNDEN':'STOP ANGEFORDERT');
          void refresh();
        } catch {
          value('jh-prompt','SPRACHE NICHT VERFÜGBAR – CHAT NUTZEN');
          notice('SPRACHE NICHT VERFÜGBAR · ÖFFNE CHAT');
        } finally {target.disabled=false;}
      }
    });
    setCoreMode(coreOnly);
    installIntentBridge();
    document.addEventListener('keydown', event => { if(event.key === 'Escape' && panel && !$('#jh-core-popup', panel)?.hidden) closeCorePopup(); });
    tick();clock=setInterval(tick,1000);void refresh();polling=setInterval(()=>void refresh(),8000);
  }
  async function ready() {
    if(started || !document.querySelector('[data-testid="home-view"]')) return;
    // Do not obscure initial setup, EULA, guided tour or the authentication gate.
    try {
      const state=await safeFetch('/api/onboarding/state');
      if(state.completed !== true || state.tour_completed === false) return;
      if(!document.querySelector('[data-testid="home-view"]')) return;
      started=true;mount();
    } catch { /* Leave the native app visible when status is unavailable */ }
  }
  const check=setInterval(()=>{if(started){clearInterval(check);return;} void ready();},600);
  window.addEventListener('pagehide',()=>{clearInterval(check);stop();},{once:true});
})();
