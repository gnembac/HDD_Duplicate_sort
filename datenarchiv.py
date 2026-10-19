#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DatenArchiv – Analyse, Deduplizierung und Neustrukturierung von Datenbeständen
==============================================================================
Sicherheitsprinzipien (nicht verhandelbar):
  1. Standardmodus ist NUR LESEND (analyse/report).
  2. Es wird NIEMALS gelöscht – Duplikate werden in einen Quarantäne-Ordner
     VERSCHOBEN (Originalpfad bleibt als relative Struktur erhalten, dadurch
     vollständig reversibel).
  3. Nur byte-identische Dateien (BLAKE2b-Voll-Hash) gelten als Duplikat.
     Bildvarianten unterschiedlicher Qualität (RAW/Druck/Web) werden nur
     BERICHTET, nie angetastet.

Aufrufbeispiele:
  python datenarchiv.py scan       E:\\            --db inventar.db
  python datenarchiv.py duplikate  --db inventar.db
  python datenarchiv.py bilder     --db inventar.db          (benötigt: pip install Pillow ImageHash)
  python datenarchiv.py struktur   --db inventar.db
  python datenarchiv.py report     --db inventar.db --out report.html
  python datenarchiv.py quarantaene --db inventar.db --ziel E:\\_QUARANTAENE --dry-run
  python datenarchiv.py quarantaene --db inventar.db --ziel E:\\_QUARANTAENE --ausfuehren

