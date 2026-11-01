# München Termine

Veranstaltungskalender für München und das MVV-Gebiet, als Web-App für Browser und iPhone.

- `app/` – die Web-App (wird über GitHub Pages veröffentlicht)
- `collector/` – der Sammler, der die Quellen durchsucht und `app/data/events.json` schreibt
- `.github/workflows/collect.yml` – sammelt täglich neue Termine, auch von Hand startbar
- `.github/workflows/pages.yml` – stellt die App nach jeder Änderung online

## Termine sofort neu sammeln

GitHub → Reiter **Actions** → **Termine sammeln** → **Run workflow**. Nach einigen Minuten ist die App aktualisiert.

## Auf dem iPhone installieren

Die App-Adresse in Safari öffnen → Teilen-Symbol → **Zum Home-Bildschirm**.
