# JARVIS – Nur Kern (Version 7)

Die neue Version enthält zusätzlich zum bisherigen Chef-Dashboard einen umschaltbaren, reduzierten Kernmodus. Die ursprüngliche Version 6 und bestehende KI-Funktionen bleiben im Projekt erhalten.

## Aktivieren

1. JARVIS wie bisher starten (`run.bat` und die reguläre Einrichtung).
2. Im Chef-Dashboard **unten rechts „NUR KERN“** auswählen.
3. Es erscheint nur der grosse animierte Kern mit **ZUHÖREN**, **STOP**, **CHAT**, **AUFGABEN**, **GEDÄCHTNIS** und **SYSTEM**.
4. Unten „DASHBOARD“ wählen, um zur grossen Ansicht zurückzukehren. Die letzte Wahl wird im Browser gespeichert.

## Einblendungen

- Chat: Das bestehende Chatfenster wird nur versetzt und **nicht dupliziert**. JARVIS-Antworten im Chat erscheinen hier, sobald die vorhandene KI sie liefert.
- Beim Übergang der laufenden Sprachsitzung in den echten Status `speaking` öffnet sich der Chat automatisch, sofern nicht bereits eine andere Karte geöffnet ist. Statusmeldungen bei `listening`, `thinking` und `speaking` blenden sich ebenfalls kurz ein.
- Aufgaben/Gedächtnis/System: Schwebende Fenster laden echte Daten aus den bestehenden, authentifizierten JARVIS-APIs und zeigen einen Fehlerhinweis, wenn das Backend nicht erreichbar ist.
- Escape oder ✕ schliesst die Karte. „ZUHÖREN“/„STOP“ benutzen die vorhandenen Voice-API-Endpunkte.

## Wichtig

Für eine funktionierende Sprachunterhaltung müssen die Sprachmodule, ein Mikrofon und der bestehende JARVIS-Sprachdienst korrekt eingerichtet sein. Diese Implementierung implementiert keine neue automatische Spracherkennung für einen Browser, auf dem keine Backend-Sprachpipeline läuft. Ebenso interpretiert die Kernansicht gesprochene Wünsche nicht selbst in Aktionen: JARVIS übernimmt dies über die vorhandene KI; kontextspezifische Popups jenseits des Chats sind derzeit manuell anwählbar. Auf iPhone und der echten Windows-App wurde diese Version noch nicht getestet.

Das Dashboard enthält dekorative Grafiken. Nur Daten aus erreichbaren APIs sind live; Offline-Werte werden nicht erfunden. Die Vorschau nutzt simulierte Testdaten.
