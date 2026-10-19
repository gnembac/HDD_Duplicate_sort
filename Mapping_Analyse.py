#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mapping_analyse.py — Ermittelt aus inventar.db die real vorkommenden
Ordnernamen (alle Ebenen) als empirische Basis fuer die Themen-Zuordnung (T2).

Zweck: Liefert (a) haeufigste Ordner-Segmente, (b) erkannte Laender/Orte fuer
Urlaub, (c) Themen-Treffer nach Schluesselwort-Heuristik. Nur lesend.

Aufruf:
  python mapping_analyse.py --db inventar.db
  python mapping_analyse.py --db inventar.db --quelle "/media/gnembac/Data/2025_02_Backup_all_HDs"
  python mapping_analyse.py --db inventar.db --csv mapping_kandidaten.csv
"""
from __future__ import annotations
import argparse, sqlite3, csv, sys, re
from collections import Counter, defaultdict
from pathlib import PurePosixPath

# --- Themen-Schluesselwoerter (Heuristik, erweiterbar) -----------------------
# Format: Ziel-E1 : Liste von Regex-Schluesselwoertern (case-insensitive)
THEMA_KEYWORDS = {
    "550_Urlaub":            [r"urlaub", r"reise", r"segel", r"safari", r"ecotraining"],
    "300_Landwirtschaft":    [r"landwirt", r"acker", r"ernte", r"feld", r"traktor", r"schlepper"],
    "040_Steuer":            [r"steuer", r"finanzamt", r"est\b", r"elster"],
    "700_Hobbies":           [r"hobb", r"jagd", r"angel", r"motorrad", r"funk", r"segel"],
    "080_Amateurfunk":       [r"funk", r"amateurfunk", r"hamradio", r"dl7gn", r"winlink"],
    "020_Bauen_Projekte":    [r"bau", r"scheune", r"keller", r"renovier", r"aussenanlag", r"au\u00dfenanlag"],
    "005_Gewerbe_NCFAI":     [r"gewerbe", r"ncfai", r"consulting", r"flavour", r"pv[_ ]?anlage",
                              r"pv[_ ]?frei", r"pv[_ ]?dach", r"bess", r"photovolta"],
    "800_Gesundheit":        [r"gesundheit", r"arzt", r"medizin", r"gicht", r"purin"],
    "000_Bier_Brauen":       [r"bier", r"brau", r"sommelier", r"bjcp", r"malz", r"hopfen"],
    "015_Kochen_Rezepte":    [r"koch", r"rezept", r"gew\u00fcrz", r"gewuerz"],
    "400_Dokumente":         [r"dokument", r"wichtig", r"vertrag", r"versicherung", r"karten"],
    "500_EDV_IT":            [r"edv", r"\bit\b", r"computer", r"software", r"anleitung", r"colab", r"ebook", r"buecher"],
    "900_Bilder_priv":       [r"eltern", r"familie", r"persönlich", r"persoenlich", r"privat", r"priv\b"],
    "950_Geldanlagen":       [r"geldanlage", r"rente", r"aktie", r"depot", r"crypto", r"broker"],
    "600_Wetter":            [r"wetter", r"klima"],
    "070_LKW":               [r"lkw", r"f\u00fchrerschein", r"fuehrerschein"],
}

# --- Laender/Orte-Gazetteer fuer Urlaubs-Erkennung (Auszug, erweiterbar) ------
LAENDER = [
    "südafrika","suedafrika","botswana","namibia","kenia","tansania","yemen","jemen",
    "malta","schottland","irland","island","norwegen","schweden","finnland","dänemark",
    "daenemark","italien","spanien","frankreich","portugal","griechenland","kroatien",
    "österreich","oesterreich","schweiz","niederlande","belgien","polen","tschechien",
    "ungarn","budapest","rumänien","türkei","tuerkei","marokko","ägypten","aegypten",
    "usa","kanada","mexiko","brasilien","argentinien","chile","peru","thailand","vietnam",
    "indien","nepal","japan","china","australien","neuseeland","nordsee","ostsee","alpen",
    "speyside","lofoten",
]
_RE_JAHR = re.compile(r"(19|20)\d{2}")
_RE_LAND = re.compile("|".join(sorted(LAENDER, key=len, reverse=True)), re.IGNORECASE)

def get_quelle(con, arg):
    if arg: return arg
    r = con.execute("SELECT v FROM meta WHERE k='quelle'").fetchone()
    return r[0] if r else ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="inventar.db")
    ap.add_argument("--quelle", default=None)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--top", type=int, default=60)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    quelle = get_quelle(con, args.quelle).rstrip("/")
    if not quelle:
        sys.exit("Quelle unbekannt – bitte --quelle angeben.")

    seg_counter = Counter()          # jedes Ordnersegment
    thema_treffer = defaultdict(lambda: [0, 0])   # E1 -> [dateien, ordner]
    urlaub_orte = Counter()
    n = 0
    for (pfad,) in con.execute("SELECT pfad FROM dateien WHERE status!='duplikat'"):
        n += 1
        try:
            rel = PurePosixPath(pfad).relative_to(quelle)
        except ValueError:
            continue
        ordner = rel.parts[:-1]      # ohne Dateiname
        for seg in ordner:
            seg_counter[seg] += 1
        pfad_low = "/".join(ordner).lower()
        # Themen-Zuordnung: erster Treffer gewinnt
        for e1, keys in THEMA_KEYWORDS.items():
            if any(re.search(k, pfad_low) for k in keys):
                thema_treffer[e1][0] += 1
                break
        # Urlaubs-Ort-Erkennung
        m = _RE_LAND.search(pfad_low)
        if m:
            urlaub_orte[m.group(0).lower()] += 1

    print(f"Analysierte Nicht-Duplikat-Dateien: {n}\n")

    print("=== Top-Ordnersegmente (alle Ebenen) ===")
    print(f"{'Segment':<45}{'Dateien':>10}")
    for seg, c in seg_counter.most_common(args.top):
        print(f"{seg[:44]:<45}{c:>10}")

    print("\n=== Themen-Treffer (Heuristik, Schluesselwort) ===")
    print(f"{'Ziel-E1':<28}{'Dateien':>10}")
    gesamt_zug = 0
    for e1, (d, _) in sorted(thema_treffer.items(), key=lambda x: -x[1][0]):
        print(f"{e1:<28}{d:>10}")
        gesamt_zug += d
    print(f"{'--- zugeordnet gesamt':<28}{gesamt_zug:>10}")
    print(f"{'--- NICHT zugeordnet':<28}{n-gesamt_zug:>10}  -> 999_Unsortiert")
    quote = 100*gesamt_zug/n if n else 0
    print(f"Geschaetzte Auto-Zuordnungsquote: {quote:.1f} %  (Rest -> Auffang)")

    print("\n=== Erkannte Urlaubs-Orte/Laender (Gazetteer) ===")
    print(f"{'Ort/Land':<22}{'Dateien':>10}")
    for ort, c in urlaub_orte.most_common(30):
        print(f"{ort:<22}{c:>10}")

    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["segment", "dateien"])
            w.writerows(seg_counter.most_common())
        print(f"\nSegment-Liste geschrieben: {args.csv}")

if __name__ == "__main__":
    main()