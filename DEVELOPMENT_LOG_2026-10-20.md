# JARVIS – Entwicklungsdurchlauf 1 (20.10.2026, 23:00 Schweizer Zeit)

## Änderung
Die Wiki-Gedächtnisfunktion blockiert nun zusätzliche Zugangsdaten, bevor sie auf Datenträger und in den Suchindex gelangen können. Ergänzt wurden Erkennungsmuster für GitHub-Fine-Grained-Tokens, GitLab-PATs, npm-Tokens, Slack-Tokens, AWS-Access-Key-IDs, Passwörter mit Leerzeichen in Anführungszeichen und verschlüsselte/PGP-Private-Key-Header. Die Diagnose meldet weiterhin ausschliesslich Musternamen statt Zugangsdaten.

Geänderte Dateien:
- `jarvis/memory/wiki/secret_guard.py`
- `tests/unit/memory/wiki/test_secret_guard.py`
- `tests/unit/memory/wiki/test_secret_guard_writer_integration.py` (neu)
- `tests/unit/memory/wiki/test_secret_guard_security_regression.py` (neu)

## Drei tatsächlich ausgeführte Testgruppen
1. **Funktionstest:** 30 parametrische Unit-Tests für erlaubte und gesperrte Inhalte – **30 bestanden**.
2. **Integrationstest:** Zwei Tests mit dem echten `AtomicWriter`: Zugangsdaten gelangen weder in Datei noch Backup und erscheinen nicht im Log; normale Wiki-Seite wird geschrieben – **2 bestanden**.
3. **Sicherheits-/Regressionstest:** Diagnose ohne Geheimniswert, normaler Text inklusive Git-SHA bleibt erlaubt, grosse harmlose Notiz verarbeitet sich ohne Hänger – **3 bestanden**.

**Gesamt: 35 Tests bestanden.** Die Tests wurden mit einer isolierten Importumgebung ausgeführt, die schwere Paket-Initialisierung umgeht; der echte `AtomicWriter` wurde dabei geladen und ausgeführt. Ein vollständiger Projekttest war nicht möglich, weil in der Laufzeitumgebung unter anderem `aiosqlite` fehlt. Die Anwendung wurde nicht als Windows-EXE oder auf einem iPhone getestet.

## Offene Risiken
- Regex-Muster können nicht sämtliche Zugangsdaten erkennen; sie ersetzen keine Verschlüsselung oder Zugriffsprüfung.
- Neue Muster können legitime Notizen fälschlich blockieren. Diese werden als gesperrt gemeldet, nicht still verworfen.
- Es gibt noch keinen verbundenen GitHub-Branch für fortlaufende versionierte Änderungen.

## Nächster Entwicklungsschritt
Sichere Remote-Verbindungen und Widerruf/Überprüfung der Handy-Steuerung mit tatsächlichen End-to-End-Tests verbessern.
