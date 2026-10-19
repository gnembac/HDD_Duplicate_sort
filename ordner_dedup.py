#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ordner_dedup.py — Deduplizierung und Einsortierung auf ORDNEREBENE.

Prinzipien (Nutzervorgabe):
  * Blattordner (letzte Ordnerebene mit Dateien) ist die atomare Einheit.
  * Dateien verlassen nie ihren Ordner, werden nie umbenannt, nie geloescht.
  * Duplikat-Kandidat: gleicher Ordnername + gleiche Dateianzahl + gleiche Typen.
  * BEWEIS (Sicherheitsergaenzung): BLAKE2b-Signatur ueber die sortierten
    Datei-Hashes aus inventar.db. Nur signaturgleiche Ordner gelten als
    Duplikat; Kandidaten mit abweichendem Inhalt werden NUR BERICHTET.
  * Nicht zuordenbare Ordner -> 999_Sonstiges/<Originalordnername>.
  * Kopieren additiv (copy2), Quelle bleibt unveraendert, Dry-Run Standard.

Aufruf:
  python ordner_dedup.py analyse --db inventar.db --mapping struktur_mapping.json
  python ordner_dedup.py kopieren --db inventar.db --mapping struktur_mapping.json \
         --ziel "/media/gnembac/LaCie" --wurzel "Archiv"              # Dry-Run
  ... zusaetzlich --ausfuehren [--verifizieren]                        # Echtlauf
