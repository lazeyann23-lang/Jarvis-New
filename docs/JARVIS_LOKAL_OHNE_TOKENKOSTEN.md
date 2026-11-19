# JARVIS ohne laufende KI-Tokenkosten (Windows)

Dieser Modus verwendet den **bereits vorhandenen Ollama-Brain**. Lokal ausgeführte Modelle verursachen **keine Anbieter-Tokenrechnung und haben keine Cloud-Ratenlimits**. Er macht das Kontextfenster aber **nicht unendlich**: RAM/VRAM, Leistung, Speicherplatz, maximale Antwortlänge und Modellfähigkeiten bleiben begrenzt.

## Einrichtung (einmalig)

1. Ollama auf dem Windows-PC installieren: https://ollama.com/download (und starten).
2. In PowerShell ein lokales Modell herunterladen, z. B. `ollama pull qwen3:4b`. Grössere Modelle benötigen mehr RAM/VRAM. Bei wenig RAM ein kleineres Modell wählen.
3. Im JARVIS-Projekt `run-local-unlimited.bat` doppelklicken. Die bisherige `run.bat` bleibt unverändert.

Vorprüfung ohne Start: `run-local-unlimited.bat --check`.
Optional gezielte Modellauswahl: `run-local-unlimited.bat --model qwen3:4b` (nur wenn lokal installiert). `:cloud`-Modelle werden abgelehnt. Die Anwendung startet ansonsten mit dem kleinsten geeigneten heruntergeladenen Modell; für Werkzeuge muss dieses Tool-Calling unterstützen.

**Achtung:** Ohne die Option `--local-voice` stellt dieser Modus nur den **KI-Brain** auf lokal. Speech-to-Text (STT), Sprachausgabe (TTS), externe Agenten, Internet-/Online-Dienste, Mobilfunk/Telefonie und integrierte APIs können weiterhin Cloud-Dienste verwenden, Limits haben oder Kosten verursachen. Nicht das ganze System ist damit automatisch kostenlos oder offline.

Für lokale Sprache: `run-local-unlimited.bat --local-voice`. Dadurch werden `faster-whisper` und `piper-local` ausgewählt. Diese benötigen zuvor installierte Abhängigkeiten und passende lokal heruntergeladene Sprach-/Stimmmodelle. Ein bewusst in der Anwendung ausgewählter STT-Anbieter kann Vorrang vor einer temporären Umgebungsvariable haben; darum die aktive STT-Anzeige prüfen. Teste vor produktiver Verwendung, ob Sprachinput und -output tatsächlich funktionieren. Für Realtime-Voice ggf. zuerst auf die normale Pipeline umstellen.

Für **24/7-Zugriff vom Handy** muss JARVIS auf laufender geeigneter Hardware gehostet werden. Ollama auf dem PC funktioniert **nicht**, wenn dieser ausgeschaltet ist; auf kleinen Raspberry-Pi-Geräten ist ein leistungsfähiges Sprach-/Toolmodell normalerweise zu langsam oder nicht lauffähig. Den ungesicherten Ollama-Port 11434 **niemals direkt ins Internet freigeben**. Die Handy-Weboberfläche ist separat abzusichern.

Der Launcher greift nur auf `127.0.0.1:11434` zu, prüft tatsächlich lokal gespeicherte Modelle, erzwingt den Ollama-Brain für die üblichen Brain-Tiers und verändert weder `jarvis.toml` noch Benutzer-Systemvariablen dauerhaft. Für externe Spezialfunktionen/Worker existiert keine universelle Garantie, dass alle Cloud-Aufrufe vermieden werden.
