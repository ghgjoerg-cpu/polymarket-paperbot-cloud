# Schritt 26 – GitHub Cloud Paper Bot

Dieser Ordner ist die kostenlose Cloud-Version deines Paper-Bots.

## Was läuft in der Cloud?

Aktuell:
- Tennis
- WNBA
- UFC

Nicht enthalten:
- echte Orders
- Wallet
- API-Trading
- Umgehung von Geoblocking
- Fußball 1X2 aus deinem lokalen Spezial-Runner

Der Cloud-Runner ist absichtlich konservativ und macht nur Paper-Signale.

## Was passiert automatisch?

GitHub Actions startet den Bot alle 30 Minuten und aktualisiert:

- `docs/index.html`
- `docs/report.json`
- `docs/history.jsonl`
- `docs/last_run.txt`

Wenn GitHub Pages aktiviert ist, kannst du `docs/index.html` als iPhone-Dashboard öffnen.

## Schritt-für-Schritt

### 1. Neues GitHub Repository erstellen

1. Auf GitHub einloggen.
2. Neues Repository erstellen.
3. Name zum Beispiel:
   `polymarket-paperbot-cloud`
4. Privat oder öffentlich ist egal.
5. Dieses ZIP entpacken.
6. Alle Dateien und Ordner aus dem ZIP in das Repository hochladen.

Wichtig:
Der Ordner `.github/workflows/` muss mit hochgeladen werden.

### 2. ODDS_API_KEY als Secret speichern

1. Repository öffnen.
2. Settings öffnen.
3. Secrets and variables → Actions.
4. New repository secret.
5. Name:
   `ODDS_API_KEY`
6. Value:
   deinen Odds-API-Key einfügen.
7. Speichern.

Niemals den Key in eine Datei schreiben.

### 3. Workflow manuell testen

1. Repository öffnen.
2. Tab `Actions`.
3. Workflow `Polymarket Paper Bot Cloud` auswählen.
4. `Run workflow` anklicken.
5. Warten, bis der Lauf grün ist.

Wenn der Lauf fehlschlägt:
- Prüfen, ob `ODDS_API_KEY` exakt so heißt.
- Prüfen, ob alle Dateien hochgeladen wurden.

### 4. GitHub Pages aktivieren

1. Repository Settings öffnen.
2. Pages öffnen.
3. Build and deployment:
   - Source: Deploy from a branch
   - Branch: main
   - Folder: /docs
4. Save.
5. Nach kurzer Zeit erscheint dort eine URL.
6. Diese URL am iPhone in Safari öffnen.
7. Teilen-Symbol → Zum Home-Bildschirm.

### 5. Tägliche Nutzung

Auf dem iPhone nur noch Dashboard öffnen.

Wenn dort steht:

`ALLES RUHIG`

machst du nichts.

Wenn dort steht:

`PAPER_BUY GEFUNDEN`

kopierst du `docs/report.json` oder einen Screenshot/Bericht hier rein.

## Kosten / Quota

Der Workflow läuft standardmäßig alle 30 Minuten.

Das sind bis zu 48 Läufe pro Tag.

Wenn deine Odds-API-Quota zu schnell fällt:
- Workflow-Datei öffnen:
  `.github/workflows/polymarket-paperbot.yml`
- Cron ändern von:
  `*/30 * * * *`
- auf zum Beispiel:
  `0 * * * *`

Dann läuft er nur noch einmal pro Stunde.

## Wichtig

Keine echten Orders.
Nur Paper-Trading.
Deutschland-Geoblocking wird nicht umgangen.
Dieser Runner nutzt nur öffentliche Marktdaten und Buchmacherquoten.
