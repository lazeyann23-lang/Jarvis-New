# JARVIS – ein Doppelklick unter Windows (V104-Quellstand)

**Status:** Diese Quelle hat einen neuen *Buildweg* für genau eine `PersonalJarvis.exe`.
Hier liegt noch **keine auf Windows gebaute oder gestartete EXE** vor.

## Automatischer Windows-Build

Aus dem **vollständigen, verifizierten** Projektstand:

1. In einem autorisierten GitHub-Repository den vollständigen Quellstand inklusive
   `.github/workflows/jarvis-onefile.yml` bereitstellen. **Nicht nur die README hochladen.**
2. Unter **Actions → JARVIS portable single-EXE (Windows x64) → Run workflow**
   den Windows-Build starten.
3. **Nur bei erfolgreichem Build** das Actions-Artefakt
   `JARVIS-Portable-OneFile-Windows-x64` herunterladen und entpacken.
4. Die darin liegende `PersonalJarvis.exe` doppelklicken.

Die Build-Maschine benötigt Internet und Python/PyInstaller; auf dem Zielcomputer
werden Python, Node.js und venv für den Kernstart nicht separat installiert.
Die vorhandene React-Oberfläche stammt aus `jarvis/ui/web/dist`; ohne diese
Build-Dateien bricht das Skript ab statt einen leeren Launcher auszuliefern.

Im OneFile-Modus entpackt PyInstaller Ressourcen temporär. Einstellungen und
Gedächtnis bleiben im Benutzerverzeichnis; die EXE muss nicht als Administrator
laufen. Einige zusätzliche Agententools, Modelle und Online-KI-Zugänge können
weiterhin eigene Downloads oder Zugangsdaten benötigen. Die Anwendung ist nicht
signiert, deshalb kann Windows eine Sicherheitswarnung anzeigen.

**Nicht geprüft:** echter Windows-Build, Windows-Start, Voice, Profilknopf,
Provider-Auswahl, Taskleistenicon. Diese Tests sind vor einer Release-Freigabe
auf Windows durchzuführen.
