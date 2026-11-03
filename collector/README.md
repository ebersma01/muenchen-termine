# Termin-Sammler für die Münchner Veranstaltungs-App

Der Sammler besucht die Webseiten aus `app/data/sources.json`, liest die Termine aus und
schreibt sie nach `app/data/events.json`. Die App zeigt dann genau diese Datei an.

## Was er schreibt

| Datei | Inhalt |
|---|---|
| `app/data/events.json` | alle Termine der nächsten 6 Monate (plus laufende Ausstellungen) |
| `app/data/status.json` | je Quelle: wie viele Termine gefunden/übernommen, Fehlermeldung |
| `app/data/geocache.json` | gefundene Koordinaten für die Karte (damit jede Adresse nur einmal gesucht wird) |
| `app/data/detailcache.json` | was auf den Detailseiten der Termine stand (Ort, Adresse, Preis); wird 14 Tage wiederverwendet |
| `app/data/manual-events.json` | Termine, die man von Hand einträgt; werden immer übernommen |

## Wann er läuft

* Automatisch jeden Morgen über GitHub Actions (`.github/workflows/collect.yml`).
* Von Hand: auf GitHub unter **Actions → Termine sammeln → Run workflow**.
* Lokal zum Testen: `python collector/collect.py` (vorher `pip install -r collector/requirements.txt`
  und `python -m playwright install chromium`).
  Einzelne Quelle prüfen, ohne etwas zu speichern: `python collector/collect.py --only badw --show 10`.

## Eine Quelle hinzufügen oder entfernen

Jede Quelle ist ein Eintrag in `app/data/sources.json`:

```json
{
  "id": "literaturhaus",
  "name": "Literaturhaus München",
  "url": "https://www.literaturhaus-muenchen.de/programm/",
  "categories": ["kultur"],
  "region": "München",
  "active": true,
  "fetch": { "venue": { "name": "Literaturhaus München", "address": "Salvatorplatz 1, 80333 München" } }
}
```

* **Entfernen:** `"active": false` setzen (oder den Eintrag löschen).
* **Hinzufügen:** neuen Eintrag mit eigener `id`, Name, Link zur Terminseite und Kategorie anlegen.
  Ohne weitere Angaben versucht der Sammler, die Termine automatisch zu erkennen. Das klappt bei den
  meisten Seiten, die eine Liste mit Datum und Titel zeigen. Ob es geklappt hat, steht nach dem
  nächsten Lauf in `status.json`.

### Einstellungen im Feld `fetch` (alle freiwillig)

| Feld | Bedeutung |
|---|---|
| `type` | `cards` (Standard, normale Webseite), `jsonld` (Seite mit schema.org-Daten), `ics` (iCal-Kalender), `tribe` (WordPress-Kalender), `auto` |
| `render` | `true`, wenn die Seite ihre Termine erst per JavaScript lädt (dann wird ein unsichtbarer Browser benutzt) |
| `scope` | `local` (Standard: Quelle ist in München, nur klar auswärtige Orte fallen raus) oder `filter` (bundesweite Quelle: nur Termine, bei denen ein Ort im MVV-Gebiet genannt ist) |
| `venue` | Standard-Ort, wenn die Seite keinen Ort nennt (Name, Adresse, lat, lng) |
| `keywordsOnly` | Kategorie nur über Stichwörter aus `categories.json` bestimmen (für gemischte Kalender wie muenchen.de); passt keins, fällt der Termin weg, außer `fallbackCats` ist gesetzt |
| `mixed` | feste Kategorien der Quelle behalten und passende Stichwort-Kategorien ergänzen |
| `include` / `exclude` | Suchmuster: nur passende Termine nehmen / passende weglassen |
| `card`, `title`, `date`, `location`, `link` | CSS-Selektoren, wenn die automatische Erkennung danebenliegt |
| `titleRegex`, `titlePrefix`, `locationRegex`, `titleStrip` | Feinkorrekturen für Titel und Ort |
| `detail` | Detailseiten der Termine öffnen, um Uhrzeit und Ort zu ergänzen |
| `maxEvents` | höchstens so viele Termine dieser Quelle |
| `noDetail` | Detailseiten dieser Quelle nicht öffnen |
| `monthHeader`, `dayHeader` | Spielpläne mit Monatsüberschrift („November 2026“) und nur Tageszahl: Selektoren für Monats- und Tageskopf |
| `time` | Selektor für die Uhrzeit (auch „19.30“ ohne „Uhr“) |
| `titleFirstLine` | vom Titel-Element nur die erste Textzeile nehmen |
| `drop` | Selektor für Teile der Karte, die vorher entfernt werden (z. B. Untertitel, Raumname) |
| `titleFromDetail` | Titel von der Detailseite übernehmen (wenn die Übersicht nur Bilder zeigt); Kategorie wird danach neu bestimmt |

Nach dem Einsammeln öffnet der Sammler für jeden Termin ohne Ort, Adresse oder Preis automatisch
die verlinkte Detailseite und übernimmt dort genannte Angaben (`collector/detail.py`).
Mit `--no-detail` lässt sich dieser Schritt beim Testen abschalten.

`"fetch": false` schaltet das automatische Lesen ab (z. B. wenn eine Seite Roboter aussperrt); die
Quelle bleibt dann trotzdem in der Liste der App.

## Wie die Region bestimmt wird

Region = MVV-Gebiet, etwa 45 km um den Marienplatz. Orte werden über OpenStreetMap (Nominatim) in
Koordinaten umgewandelt; Termine weiter weg als 48 km fallen raus. Liste der Orte und der
"auswärtigen" Städte: `collector/geo.py`.
