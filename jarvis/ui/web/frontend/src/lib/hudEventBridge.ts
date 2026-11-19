/**
 * Explicit, one-way, same-origin UI event from the real assistant to the
 * optional JARVIS HUD. This does not execute tools, expose credentials, or
 * broadcast a conversation to other tabs/windows.
 */
export function emitHudUserIntent(text: unknown): void {
  if (typeof text !== "string" || !text.trim() || text.length > 800) return;
  if (typeof window === "undefined" || window.parent === window) return;
  // Only the embedded, first-party chat may publish a HUD display request.
  const params = new URLSearchParams(window.location.search);
  if (params.get("solo") !== "1" || params.get("jarvis_hud") !== "off") return;
  try {
    if (window.parent.location.origin !== window.location.origin) return;
    window.parent.postMessage(
      { type: "jarvis:hud:user-intent:v1", text }, window.location.origin,
    );
  } catch {
    // A cross-origin parent must never receive chat content.
  }
}
