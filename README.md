# ha-skola24

Home Assistant custom integration för Skola24 — hämtar schemat och skapar en **kalender-entity**.

## Bakgrund

Skola24 kräver inloggning (error 9005 *"Given unit requires authorized user"*) för kommuner som Uppsala. Denna integration hanterar ASP.NET-sessionsbaserad autentisering och använder rätt `X-Scope`-header för autentiserade anrop.

| Header | Värde |
|--------|-------|
| `X-Scope` (public) | `8a22163c-8662-4535-9050-bc5e1923df48` |
| `X-Scope` (auth'd) | `a0b6c9c4-11d7-4a52-a030-a55a15058eef` |

## Installation (manuell, HAOS)

1. Kopiera mappen `custom_components/skola24/` till din HA-konfiguration:
   ```
   /config/custom_components/skola24/
   ```

2. Starta om Home Assistant.

3. Gå till **Inställningar → Enheter & tjänster → Lägg till integration → Skola24**.

4. Fyll i:
   - **Host**: `uppsala.skola24.se` (din kommuns subdomain)
   - **Användarnamn** och **Lösenord**
   - **Schematyp**: Personnummer (`ÅÅMMDD-XXXX`) eller Klass (`9A`)

## Vad skapas?

En `calendar.skola24_schema`-entity med:
- Nuvarande/nästa lektion som `event`-attribut
- Alla lektioner ±1–4 veckor synliga i HA-kalendern
- Attribut `next_events` (lista med 5 närmaste) för Lovelace-kort

## API-flöde (5 steg + auth)

```
1. GET  /Applications/Authentication/login.aspx?host=<host>
        → extrahera __VIEWSTATE m.fl. dolda fält

2. POST /Applications/Authentication/login.aspx?host=<host>
        (form-data: username, password, ASP.NET-tokens)
        → server sätter ASP.NET_SessionId, legacyuicookiestd, s24_tenant

3. POST /api/get/user/info          ← validerar sessionen
4. POST /api/services/skola24/get/timetable/viewer/units  → unitGuid
5. POST /api/get/active/school/years                      → schoolYear GUID
6. POST /api/get/timetable/render/key                     → renderKey
7. POST /api/encrypt/signature       (PIN eller klass-GUID)
8. POST /api/render/timetable        → lessonInfo[]
```

Steg 4–8 upprepas per vecka i fönstret `[idag-1v, idag+4v]`.

## Felsökning

Aktivera debug-loggning i `configuration.yaml`:
```yaml
logger:
  default: warning
  logs:
    custom_components.skola24: debug
```

Vanliga fel:
- **invalid_auth**: Fel lösenord, eller login-sidan har ändrat struktur (kolla loggen för HTML-dump).
- **9005 i loggen**: X-Scope är fel — kontrollera att `X_SCOPE_AUTH` används, inte public-scope.
- **UpdateFailed → ConfigEntryNotReady**: HA försöker igen automatiskt var 30:e sekund.

## Kända begränsningar / TODO

- [ ] Sniffa och verifiera exakt inloggnings-URL (kan skilja per kommun)
- [ ] Stöd för föräldrakonton med flera barn (listan från `/api/get/user/info`)
- [ ] Options flow för att byta klass/personnummer utan att ta bort integrationen
- [ ] HACS-kompatibelt paket (`hacs.json`)
