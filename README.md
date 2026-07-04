# DatenArchiv – Sichere Deduplizierung & Neustrukturierung

Kommandozeilen-Anwendung (Python ≥ 3.9, Windows/Linux/macOS) zur Analyse
verschachtelter Datenbestände, Erkennung exakter Duplikate, Berichts-basierter
Bildvarianten-Analyse und reversibler Bereinigung.

## Sicherheitsgarantien

| Prinzip | Umsetzung |
|---|---|
| Kein Löschen | Duplikate werden ausschließlich in einen Quarantäne-Ordner **verschoben**; relative Pfadstruktur bleibt erhalten → vollständig reversibel (JSON-Protokoll) |
| Kein Qualitätsverlust | Als Duplikat gilt nur **byte-identisch** (BLAKE2b-Voll-Hash, RFC 7693). Bildvarianten (RAW/Druck/Web) werden nur berichtet, nie verändert |
| Wiederaufnehmbar | SQLite-Inventar; abgebrochene Läufe setzen fort, ohne bereits Gehashtes erneut zu lesen |
| Prüfbar vor Aktion | Jede schreibende Operation erfordert explizit `--ausfuehren`; `--dry-run` zeigt exakt, was geschehen würde |

## Installation

```
pip install Pillow ImageHash        # nur für Bildanalyse nötig; Rest = Standardbibliothek
```

## Arbeitsablauf (empfohlene Reihenfolge)

```
:: Phase 0 – ZWINGEND: 1:1-Sicherung der HDD, z. B.:
robocopy E:\ F:\HDD_Sicherung /MIR /R:1 /W:1 /LOG:kopie.log

:: Phase 1 – Inventar (nur lesend)
python datenarchiv.py scan E:\ --db inventar.db

:: Phase 2 – exakte Duplikate (3-stufig: Größe → Partial-Hash → Voll-Hash)
python datenarchiv.py duplikate --db inventar.db

:: Phase 2b – Bilder: EXIF, Auflösung, visuelle Varianten (nur Bericht)
python datenarchiv.py bilder --db inventar.db

:: Phase 3 – Vorschlag Zielstruktur (Fotos/JJJJ/JJJJ-MM nach EXIF-Datum)
python datenarchiv.py struktur --db inventar.db

:: Phase 4 – HTML-Report sichten (Duplikat-Gruppen, Varianten-Gruppen)
python datenarchiv.py report --db inventar.db --out report.html

:: Phase 5 – erst Probe, dann Ausführung
python datenarchiv.py quarantaene --db inventar.db --ziel E:\_QUARANTAENE --dry-run
python datenarchiv.py quarantaene --db inventar.db --ziel E:\_QUARANTAENE --ausfuehren
```

Quarantäne-Ordner nach 4–8 Wochen Bewährungszeit manuell prüfen und löschen.
Rückgängig machen: Protokoll `inventar.quarantaene.json` enthält alle
Von/Nach-Paare.

```
:: Phase 6 – bereinigte Struktur auf NEUEN Datenträger KOPIEREN
::           (Quelle bleibt vollständig unverändert; ohne --ziel: Auswahlmenü
::            aller Datenträger mit Freiplatz-Anzeige)
python datenarchiv.py kopieren --db inventar.db
python datenarchiv.py kopieren --db inventar.db --ziel G:\ --wurzel Archiv
python datenarchiv.py kopieren --db inventar.db --ziel G:\ --ausfuehren --verifizieren
```

## Phase 6 – Eigenschaften

| Merkmal | Umsetzung |
|---|---|
| Nur Kopieren | `shutil.copy2` (erhält Zeitstempel); Quelle wird nie verändert |
| RAW-Schutz | RAW-Dateien (CR2/NEF/ARW/DNG …) werden in keiner Phase geöffnet oder dekodiert – ausschließlich byte-identische Kopie |
| Urlaubsordner | Zielordner `Fotos/JJJJ/JJJJ-MM_<Ereignis>/`; Ereignis = erste nicht-generische Ordnerbezeichnung im Quellpfad (z. B. `Urlaub_Italien`, `Norwegen_Lofoten`); generische Namen (Backup, DCIM, 100CANON, Jahreszahlen …) werden übersprungen. Heuristik – vor Ausführung im Plan-CSV prüfbar |
| Freiplatz-Prüfung | Abbruch, falls Ziel < benötigtes Volumen + 5 % Reserve |
| Plan vor Aktion | Dry-Run ist Standard; vollständiger Kopierplan als CSV (`;`-getrennt, Excel-tauglich) |
| Verifikation | Größenvergleich immer; `--verifizieren` zusätzlich BLAKE2b-Hash Quelle↔Ziel je Datei |
| Wiederaufnahme | Abgebrochene Läufe einfach neu starten – bereits vorhandene Dateien (gleiche Größe) werden übersprungen |

## Original-Auswahl bei Duplikat-Gruppen

Pro Gruppe bleibt genau eine Datei erhalten. Auswahlregel (Heuristik,
anpassbar in `cmd_duplikate`): kürzester Pfad zuerst (meist die „geordnetste"
Kopie), bei Gleichstand ältester Zeitstempel.

## Google-Drive-Anbindung (empfohlen: rclone)

Eigenentwicklung der Sync-Logik ist bewusst nicht Teil dieser Anwendung –
rclone (Open Source, https://rclone.org) löst Konflikt­behandlung, Teil-Uploads
und API-Quoten ausgereift.

```
rclone config                      # einmalig: Remote "gdrive" anlegen (Typ: drive)

:: Lokale Zielstruktur nach Drive spiegeln (additiv/änderungsbasiert):
rclone sync D:\Archiv gdrive:Archiv --backup-dir gdrive:_Papierkorb/%DATE% --dry-run
rclone sync D:\Archiv gdrive:Archiv --backup-dir gdrive:_Papierkorb/%DATE%

:: Bidirektional (Vorsicht, erst nach Einlesen der Doku):
rclone bisync D:\Archiv gdrive:Archiv --resync   # nur beim ersten Lauf
```

Quellen: https://rclone.org/drive/ · https://rclone.org/commands/rclone_bisync/

## Grenzen / bekannte Einschränkungen

- Varianten-Erkennung nutzt exakten pHash-Vergleich (konservativ). Ähnliche,
  aber beschnittene/bearbeitete Bilder werden ggf. nicht gruppiert (k.A. zur
  Trefferquote ohne Testlauf auf realen Daten).
- RAW-Formate werden inventarisiert, aber nicht dekodiert (kein pHash für RAW;
  erweiterbar via `rawpy`).
- Geschätzte Laufzeit 1 TB HDD: Scan < 30 min; Voll-Hashing nur der
  Duplikat-Kandidaten typ. 1–4 h (abhängig vom Duplikat-Anteil) — Schätzwerte.
