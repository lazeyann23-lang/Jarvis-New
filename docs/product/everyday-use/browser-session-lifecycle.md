# Browser-Sitzungen erneuern und beenden

Ab Entwicklungsrunde 4 (21.10.2026) stehen am globalen `SurfaceSecurity`-ASGI-Schutz
zwei zusätzliche **POST**-Endpunkte zur Verfügung. Sie sind sowohl während des
Fast-Boots als auch nach dem normalen Start erreichbar:

| Aktion | Endpunkt | Ergebnis |
| --- | --- | --- |
| Sitzung erneuern | `/api/ui/session/refresh` | HTTP 204, neues HttpOnly-Sessioncookie; altes ungültig |
| Abmelden | `/api/ui/session/logout` | HTTP 204, Session serverseitig widerrufen, Cookie gelöscht |

Beide Aktionen erfordern **eine gültige Sitzung als Cookie**, eine vertrauenswürdige
`Origin`-Angabe und HTTPS oder eine direkte Loopback-Verbindung. Weder ein Bearer-
Control-Key noch der Zugriff auf die offene lokale Oberfläche genügen hier für
eine neue Browser-Sitzung. Sessions sind maximal 24 Stunden ab Ausstellung
gültig; die Verlängerung ist eine explizite neue Ausstellung. Gleichzeitige
Erneuerungsversuche mit demselben Cookie dürfen nur einmal erfolgreich sein.

Browser-Aufruf (die Cookies bleiben dank HttpOnly für JavaScript unsichtbar):

```js
await fetch('/api/ui/session/refresh', {
  method: 'POST', credentials: 'same-origin', cache: 'no-store',
});
// Abmelden analog: POST /api/ui/session/logout
```

Der Browser sollte auf 401 zurück zur Authentifizierung wechseln und auf 403
seine Origin/HTTPS-Konfiguration prüfen. Beide Endpunkte liefern `Cache-Control:
no-store`. Das Cookie wird mit `HttpOnly`, `SameSite=Strict` und über HTTPS auch
mit `Secure` ausgestellt. Die serverseitige Authentifizierung ist pro Prozess
im Arbeitsspeicher abgelegt; Neustarts beenden die Sitzungen.

**Bekannte Einschränkungen:** Bestehende WebSocket-Verbindungen werden beim
Abmelden nicht aktiv geschlossen. Bereits ausgestellte einmalige WebSocket-
Hello-Tickets bleiben bis zum Verbrauch oder dem 60-Sekunden-Ablauf gültig.
Eine durchgehende Browser-/Mobilgeräte-UI zum automatischen Erneuern und
Abmelden wird in einer späteren Entwicklungsrunde angeschlossen. Für Zugriffe
von ausserhalb des eigenen Rechners muss HTTPS korrekt eingerichtet sein;
nicht einfach einen ungeschützten JARVIS-Port ins Internet freigeben.