Abhängigkeiten: Python >= 3.9 (Standardbibliothek). Optional: Pillow, ImageHash.
"""

from __future__ import annotations
import argparse, hashlib, os, sqlite3, sys, time, shutil, html, json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ----------------------------- Konstanten -----------------------------------

PARTIAL_BYTES = 64 * 1024          # Partial-Hash: erste 64 KiB
HASH_ALGO = "blake2b"              # RFC 7693

BILD_EXT   = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif",
              ".heic", ".heif", ".webp"}
RAW_EXT    = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".dng", ".orf",
              ".rw2", ".raf", ".pef", ".srw"}
VIDEO_EXT  = {".mp4", ".mov", ".avi", ".mkv", ".mts", ".m2ts", ".wmv", ".3gp"}
DOKU_EXT   = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
              ".odt", ".ods", ".odp", ".txt", ".rtf", ".csv", ".md"}
AUDIO_EXT  = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".wma"}
ARCHIV_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"}

# Qualitäts-Rangfolge für Bildformate (kleiner = hochwertiger).
# HEURISTIK ("geschätzt") – dient nur der Sortierung im Report.
FORMAT_RANG = {**{e: 0 for e in RAW_EXT},
               ".tif": 1, ".tiff": 1, ".png": 2, ".heic": 3, ".heif": 3,
               ".webp": 4, ".jpg": 5, ".jpeg": 5, ".bmp": 6, ".gif": 7}

def kategorie(ext: str) -> str:
    e = ext.lower()
    if e in RAW_EXT:    return "Bild-RAW"
    if e in BILD_EXT:   return "Bild"
    if e in VIDEO_EXT:  return "Video"
    if e in DOKU_EXT:   return "Dokument"
    if e in AUDIO_EXT:  return "Audio"
    if e in ARCHIV_EXT: return "Archiv"
    return "Sonstiges"

ZIEL_ORDNER = {"Bild-RAW": "Fotos-RAW", "Bild": "Fotos", "Video": "Videos",
               "Dokument": "Dokumente", "Audio": "Musik",
               "Archiv": "Archive", "Sonstiges": "Sonstiges"}

# Generische Ordnernamen, die KEIN Ereignis/Urlaubsziel bezeichnen (Heuristik)
import re
GENERISCH = {"backup", "backups", "sicherung", "sicherungen", "kopie", "neu",
             "alt", "bilder", "fotos", "photos", "images", "pictures", "dcim",
             "camera", "kamera", "eigene bilder", "eigene dateien", "downloads",
             "desktop", "dokumente", "documents", "video", "videos", "misc",
             "sonstiges", "temp", "tmp", "export", "import", "originale",
             "original", "jpg", "jpeg", "raw", "scans", "handy", "smartphone"}
_RE_GENERISCH = re.compile(
    r"^(\d{1,4}"                    # reine Zahlen / Jahre
    r"|\d{4}[-_. ]?\d{0,2}[-_. ]?\d{0,2}"   # Datumsmuster 2019-07(-14)
    r"|backup.*|sicherung.*"
    r"|\d{3}[a-z_]+"                # Kamera-Ordner: 100CANON, 101MSDCF …
    r"|img.*|dsc.*)$", re.IGNORECASE)
_RE_UNGUELTIG = re.compile(r'[<>:"/\\|?*\x00-\x1f]')   # Windows-verbotene Zeichen

def ereignis_label(pfad: str, quelle: str | None) -> str | None:
    """Ermittelt aus dem Quellpfad die aussagekräftigste Ordnerbezeichnung
    (z. B. 'Urlaub_Italien', 'Norwegen_2018') für die Zielstruktur.
    Sucht vom tiefsten zum obersten Ordner den ersten nicht-generischen Namen.
    Heuristik ('geschätzt') – im Plan-CSV vor dem Kopieren prüfbar."""
    try:
        teile = Path(pfad).parent.relative_to(quelle).parts if quelle else \
                Path(pfad).parent.parts
    except (ValueError, TypeError):
        teile = Path(pfad).parent.parts
    for name in reversed(teile):
        n = name.strip()
        if len(n) < 3:                          continue
        if n.lower() in GENERISCH:              continue
        if _RE_GENERISCH.match(n):              continue
        return _RE_UNGUELTIG.sub("_", n).strip(" .")[:60]
    return None

# ----------------------------- Datenbank ------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS dateien(
  id INTEGER PRIMARY KEY,
  pfad TEXT UNIQUE NOT NULL,
  groesse INTEGER NOT NULL,
  mtime REAL NOT NULL,
  ext TEXT, kategorie TEXT,
  hash_partial TEXT, hash_voll TEXT,
  bild_breite INTEGER, bild_hoehe INTEGER,
  exif_datum TEXT, phash TEXT,
  status TEXT DEFAULT ''            -- '', 'duplikat', 'original', 'verschoben'
);
CREATE INDEX IF NOT EXISTS ix_groesse ON dateien(groesse);
CREATE INDEX IF NOT EXISTS ix_hp ON dateien(hash_partial);
CREATE INDEX IF NOT EXISTS ix_hv ON dateien(hash_voll);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

def db_open(pfad: str) -> sqlite3.Connection:
    con = sqlite3.connect(pfad)
    con.executescript(SCHEMA)
    return con

# ----------------------------- Hilfsfunktionen -------------------------------

def mb(n): return f"{n/1_048_576:,.1f} MB".replace(",", ".")
def gb(n): return f"{n/1_073_741_824:,.2f} GB".replace(",", ".")

def datei_hash(pfad: str, nur_partial: bool) -> str | None:
    try:
        h = hashlib.new(HASH_ALGO)
        with open(pfad, "rb") as f:
            if nur_partial:
                h.update(f.read(PARTIAL_BYTES))
            else:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(block)
        return h.hexdigest()
    except OSError as e:
        print(f"  ! Lesefehler: {pfad} ({e})", file=sys.stderr)
        return None

def fortschritt(i, n, t0, einheit="Dateien"):
    if i % 200 == 0 or i == n:
        dt = time.time() - t0
        rate = i / dt if dt > 0 else 0
        print(f"\r  {i}/{n} {einheit} ({rate:,.0f}/s)", end="", flush=True)

# ----------------------------- Phase 1: Scan --------------------------------

def cmd_scan(args):
    quelle = Path(args.quelle).resolve()
    if not quelle.exists():
        sys.exit(f"Quelle nicht gefunden: {quelle}")
    con = db_open(args.db)
    con.execute("INSERT OR REPLACE INTO meta VALUES('quelle',?)", (str(quelle),))
    print(f"Scanne (nur lesend): {quelle}")
    t0, n, vol = time.time(), 0, 0
    batch = []
    for wurzel, dirs, dateien in os.walk(quelle):
        # Quarantäne- und Systemordner überspringen
        dirs[:] = [d for d in dirs if not d.startswith(("_QUARANTAENE",
                    "$RECYCLE.BIN", "System Volume Information", ".Trash"))]
        for name in dateien:
            p = Path(wurzel) / name
            try:
                st = p.stat()
            except OSError:
                continue
            ext = p.suffix.lower()
            batch.append((str(p), st.st_size, st.st_mtime, ext, kategorie(ext)))
            n += 1; vol += st.st_size
            if len(batch) >= 1000:
                con.executemany("""INSERT OR REPLACE INTO dateien
                    (pfad,groesse,mtime,ext,kategorie) VALUES(?,?,?,?,?)""", batch)
                con.commit(); batch.clear()
            if n % 1000 == 0:
                print(f"\r  {n} Dateien, {gb(vol)}", end="", flush=True)
    if batch:
        con.executemany("""INSERT OR REPLACE INTO dateien
            (pfad,groesse,mtime,ext,kategorie) VALUES(?,?,?,?,?)""", batch)
    con.commit()
    print(f"\nFertig: {n} Dateien, {gb(vol)} in {time.time()-t0:,.0f} s "
          f"→ Inventar: {args.db}")

# ------------------------ Phase 2: Exakt-Duplikate ---------------------------

def cmd_duplikate(args):
    con = db_open(args.db)
    # Stufe 1: nur Größen mit >1 Vorkommen sind Kandidaten
    kand = con.execute("""SELECT groesse FROM dateien WHERE groesse>0
                          GROUP BY groesse HAVING COUNT(*)>1""").fetchall()
    groessen = [g for (g,) in kand]
    print(f"Stufe 1 (Größe):   {len(groessen)} Größen-Gruppen als Kandidaten")

    # Stufe 2: Partial-Hash nur für Kandidaten
    t0, i = time.time(), 0
    zeilen = con.execute(f"""SELECT id,pfad FROM dateien
        WHERE groesse IN ({",".join("?"*len(groessen))})
        AND (hash_partial IS NULL OR hash_partial='')""",
        groessen).fetchall() if groessen else []
    for fid, pfad in zeilen:
        i += 1; fortschritt(i, len(zeilen), t0)
        con.execute("UPDATE dateien SET hash_partial=? WHERE id=?",
                    (datei_hash(pfad, True), fid))
        if i % 500 == 0: con.commit()
    con.commit()
    print(f"\nStufe 2 (Partial): {i} Dateien gehasht")

    # Stufe 3: Voll-Hash nur wo (groesse, hash_partial) mehrfach
    zeilen = con.execute("""
        SELECT d.id,d.pfad FROM dateien d
        JOIN (SELECT groesse,hash_partial FROM dateien
              WHERE hash_partial IS NOT NULL AND hash_partial!=''
              GROUP BY groesse,hash_partial HAVING COUNT(*)>1) g
        ON d.groesse=g.groesse AND d.hash_partial=g.hash_partial
        WHERE d.hash_voll IS NULL OR d.hash_voll=''""").fetchall()
    t0, i = time.time(), 0
    for fid, pfad in zeilen:
        i += 1; fortschritt(i, len(zeilen), t0)
        con.execute("UPDATE dateien SET hash_voll=? WHERE id=?",
                    (datei_hash(pfad, False), fid))
        if i % 200 == 0: con.commit()
    con.commit()
    print(f"\nStufe 3 (Voll-Hash BLAKE2b): {i} Dateien gehasht")

    # Duplikat-Gruppen markieren: pro Gruppe bleibt 1 Original.
    # Original-Wahl: kürzester Pfad (meist "geordnetste" Kopie), dann ältestes mtime.
    con.execute("UPDATE dateien SET status='' WHERE status IN('duplikat','original')")
    gruppen = con.execute("""SELECT hash_voll FROM dateien
        WHERE hash_voll IS NOT NULL AND hash_voll!=''
        GROUP BY hash_voll HAVING COUNT(*)>1""").fetchall()
    einsparung, dupl = 0, 0
    for (hv,) in gruppen:
        rows = con.execute("""SELECT id,pfad,groesse,mtime FROM dateien
                              WHERE hash_voll=? ORDER BY LENGTH(pfad),mtime""",
                           (hv,)).fetchall()
        con.execute("UPDATE dateien SET status='original' WHERE id=?", (rows[0][0],))
        for r in rows[1:]:
            con.execute("UPDATE dateien SET status='duplikat' WHERE id=?", (r[0],))
            einsparung += r[2]; dupl += 1
    con.commit()
    print(f"\nErgebnis: {len(gruppen)} Duplikat-Gruppen, {dupl} überzählige "
          f"Dateien, Einsparpotenzial {gb(einsparung)}")
    print("→ Nächster Schritt: 'report' erzeugen und prüfen, danach optional "
          "'quarantaene --dry-run'.")

# ------------------- Phase 2b: Bildvarianten (nur Bericht) -------------------

def cmd_bilder(args):
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
    except ImportError:
        sys.exit("Benötigt Pillow:  pip install Pillow  (optional zusätzlich ImageHash)")
    try:
        import imagehash
        hat_phash = True
    except ImportError:
        hat_phash = False
        print("Hinweis: 'ImageHash' nicht installiert → Varianten-Erkennung "
              "(pHash) entfällt, EXIF/Auflösung werden trotzdem erfasst.")
    con = db_open(args.db)
    zeilen = con.execute("""SELECT id,pfad FROM dateien
        WHERE kategorie='Bild' AND (bild_breite IS NULL)""").fetchall()
    t0 = time.time()
    for i, (fid, pfad) in enumerate(zeilen, 1):
        fortschritt(i, len(zeilen), t0, "Bilder")
        try:
            with Image.open(pfad) as im:
                b, h = im.size
                exif_dt = None
                exif = im.getexif()
                if exif:
                    for tag_id, wert in exif.items():
                        if TAGS.get(tag_id) in ("DateTimeOriginal", "DateTime"):
                            exif_dt = str(wert); break
                ph = str(imagehash.phash(im)) if hat_phash else None
            con.execute("""UPDATE dateien SET bild_breite=?,bild_hoehe=?,
                           exif_datum=?,phash=? WHERE id=?""",
                        (b, h, exif_dt, ph, fid))
        except Exception:
            con.execute("UPDATE dateien SET bild_breite=-1 WHERE id=?", (fid,))
        if i % 200 == 0: con.commit()
    con.commit()
    print(f"\nBildanalyse abgeschlossen ({len(zeilen)} Bilder).")
    if hat_phash:
        n = con.execute("""SELECT COUNT(*) FROM (SELECT phash FROM dateien
            WHERE phash IS NOT NULL GROUP BY phash HAVING COUNT(*)>1)""").fetchone()[0]
        print(f"Visuell ähnliche Gruppen (identischer pHash): {n} – NUR Bericht, "
              f"diese Varianten werden nie automatisch angetastet.")

# ---------------------- Phase 3: Strukturvorschlag ---------------------------

def zielpfad(kat, mtime, exif_datum, ereignis=None):
    """Vorschlag: <Kategorie>/JJJJ/JJJJ-MM[_Ereignis]/ für Medien,
    <Kategorie>/JJJJ/ sonst. Ereignis = Urlaubsland/Gebiet aus Ordnernamen."""
    dt = None
    if exif_datum:
        try: dt = datetime.strptime(exif_datum[:19], "%Y:%m:%d %H:%M:%S")
        except ValueError: dt = None
    if dt is None:
        dt = datetime.fromtimestamp(mtime)
    basis = ZIEL_ORDNER.get(kat, "Sonstiges")
    if kat in ("Bild", "Bild-RAW", "Video"):
        monat = f"{dt.year}-{dt.month:02d}"
        if ereignis:
            monat += f"_{ereignis}"
        return f"{basis}/{dt.year}/{monat}"
    return f"{basis}/{dt.year}"

def cmd_struktur(args):
    con = db_open(args.db)
    q = con.execute("SELECT v FROM meta WHERE k='quelle'").fetchone()
    quelle = q[0] if q else None
    zaehler, volumen = defaultdict(int), defaultdict(int)
    for pfad, kat, mtime, exif, g, status in con.execute(
            """SELECT pfad,kategorie,mtime,exif_datum,groesse,status FROM dateien"""):
        if status in ("duplikat", "verschoben"):
            continue   # Duplikate zählen nicht in die Zielstruktur
        z = zielpfad(kat, mtime, exif, ereignis_label(pfad, quelle))
        zaehler[z] += 1; volumen[z] += g
    print("Vorgeschlagene Zielstruktur (ohne Duplikate):\n")
    print(f"{'Zielordner':<40}{'Dateien':>10}{'Volumen':>14}")
    print("-" * 64)
    for z in sorted(zaehler):
        print(f"{z:<40}{zaehler[z]:>10}{gb(volumen[z]):>14}")
    print("-" * 64)
    print(f"{'SUMME':<40}{sum(zaehler.values()):>10}{gb(sum(volumen.values())):>14}")
    print("\nHinweis: Der Vorschlag basiert auf EXIF-Aufnahmedatum (Bilder) bzw. "
          "Datei-Änderungsdatum (Heuristik, 'geschätzt'). Umsetzung erfolgt "
          "bewusst NICHT automatisch – Prüfung im HTML-Report.")

# ---------------------- Phase 4: Quarantäne (reversibel) ---------------------

def cmd_quarantaene(args):
    con = db_open(args.db)
    quelle = con.execute("SELECT v FROM meta WHERE k='quelle'").fetchone()
    quelle = Path(quelle[0]) if quelle else None
    ziel = Path(args.ziel)
    rows = con.execute("""SELECT id,pfad,groesse FROM dateien
                          WHERE status='duplikat'""").fetchall()
    if not rows:
        sys.exit("Keine markierten Duplikate. Zuerst 'duplikate' ausführen.")
    gesamt = sum(r[2] for r in rows)
    modus = "AUSFÜHREN" if args.ausfuehren else "DRY-RUN (es wird nichts verändert)"
    print(f"Modus: {modus}\nDuplikate: {len(rows)} Dateien, {gb(gesamt)}\n"
          f"Ziel-Quarantäne: {ziel}\n")
    protokoll = []
    for fid, pfad, g in rows:
        p = Path(pfad)
        try:
            rel = p.relative_to(quelle) if quelle else Path(p.name)
        except ValueError:
            rel = Path(p.name)
        z = ziel / rel
        protokoll.append({"von": str(p), "nach": str(z), "bytes": g})
        if args.ausfuehren:
            z.parent.mkdir(parents=True, exist_ok=True)
            if z.exists():                       # Kollision → Suffix
                z = z.with_name(z.stem + f"__{fid}" + z.suffix)
            shutil.move(str(p), str(z))          # VERSCHIEBEN, nie löschen
            con.execute("UPDATE dateien SET status='verschoben', pfad=? WHERE id=?",
                        (str(z), fid))
    if args.ausfuehren:
        con.commit()
    log = Path(args.db).with_suffix(".quarantaene.json")
    log.write_text(json.dumps(protokoll, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    print(f"Protokoll (für Rückgängig-Machen): {log}")
    if not args.ausfuehren:
        print("Zum tatsächlichen Verschieben:  --ausfuehren  statt  --dry-run")

# --------------- Themen-Mapping (Phase 6, Modus --mapping) -------------------

def lade_mapping(pfad: str) -> dict:
    cfg = json.loads(Path(pfad).read_text(encoding="utf-8"))
    cfg["_kw"] = [(ziel, [re.compile(p, re.IGNORECASE) for p in muster])
                  for ziel, muster in cfg["keywords"]]
    cfg["_laender"] = [(re.compile(p, re.IGNORECASE), name)
                       for p, name in cfg["laender"].items()]
    return cfg

def _norm(s: str) -> str:
    """Trenner zu Leerzeichen: '_' ist Regex-Wortzeichen, \\b griffe sonst nicht."""
    return re.sub(r"[_\-.]+", " ", s)

def themen_ziel(cfg, pfad, quelle, kat, mtime, exif_datum) -> tuple[str, str]:
    """Liefert (relativer Zielordner, regelquelle) gemaess Kaskade:
    alt_praefix > keywords > laender(Urlaub) > fallback. Heuristik – im
    Dry-Run-Plan pruefbar."""
    try:
        teile = Path(pfad).parent.relative_to(quelle).parts if quelle else \
                Path(pfad).parent.parts
    except (ValueError, TypeError):
        teile = Path(pfad).parent.parts
    seg_low = [t.lower() for t in teile]
    pfad_norm = _norm("/".join(seg_low))

    dt = None
    if exif_datum:
        try: dt = datetime.strptime(exif_datum[:19], "%Y:%m:%d %H:%M:%S")
        except ValueError: dt = None
    if dt is None:
        dt = datetime.fromtimestamp(mtime)

    def mit_jahr(ziel):
        if kat in cfg.get("jahr_unterordner_fuer", []):
            return f"{ziel}/{dt.year}"
        return ziel

    for seg in seg_low:
        if seg in cfg["alt_praefix"]:
            return mit_jahr(cfg["alt_praefix"][seg]), f"alt_praefix:{seg}"
    for ziel, muster in cfg["_kw"]:
        for m in muster:
            if m.search(pfad_norm):
                return mit_jahr(ziel), f"keyword:{m.pattern}"
    for m, ort in cfg["_laender"]:
        if m.search(pfad_norm):
            # Ihr Vorgabemuster: 900_Urlaub_Bilder/JJJJ_MM_Ort
            return (f"{cfg['urlaub_e1']}/{dt.year}_{dt.month:02d}_{ort}",
                    f"land:{ort}")
    return f"{cfg['fallback']}/{dt.year}-{dt.month:02d}", "fallback"

E1_README = ("README.txt – automatisch erzeugt von datenarchiv.py\n"
             "Ordner: {e1}\nQuelle: bereinigte Kopie aus {quelle}\n"
             "Erstellt: {datum}\nZuordnung: Ordnernamen-Heuristik (Kaskade "
             "alt_praefix > keywords > laender > fallback), Konfiguration: "
             "struktur_mapping.json\nHinweis: Nur byte-identische Duplikate "
             "wurden weggelassen; alle Qualitaetsvarianten sind enthalten.\n")

# --------------- Phase 6: Kopieren auf neuen Datenträger ---------------------

def datentraeger_menue() -> Path:
    """Interaktive Auswahl des Ziel-Datenträgers mit Freiplatz-Anzeige."""
    kandidaten = []
    if hasattr(os, "listdrives"):                     # Windows, Python >= 3.12
        kandidaten = list(os.listdrives())
    elif os.name == "nt":                             # Windows, ältere Python
        import string
        kandidaten = [f"{b}:\\" for b in string.ascii_uppercase
                      if os.path.exists(f"{b}:\\")]
    else:                                             # Linux/macOS: Mounts
        for wurzel in ("/mnt", "/media", "/Volumes"):
            if os.path.isdir(wurzel):
                kandidaten += [os.path.join(wurzel, d)
                               for d in sorted(os.listdir(wurzel))]
    print("Verfügbare Ziel-Datenträger/Ordner:")
    print(f"  {'Nr':<4}{'Pfad':<30}{'Frei':>12}{'Gesamt':>12}")
    for i, k in enumerate(kandidaten, 1):
        try:
            du = shutil.disk_usage(k)
            print(f"  {i:<4}{k:<30}{gb(du.free):>12}{gb(du.total):>12}")
        except OSError:
            print(f"  {i:<4}{k:<30}{'k.A.':>12}{'k.A.':>12}")
    wahl = input("Nummer wählen ODER Zielpfad direkt eingeben: ").strip()
    if wahl.isdigit() and 1 <= int(wahl) <= len(kandidaten):
        return Path(kandidaten[int(wahl) - 1])
    return Path(wahl)

def cmd_kopieren(args):
    con = db_open(args.db)
    q = con.execute("SELECT v FROM meta WHERE k='quelle'").fetchone()
    quelle = q[0] if q else None

    ziel = Path(args.ziel) if args.ziel else datentraeger_menue()
    ziel_basis = ziel / args.wurzel

    # Zu kopieren: alles außer als Duplikat markierte / bereits verschobene
    rows = con.execute("""SELECT id,pfad,groesse,mtime,kategorie,exif_datum
        FROM dateien WHERE status NOT IN('duplikat','verschoben')
        ORDER BY pfad""").fetchall()
    benoetigt = sum(r[2] for r in rows)

    # Freiplatz-Prüfung (5 % Reserve) – auf nächstem existierenden Elternpfad,
    # falls der Zielordner selbst noch nicht angelegt ist
    pruefpfad = ziel
    while not pruefpfad.exists() and pruefpfad.parent != pruefpfad:
        pruefpfad = pruefpfad.parent
    try:
        frei = shutil.disk_usage(pruefpfad).free
    except OSError:
        sys.exit(f"Ziel nicht erreichbar: {ziel}")
    print(f"\n{'Dateien zu kopieren':<28}{len(rows):>12}")
    print(f"{'Benötigtes Volumen':<28}{gb(benoetigt):>12}")
    print(f"{'Frei auf Ziel':<28}{gb(frei):>12}")
    if frei < benoetigt * 1.05:
        sys.exit("ABBRUCH: Zieldatenträger zu klein (inkl. 5 % Reserve).")

    # Plan erstellen: Quellpfad -> Zielpfad
    cfg = lade_mapping(args.mapping) if args.mapping else None
    plan, zaehler, regeln = [], defaultdict(int), defaultdict(int)
    for fid, pfad, g, mtime, kat, exif in rows:
        if cfg:
            rel, regel = themen_ziel(cfg, pfad, quelle, kat, mtime, exif)
            regeln[regel.split(":")[0]] += 1
        else:
            rel = zielpfad(kat, mtime, exif, ereignis_label(pfad, quelle))
        zielp = ziel_basis / rel / Path(pfad).name
        plan.append((fid, pfad, str(zielp), g))
        zaehler[rel] += 1
    if cfg:
        print("\nZuordnung nach Regelquelle:")
        for r, c in sorted(regeln.items(), key=lambda x: -x[1]):
            print(f"  {r:<14}{c:>8}  ({100*c/len(plan):.1f} %)")

    plan_csv = Path(args.db).with_suffix(".kopierplan.csv")
    with open(plan_csv, "w", encoding="utf-8-sig", newline="") as f:
        import csv
        w = csv.writer(f, delimiter=";")
        w.writerow(["quelle", "ziel", "bytes"])
        w.writerows([(p, z, g) for _, p, z, g in plan])

    print(f"\nZielstruktur (Top 40 Ordner):")
    print(f"  {'Ordner':<58}{'Dateien':>8}")
    for rel in sorted(zaehler)[:40]:
        print(f"  {rel:<58}{zaehler[rel]:>8}")
    if len(zaehler) > 40:
        print(f"  … insgesamt {len(zaehler)} Zielordner")
    print(f"\nVollständiger Plan: {plan_csv}")

    if not args.ausfuehren:
        print("\nDRY-RUN – es wurde NICHTS kopiert. Plan-CSV prüfen, dann mit "
              "--ausfuehren starten.\nHinweis: RAW-Dateien werden ausschließlich "
              "byte-identisch kopiert (shutil.copy2), niemals geöffnet/verändert.")
        return

    # Vorflug-Pruefung: Quelle erreichbar? (verhindert Massenfehler bei
    # nicht gemountetem Datentraeger)
    if quelle and not Path(quelle).exists():
        sys.exit(f"ABBRUCH: Quelle nicht erreichbar: {quelle}\n"
                 f"Datentraeger gemountet? Pruefen mit:  ls {quelle}")
    if plan and not Path(plan[0][1]).exists():
        sys.exit(f"ABBRUCH: erste Plandatei fehlt: {plan[0][1]}\n"
                 f"Quelle gemountet / Pfad unveraendert? Ggf. 'scan' erneut.")

    # Ausführung: additiv, wiederaufnehmbar, Quelle bleibt unverändert
    t0, kopiert, uebersprungen, vol = time.time(), 0, 0, 0
    fehler, fehler_serie = 0, 0
    fehlerlog = Path(args.db).with_suffix(".fehler.log")
    flog = open(fehlerlog, "w", encoding="utf-8")
    for i, (fid, pfad, zielp, g) in enumerate(plan, 1):
        zp = Path(zielp)
        try:
            if zp.exists():
                if zp.stat().st_size == g:            # bereits vorhanden → skip
                    uebersprungen += 1
                    continue
                zp = zp.with_name(zp.stem + f"__{fid}" + zp.suffix)
            zp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pfad, zp)                    # KOPIEREN, nie verschieben
            if zp.stat().st_size != g:
                raise OSError("Größen-Differenz nach Kopie")
            if args.verifizieren:
                if datei_hash(pfad, False) != datei_hash(str(zp), False):
                    raise OSError("Hash-Differenz nach Kopie")
            kopiert += 1; vol += g
        except OSError as e:
            fehler += 1; fehler_serie += 1
            flog.write(f"{pfad}\t{e}\n")
            if fehler <= 20:
                print(f"\n  ! FEHLER bei {pfad}: {e}", file=sys.stderr)
            elif fehler == 21:
                print(f"\n  ! Weitere Fehler nur noch in {fehlerlog}",
                      file=sys.stderr)
            if fehler_serie >= 200:
                flog.close()
                sys.exit(f"\nABBRUCH nach {fehler_serie} Fehlern in Folge – "
                         f"Quelle vermutlich nicht erreichbar/getrennt.\n"
                         f"Fehlerprotokoll: {fehlerlog}\n"
                         f"Nach Behebung gleichen Befehl erneut starten "
                         f"(bereits Kopiertes wird uebersprungen).")
            continue
        fehler_serie = 0
        if i % 50 == 0 or i == len(plan):
            dt = time.time() - t0
            print(f"\r  {i}/{len(plan)}  kopiert {kopiert}  "
                  f"übersprungen {uebersprungen}  {gb(vol)}  "
                  f"({vol/1_048_576/dt if dt else 0:,.0f} MB/s)",
                  end="", flush=True)
    if args.mapping:
        e1_menge = {Path(z).relative_to(ziel_basis).parts[0]
                    for _, _, z, _ in plan if Path(z).is_relative_to(ziel_basis)}
        for e1 in sorted(e1_menge):
            rp = ziel_basis / e1 / "README.txt"
            if not rp.exists():
                rp.parent.mkdir(parents=True, exist_ok=True)
                rp.write_text(E1_README.format(e1=e1, quelle=quelle,
                              datum=f"{datetime.now():%d.%m.%Y}"),
                              encoding="utf-8")
    flog.close()
    if fehler == 0 and fehlerlog.exists():
        fehlerlog.unlink()
    print(f"\nFertig: {kopiert} kopiert, {uebersprungen} übersprungen, "
          f"{fehler} Fehler{f' (siehe {fehlerlog})' if fehler else ''}, "
          f"{gb(vol)} in {time.time()-t0:,.0f} s.")
    print("Quelle wurde NICHT verändert. Bei Abbruch: gleichen Befehl erneut "
          "starten – vorhandene Dateien werden übersprungen.")

# ---------------------------- Phase 5: Report --------------------------------

def cmd_report(args):
    con = db_open(args.db)
    q = con.execute("SELECT v FROM meta WHERE k='quelle'").fetchone()
    stat = con.execute("""SELECT kategorie,COUNT(*),SUM(groesse) FROM dateien
                          GROUP BY kategorie ORDER BY SUM(groesse) DESC""").fetchall()
    dupl = con.execute("""SELECT COUNT(*),COALESCE(SUM(groesse),0) FROM dateien
                          WHERE status='duplikat'""").fetchone()
    gruppen = con.execute("""SELECT hash_voll,COUNT(*),SUM(groesse) FROM dateien
        WHERE status IN('original','duplikat') GROUP BY hash_voll
        ORDER BY SUM(groesse) DESC LIMIT 300""").fetchall()

    def zeile_gruppe(hv):
        rows = con.execute("""SELECT pfad,groesse,status FROM dateien
            WHERE hash_voll=? ORDER BY status DESC""", (hv,)).fetchall()
        li = "".join(
            f"<li class='{'orig' if s=='original' else 'dup'}'>"
            f"[{'BEHALTEN' if s=='original' else 'Duplikat'}] "
            f"{html.escape(p)} <small>({mb(g)})</small></li>"
            for p, g, s in rows)
        return f"<ul>{li}</ul>"

    # Varianten-Gruppen (pHash) – reiner Bericht
    var_html = ""
    var = con.execute("""SELECT phash,COUNT(*) FROM dateien
        WHERE phash IS NOT NULL GROUP BY phash HAVING COUNT(*)>1
        ORDER BY COUNT(*) DESC LIMIT 150""").fetchall()
    for ph, _ in var:
        rows = con.execute("""SELECT pfad,ext,bild_breite,bild_hoehe,groesse
            FROM dateien WHERE phash=? ORDER BY bild_breite*bild_hoehe DESC""",
            (ph,)).fetchall()
        rows = sorted(rows, key=lambda r: (FORMAT_RANG.get(r[1], 9),
                                           -(r[2] or 0) * (r[3] or 0)))
        li = "".join(f"<li>{html.escape(p)} <small>({e}, {b}×{h}, "
                     f"{mb(g)})</small></li>" for p, e, b, h, g in rows)
        var_html += f"<details><summary>Varianten-Gruppe ({len(rows)} Bilder, " \
                    f"beste Qualität zuerst)</summary><ul>{li}</ul></details>"

    kat_rows = "".join(f"<tr><td>{k}</td><td>{n}</td><td>{gb(v or 0)}</td></tr>"
                       for k, n, v in stat)
    dupl_html = "".join(
        f"<details><summary>{n}× identisch – Einsparung {gb(v-(v//n))}"
        f"</summary>{zeile_gruppe(hv)}</details>"
        for hv, n, v in gruppen)

    seite = f"""<!doctype html><html lang="de"><meta charset="utf-8">
