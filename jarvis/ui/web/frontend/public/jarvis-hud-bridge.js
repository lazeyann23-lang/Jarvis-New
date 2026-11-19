/* JARVIS HUD event bridge for the prebuilt frontend bundle.
 * Runs ONLY in the same-origin, embedded solo chat. A source-level React
 * emitter also exists and takes precedence after a future frontend rebuild.
 * Does not inspect the DOM, send requests, or forward credentials.
 */
(() => {
  'use strict';
  const params = new URLSearchParams(location.search);
  if (params.get('solo') !== '1' || params.get('jarvis_hud') !== 'off' || window.parent === window) return;
  try { if (window.parent.location.origin !== location.origin) return; } catch { return; }
  const emit = text => {
    if (typeof text !== 'string' || !text.trim() || text.length > 800) return;
    window.parent.postMessage({ type:'jarvis:hud:user-intent:v1', text }, location.origin);
  };
  const originalFetch = window.fetch;
  if (typeof originalFetch === 'function') {
    window.fetch = function(input, init) {
      const result = Reflect.apply(originalFetch, this, arguments);
      try {
        const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input?.url;
        const method = (init?.method || (typeof input === 'object' && input?.method) || 'GET').toUpperCase();
        const path = new URL(url, location.href);
        if (method === 'POST' && path.origin === location.origin &&
            /^\/api\/agent-chat\/sessions\/[^/]+\/messages$/.test(path.pathname) &&
            typeof init?.body === 'string') {
          const body = JSON.parse(init.body);
          if (typeof body?.text === 'string') {
            Promise.resolve(result).then(response => { if (response?.ok) emit(body.text); }).catch(() => {});
          }
        }
      } catch { /* Never change the application's request behavior. */ }
      return result;
    };
  }
  const OriginalSocket = window.WebSocket;
  if (typeof OriginalSocket === 'function') {
    window.WebSocket = new Proxy(OriginalSocket, {
      construct(target, args, newTarget) {
        const socket = Reflect.construct(target, args, newTarget);
        // Only the main assistant event stream; no coding, mission or third-party WS.
        try {
          const url = new URL(String(args[0]), location.href);
          const expectedProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
          if (url.protocol !== expectedProtocol || url.host !== location.host ||
              !/^\/ws(?:\/|$)/.test(url.pathname)) return socket;
          socket.addEventListener('message', event => {
            if (typeof event.data !== 'string' || event.data.length > 128000) return;
            try {
              const envelope = JSON.parse(event.data);
              const p = envelope?.payload;
              if (envelope?.event_name === 'TranscriptFinal') emit(p?.transcript?.text);
              else if (envelope?.event_name === 'TranscriptionUpdate' && p?.is_final === true) emit(p.text);
              else if (envelope?.event_name === 'MessageSent' && p?.role === 'user') emit(p.text);
            } catch { /* Non-event frames, ping and binary are ignored. */ }
          });
        } catch { /* Do not interfere with connection creation. */ }
        return socket;
      },
    });
  }
})();
