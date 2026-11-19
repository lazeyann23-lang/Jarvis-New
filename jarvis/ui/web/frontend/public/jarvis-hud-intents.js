/* Pure, deliberately conservative intent matcher for the JARVIS core view.
 * Only routes explicit DISPLAY requests to existing read-only cards.
 * No assistant/tool commands are executed and no utterances are persisted.
 */
(() => {
  'use strict';
  function classify(input) {
    if (typeof input !== 'string' || input.length > 800) return null;
    const text = input.toLocaleLowerCase('de-CH').replace(/\s+/g, ' ').trim();
    if (!text || text.length > 240) return null;
    const request = text.replace(/^(?:hey\s+)?jarvis[,:.!]?\s*/, '').replace(/^bitte\s+/, '');
    // A question about a topic is NOT an instruction to open a card.
    const action = /^(?:(?:kannst|könntest|koenntest|würdest|wuerdest) du (?:mir |bitte )?|ich (?:möchte|moechte|will) (?:gerne )?)?(?:zeig(?:e)?|öffne|oeffne|blend(?:e)?(?: ein)?|ruf(?:e)?|gib|hol|anzeig(?:en)?|show|open|display|bring|wechsel)(?:\s|$)/u.test(request);
    const politeDisplay = /^(?:kannst|könntest|koenntest|würdest|wuerdest) du\b/u.test(request) && /\b(?:anzeigen|zeigen|öffnen|oeffnen|aufrufen)\s*[?!.]?$/u.test(request);
    const personalQuestion = /^(?:was (?:sind|steht|ist) (?:in )?mein(?:e|em|en|er)?\s|wie (?:hoch|viel) (?:ist|sind) mein(?:e|em|en|er)?\s)/u.test(request);
    if (!action && !politeDisplay && !personalQuestion) return null;
    const match = (pattern) => pattern.test(request);
    if (match(/\b(?:aufgaben|aufgabe|tasks?|to-?dos?|taskliste|aufgabenliste)\b/u)) return 'tasks';
    if (match(/\b(?:gedächtnis|gedaechtnis|erinnerungen|memory|wissensspeicher)\b/u)) return 'memory';
    if (match(/\b(?:cpu|ram|arbeitsspeicher|prozessor|systemstatus|systemwerte|auslastung|systemdaten)\b/u)) return 'system';
    if (match(/\b(?:chat|unterhaltung|gespräch|gespraech|konversation)\b/u)) return 'chat';
    return null;
  }
  Object.defineProperty(window, 'JarvisHudIntent', {
    value: Object.freeze({ classify }), configurable: false, writable: false,
  });
})();