<title>DatenArchiv-Report</title><style>
body{{font-family:system-ui,Segoe UI,sans-serif;max-width:1100px;margin:2rem auto;
padding:0 1rem;color:#222}} table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:.35rem .6rem;text-align:left}}
th{{background:#f3f3f3}} details{{margin:.4rem 0;border:1px solid #ddd;
border-radius:6px;padding:.3rem .6rem}} summary{{cursor:pointer;font-weight:600}}
.orig{{color:#0a7a2f}} .dup{{color:#a33}} small{{color:#666}}
h2{{border-bottom:2px solid #eee;padding-bottom:.3rem}}</style>
<h1>DatenArchiv – Analysebericht</h1>
<p>Quelle: <code>{html.escape(q[0]) if q else 'k.A.'}</code> ·
erstellt {datetime.now():%d.%m.%Y %H:%M}</p>
<h2>1 · Bestand nach Kategorie</h2>
<table><tr><th>Kategorie</th><th>Dateien</th><th>Volumen</th></tr>{kat_rows}</table>
<h2>2 · Exakte Duplikate (byte-identisch, BLAKE2b)</h2>
<p><b>{dupl[0]} überzählige Dateien · Einsparpotenzial {gb(dupl[1])}</b>.
Pro Gruppe bleibt genau eine Datei erhalten (grün). Entfernung erfolgt
ausschließlich per reversibler Quarantäne-Verschiebung.</p>
{dupl_html or "<p>k.A. – noch keine Duplikat-Analyse gelaufen oder keine Funde.</p>"}
<h2>3 · Bildvarianten (visuell ähnlich – NUR Bericht)</h2>
<p>Diese Gruppen enthalten dasselbe Motiv in unterschiedlicher Qualität
(z.&nbsp;B. RAW/Druck/Web). Sie werden <b>niemals automatisch verändert</b>;
die Liste dient nur Ihrer manuellen Sichtung. Sortierung: beste Qualität zuerst
(Heuristik: Format-Rang, dann Pixelfläche – „geschätzt“).</p>
{var_html or "<p>k.A. – 'bilder'-Analyse nicht gelaufen oder ImageHash fehlt.</p>"}
</html>"""
    Path(args.out).write_text(seite, encoding="utf-8")
    print(f"Report geschrieben: {args.out}")

# ------------------------------- CLI -----------------------------------------

def main():
    ap = argparse.ArgumentParser(description="DatenArchiv – sichere "
        "Deduplizierung und Strukturanalyse (löscht niemals Daten).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="Quelle inventarisieren (nur lesend)")
    s.add_argument("quelle"); s.add_argument("--db", default="inventar.db")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("duplikate", help="Exakte Duplikate ermitteln (nur lesend)")
    s.add_argument("--db", default="inventar.db"); s.set_defaults(fn=cmd_duplikate)

    s = sub.add_parser("bilder", help="Bildmetadaten + Varianten-Analyse (nur lesend)")
    s.add_argument("--db", default="inventar.db"); s.set_defaults(fn=cmd_bilder)

    s = sub.add_parser("struktur", help="Zielstruktur vorschlagen (nur lesend)")
    s.add_argument("--db", default="inventar.db"); s.set_defaults(fn=cmd_struktur)

    s = sub.add_parser("report", help="HTML-Report erzeugen")
    s.add_argument("--db", default="inventar.db")
    s.add_argument("--out", default="report.html"); s.set_defaults(fn=cmd_report)

    s = sub.add_parser("kopieren", help="Phase 6: bereinigte Struktur additiv "
                       "auf NEUEN Datenträger kopieren (Quelle bleibt unverändert)")
    s.add_argument("--db", default="inventar.db")
    s.add_argument("--ziel", help="Ziel-Datenträger/Ordner; ohne Angabe: Auswahlmenü")
    s.add_argument("--wurzel", default="Archiv",
                   help="Wurzelordner auf dem Ziel (Standard: Archiv)")
    s.add_argument("--ausfuehren", action="store_true",
                   help="tatsächlich kopieren (Standard: Dry-Run mit Plan-CSV)")
    s.add_argument("--verifizieren", action="store_true",
                   help="nach jeder Kopie BLAKE2b-Hash Quelle↔Ziel vergleichen")
    s.add_argument("--mapping", help="struktur_mapping.json fuer themenbasierte "
                   "Zielstruktur (ohne: Zeitstruktur Fotos/JJJJ/JJJJ-MM)")
    s.set_defaults(fn=cmd_kopieren)

    s = sub.add_parser("quarantaene", help="Duplikate reversibel verschieben")
    s.add_argument("--db", default="inventar.db")
    s.add_argument("--ziel", required=True, help="Quarantäne-Ordner")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--ausfuehren", action="store_true")
    s.set_defaults(fn=cmd_quarantaene)

    args = ap.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()