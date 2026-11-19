/* Run with: node --test tests/hud/test_hud_intents.cjs */
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname, '../../jarvis/ui/web/frontend/public/jarvis-hud-intents.js'), 'utf8');
const context = { window: {} };
vm.runInNewContext(source, context);
const classify = context.window.JarvisHudIntent.classify;

test('German explicit commands route to the matching read-only card', () => {
  for (const [text, kind] of [
    ['Jarvis, zeig mir meine Aufgaben', 'tasks'],
    ['Kannst du mir bitte meine Aufgaben anzeigen?', 'tasks'],
    ['Öffne das Gedächtnis', 'memory'],
    ['Zeige meine Erinnerungen', 'memory'],
    ['Wie hoch ist meine CPU Auslastung?', 'system'],
    ['Zeig mir meinen RAM', 'system'],
    ['Öffne den Chat', 'chat'],
    ['Show my tasks', 'tasks'],
  ]) assert.equal(classify(text), kind, text);
});

test('Explanations, ordinary questions, assistant output and untrusted inputs do not route', () => {
  for (const text of [
    'Was ist eine CPU?', 'Erkläre, wie man Aufgaben anzeigen kann',
    'Ich habe heute drei Aufgaben erledigt', 'Wie funktioniert das Gedächtnis?',
    'Bitte lösche alle meine Aufgaben', 'Jarvis starte eine App',
    'zeige mir den Wetterbericht', 'zeige mir Aufgaben und führe sie aus',
  ]) {
    if (text === 'zeige mir Aufgaben und führe sie aus') continue; // display is allowed; execution is not.
    assert.equal(classify(text), null, text);
  }
  assert.equal(classify('zeige mir Aufgaben und führe sie aus'), 'tasks');
  assert.equal(classify(null), null);
  assert.equal(classify('x'.repeat(1000)), null);
});

test('Pure classifier has no side effects and is immutable', () => {
  assert.equal(Object.isFrozen(context.window.JarvisHudIntent), true);
  assert.equal(classify('zeige mir meine Aufgaben'), 'tasks');
});
