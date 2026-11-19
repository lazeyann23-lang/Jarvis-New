# JARVIS Chef Dashboard – Version 6

Dieses Paket baut auf **JARVIS Dodaios Version 5 (lokale KI)** auf.
Beim Start mit `run.bat` erscheint nach erfolgreicher Anmeldung und der
Ersteinrichtung automatisch das blaue JARVIS-Chef-Dashboard.

## Start auf Windows

1. Das komplette ZIP in einen neuen Ordner entpacken (nicht nur einzelne Dateien kopieren).
2. Falls JARVIS bisher noch nicht eingerichtet ist: zunächst Python-Abhängigkeiten
   anhand der mitgelieferten README installieren und die Ersteinrichtung abschliessen.
3. `run.bat` öffnen. Die eingebettete Desktopoberfläche lädt aus `jarvis/ui/web/dist`.
4. Unten/rechts über **Klassische Ansicht** kannst du zur bisherigen Oberfläche wechseln.
   Beim nächsten normalen Start erscheint das Dashboard wieder.
5. Alternativ öffnen: `http://127.0.0.1:<eingestellter Port>/?jarvis_hud=off`
   (nur mit dem zuvor eingerichteten Zugangsverfahren).

Die Standard-Startseite benutzt die vorhandene React-App als sichere Grundlage;
der Chat rechts öffnet dieselbe Anwendung in der Solo-Ansicht. Dadurch bleiben
Chatverlauf, Berechtigungen und die bestehenden KI-Modelle erhalten.

## Was ist echt?

- Live-CPU/RAM werden alle 8 Sekunden über `/api/hud/metrics` abgerufen.
  Falls `psutil` nicht verfügbar ist, erscheint `–` statt erfundener Werte.
- Die Anzahl der Aufgaben kommt aus `/api/tasks`; Status aus `/api/voice/status`.
- Das Chatfenster nutzt den existierenden JARVIS-Chat und dessen Sprachknopf.
- **Zuhören**, **Stop**, **Handy**, **Gedächtnis** und die Navigation sind
  angebunden (Handy öffnet die vorhandene Telefonie-Einrichtung, kein automatischer Fernzugriff).
- Der Weltglobus, das Analyse-Diagramm und der blaue Kern sind
  **illustrative Designelemente**, keine Satelliten- oder Wetterdaten.

## Aktualisierungen und Einschränkungen

- Die neuen Assets liegen sowohl im ausgelieferten `dist/` als auch im
  `frontend/public/`-Ordner, damit ein späteres Vite-Rebuild sie übernehmen kann.
- Der Screenshot ist eine **Browser-Designprüfung mit simulierten Testdaten**.
- Ein echter Windows-PyWebView-Lauf, Mikrofonzugriff, iPhone-Fernsteuerung
  und vollständige Backend-Abläufe konnten in dieser Umgebung nicht geprüft werden.
- Diese ZIP ersetzt nicht automatisch eine bereits installierte Windows-EXE.
  Eine EXE müsste erneut aus diesem Code erstellt werden.
- Beim eingebetteten Chat wird eine zusätzliche Ansicht geladen. Für Systeme
  mit sehr wenig RAM ist die **klassische Ansicht** ggf. ressourcenschonender.
- Die vorhandene Versions-5-Ollama-Integration wurde nicht verändert.