"""
from __future__ import annotations
import argparse, csv, hashlib, json, re, shutil, sys, time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ------------------------- Mapping (Ordner-Ebene) ----------------------------

def lade_mapping(pfad):
    cfg = json.loads(Path(pfad).read_text(encoding="utf-8"))
    cfg["_kw"] = [(z, [re.compile(p, re.IGNORECASE) for p in m])
                  for z, m in cfg["keywords"]]
    cfg["_laender"] = [(re.compile(p, re.IGNORECASE), n)
                       for p, n in cfg["laender"].items()]
    cfg.setdefault("fallback", "999_Sonstiges")
    return cfg

def _norm(s): return re.sub(r"[_\-.]+", " ", s)

def thema_fuer_ordner(cfg, ordner: str, quelle: str) -> tuple[str, str]:
    """Genau EINE Themenentscheidung pro Ordner (kein Datums-Split)."""
    try:
        teile = Path(ordner).relative_to(quelle).parts
    except ValueError:
        teile = Path(ordner).parts
    seg_low = [t.lower() for t in teile]
    pfad_norm = _norm("/".join(seg_low))
    for seg in seg_low:
        if seg in cfg["alt_praefix"]:
            return cfg["alt_praefix"][seg], f"alt_praefix:{seg}"
    for ziel, muster in cfg["_kw"]:
        for m in muster:
            if m.search(pfad_norm):
                return ziel, f"keyword:{m.pattern}"
    for m, ort in cfg["_laender"]:
        if m.search(pfad_norm):
            return cfg["urlaub_e1"], f"land:{ort}"
    return cfg["fallback"], "fallback"

# ------------------------- Ordner-Inventar aus DB ----------------------------

def ordner_inventar(db):
    import sqlite3
    con = sqlite3.connect(db)
    q = con.execute("SELECT v FROM meta WHERE k='quelle'").fetchone()
    quelle = q[0].rstrip("/") if q else ""
    ordner = {}
    for pfad, groesse, ext, hv, mtime in con.execute(
            "SELECT pfad,groesse,ext,hash_voll,mtime FROM dateien"):
        d = str(Path(pfad).parent)
        o = ordner.setdefault(d, {"dateien": [], "bytes": 0,
                                  "typen": Counter(), "mtime": mtime})
        # Inhalts-Element: Voll-Hash; Fallback groessen-eindeutiger Dateien:
        # Name+Groesse (fuer diese existiert per Konstruktion kein Zwilling)
        elem = hv if hv else f"S{groesse}:{Path(pfad).name.lower()}"
        o["dateien"].append(elem)
        o["bytes"] += groesse
        o["typen"][(ext or "").lower()] += 1
        o["mtime"] = min(o["mtime"], mtime)
    for d, o in ordner.items():
        o["anzahl"] = len(o["dateien"])
        o["name"] = Path(d).name.lower()
        o["typ_sig"] = tuple(sorted(o["typen"].items()))
        h = hashlib.blake2b()
        for e in sorted(o["dateien"]):
            h.update(e.encode()); h.update(b"\x00")
        o["inhalt_sig"] = h.hexdigest()
    return quelle, ordner

# ------------------------- Analyse / Gruppierung -----------------------------

def gruppiere(ordner):
    kand = defaultdict(list)          # (name, anzahl, typ_sig) -> [pfade]
    for d, o in ordner.items():
        kand[(o["name"], o["anzahl"], o["typ_sig"])].append(d)
    exakt, kriterien_only = [], []
    for key, pfade in kand.items():
        if len(pfade) < 2:
            continue
        nach_sig = defaultdict(list)
        for d in pfade:
            nach_sig[ordner[d]["inhalt_sig"]].append(d)
        for sig, gl in nach_sig.items():
            if len(gl) > 1:
                gl.sort(key=lambda p: (len(p), ordner[p]["mtime"]))
                exakt.append(gl)      # gl[0] = Vertreter, Rest Duplikat
        if len(nach_sig) > 1:
            kriterien_only.append(pfade)
    return exakt, kriterien_only

def cmd_analyse(args, still=False):
    quelle, ordner = ordner_inventar(args.db)
    exakt, krit = gruppiere(ordner)
    dup_ordner = [p for g in exakt for p in g[1:]]
    dup_bytes = sum(ordner[p]["bytes"] for p in dup_ordner)
    ges_bytes = sum(o["bytes"] for o in ordner.values())
    if not still:
        print(f"Quelle:                    {quelle}")
        print(f"Blattordner gesamt:        {len(ordner)}")
        print(f"Exakte Duplikatgruppen:    {len(exakt)}  "
              f"(Name+Anzahl+Typ UND Inhalts-Signatur identisch)")
        print(f"  davon ueberzaehlige Ordner: {len(dup_ordner)}")
        print(f"  Einsparung:              {dup_bytes/1e9:,.2f} GB "
              f"von {ges_bytes/1e9:,.2f} GB "
              f"({100*dup_bytes/ges_bytes:.1f} %)")
        print(f"Kriterien-Treffer mit ABWEICHENDEM Inhalt: {len(krit)} Gruppen"
              f"  -> werden NICHT entfernt, nur berichtet")
        rep = Path(args.db).with_suffix(".ordner_kriterien_abweichend.csv")
        with open(rep, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["gruppe", "ordner", "dateien", "inhalt_sig"])
            for i, pfade in enumerate(krit, 1):
                for p in pfade:
                    w.writerow([i, p, ordner[p]["anzahl"],
                                ordner[p]["inhalt_sig"][:16]])
        print(f"Report abweichender Kriterien-Treffer: {rep}")
    return quelle, ordner, exakt

# ------------------------- Kopieren (Ordner-Einheiten) -----------------------

def cmd_kopieren(args):
    quelle, ordner, exakt = cmd_analyse(args, still=False)
    cfg = lade_mapping(args.mapping)
    duplikate = {p for g in exakt for p in g[1:]}
    zu_kopieren = [d for d in sorted(ordner) if d not in duplikate]

    ziel_basis = Path(args.ziel) / args.wurzel
    plan, belegte, regeln = [], {}, Counter()
    for d in zu_kopieren:
        thema, regel = thema_fuer_ordner(cfg, d, quelle)
        regeln[regel.split(":")[0]] += 1
        zn = Path(d).name or "WURZEL"
        zielordner = ziel_basis / thema / zn
        k = 1
        while str(zielordner).lower() in belegte:          # Ordnernamens-Kollision
            k += 1
            zielordner = ziel_basis / thema / f"{zn}__{k}"
        belegte[str(zielordner).lower()] = d
        plan.append((d, str(zielordner), ordner[d]["anzahl"],
                     ordner[d]["bytes"], thema, regel))

    csvp = Path(args.db).with_suffix(".ordnerplan.csv")
    with open(csvp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["quellordner", "zielordner", "dateien", "bytes",
                    "thema", "regel"])
        w.writerows(plan)

    print(f"\nZu kopierende Ordner:      {len(plan)}   "
          f"({sum(p[3] for p in plan)/1e9:,.2f} GB, "
          f"{sum(p[2] for p in plan)} Dateien)")
    print("Zuordnung nach Regelquelle:")
    for r, c in regeln.most_common():
        print(f"  {r:<14}{c:>8}  ({100*c/len(plan):.1f} %)")
    them = Counter(p[4].split('/')[0] for p in plan)
    print("Ordner je E1:")
    for t, c in them.most_common():
        print(f"  {t:<38}{c:>7}")
    print(f"Vollstaendiger Plan: {csvp}")

    if not args.ausfuehren:
        print("\nDRY-RUN – nichts kopiert. Plan pruefen, dann --ausfuehren.")
        return

    if quelle and not Path(quelle).exists():
        sys.exit(f"ABBRUCH: Quelle nicht erreichbar: {quelle}")
    t0 = time.time(); kop, skip, fehler, serie, vol = 0, 0, 0, 0, 0
    flog = open(Path(args.db).with_suffix(".ordner_fehler.log"), "w",
                encoding="utf-8")
    import sqlite3
    con = sqlite3.connect(args.db)
    for i, (d, zielordner, anz, byt, thema, regel) in enumerate(plan, 1):
        zo = Path(zielordner)
        try:
            zo.mkdir(parents=True, exist_ok=True)
            for (pfad, g) in con.execute(
                    "SELECT pfad,groesse FROM dateien WHERE pfad LIKE ? ",
                    (d.rstrip("/") + "/%",)):
                p = Path(pfad)
                if str(p.parent) != d:            # nur direkte Dateien
                    continue
                z = zo / p.name                    # Dateiname UNVERAENDERT
                if z.exists() and z.stat().st_size == g:
                    skip += 1; continue
                shutil.copy2(p, z)
                if z.stat().st_size != g:
                    raise OSError(f"Groessen-Differenz {z}")
                if args.verifizieren:
                    h1 = hashlib.blake2b(open(p, 'rb').read()).hexdigest() \
                        if g < (1 << 26) else None
                    h2 = hashlib.blake2b(open(z, 'rb').read()).hexdigest() \
                        if g < (1 << 26) else None
                    if h1 != h2:
                        raise OSError(f"Hash-Differenz {z}")
                kop += 1; vol += g
            serie = 0
        except OSError as e:
            fehler += 1; serie += 1
            flog.write(f"{d}\t{e}\n")
            if fehler <= 20:
                print(f"\n  ! FEHLER {d}: {e}", file=sys.stderr)
            if serie >= 50:
                flog.close()
                sys.exit("\nABBRUCH nach 50 Ordner-Fehlern in Folge – "
                         "Quelle/Ziel pruefen, dann erneut starten.")
        if i % 20 == 0 or i == len(plan):
            dt = time.time() - t0
            print(f"\r  {i}/{len(plan)} Ordner  {kop} Dateien kopiert  "
                  f"{skip} uebersprungen  {vol/1e9:,.2f} GB "
                  f"({vol/1e6/dt if dt else 0:,.0f} MB/s)", end="", flush=True)
    flog.close()
    print(f"\nFertig: {kop} Dateien kopiert, {skip} uebersprungen, "
          f"{fehler} Ordner-Fehler, {vol/1e9:,.2f} GB in {time.time()-t0:,.0f} s."
          f"\nQuelle unveraendert. Wiederanlauf ueberspringt Vorhandenes.")

# ------------------------------- CLI ------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Ordner-basierte Deduplizierung "
        "und Einsortierung (Ordner = atomare Einheit, loescht nie).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyse"); a.add_argument("--db", default="inventar.db")
    a.set_defaults(fn=cmd_analyse)
    k = sub.add_parser("kopieren")
    k.add_argument("--db", default="inventar.db")
    k.add_argument("--mapping", default="struktur_mapping.json")
    k.add_argument("--ziel", required=True)
    k.add_argument("--wurzel", default="Archiv")
    k.add_argument("--ausfuehren", action="store_true")
    k.add_argument("--verifizieren", action="store_true")
    k.set_defaults(fn=cmd_kopieren)
    args = ap.parse_args()
    args.fn(args)

if __name__ == "__main__":
    main()