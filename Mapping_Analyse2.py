#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mapping_analyse.py  V2 — Themen-Zuordnung mit Prioritaets-Kaskade, Gazetteer-
Integration und Rest-Analyse fuer iterative Quotensteigerung (Ziel: minimale
Restmenge in 999_Unsortiert).

Kaskade (erste Regel gewinnt, Reihenfolge = Prioritaet):
  1. Alt-Praefix-Uebersetzung (100_Beruf -> 030_...)
  2. Themen-Schluesselwoerter (Hobbies VOR Urlaub: Jagd/Konzert/Segeln = Hobby)
  3. Gazetteer-Laender -> 550_Urlaub
  4. Fallback: 999_Unsortiert  (M1-Garantie: jede Datei erhaelt einen Zielort)

Aufruf:
  python mapping_analyse.py --db inventar.db
  python mapping_analyse.py --db inventar.db --rest 40      # Rest-Analyse-Tiefe
  python mapping_analyse.py --db inventar.db --csv rest_segmente.csv
"""
from __future__ import annotations
import argparse, sqlite3, csv, sys, re
from collections import Counter, defaultdict
from pathlib import PurePosixPath

# --- 1) Alt-Praefix-Uebersetzung (exakter Segment-Match, hoechste Prioritaet) -
ALT_PRAEFIX = {
    "100_beruf":            "030_Beruf_Studium",
    "800_persönlich":       "900_Bilder_priv",
    "800_persoenlich":      "900_Bilder_priv",
    "900_info":             "200_Info_Wissen",
    "010_boot_segeln":      "700_Hobbies",
    "010_törninfo_gunter":  "700_Hobbies",
    "010_toerninfo_gunter": "700_Hobbies",
    "1_aussortiert":        "999_Unsortiert",
    "alle_ohne_aussortiert":"999_Unsortiert",
}

# --- 2) Themen-Schluesselwoerter, REIHENFOLGE = PRIORITAET ---------------------
# Nutzerregel 4: Jagd/Konzert/Segeln (Aktivitaet) VOR Urlaub pruefen.
THEMA_KEYWORDS = [
    ("030_Beruf_Studium",  [r"\bfte\b", r"info_fte", r"\bpeb\b", r"peb_o1", r"prj_",
                            r"schwungmassen", r"pr\u00fcfstand", r"pruefstand",
                            r"\bhfh\b", r"diplomarbeit", r"fernhochschule",
                            r"studium", r"beruf"]),
    ("700_Hobbies",        [r"jagd", r"wildkamera", r"ansitz", r"hochsitz",
                            r"konzert", r"iron_maiden", r"\bmusic\b", r"festival",
                            r"t\u00f6rn", r"toern", r"segel", r"\bboot\b",
                            r"motorrad", r"angel", r"hobb"]),
    ("080_Amateurfunk",    [r"amateurfunk", r"dl7gn", r"winlink", r"meshtastic",
                            r"\bfunk\b", r"hamradio"]),
    ("000_Bier_Brauen",    [r"bier", r"\bbrau", r"sommelier", r"bjcp", r"malz",
                            r"hopfen", r"bierwanderung"]),
    ("005_Gewerbe_NCFAI",  [r"ncfai", r"consulting", r"flavour", r"photovolta",
                            r"pv[_ ]?anlage", r"pv[_ ]?frei", r"pv[_ ]?dach",
                            r"\bbess\b", r"gewerbe"]),
    ("020_Bauen_Projekte", [r"\bbau\b", r"bauen", r"scheune", r"keller",
                            r"renovier", r"aussenanlag", r"au\u00dfenanlag",
                            r"sanierung"]),
    ("300_Landwirtschaft", [r"landwirt", r"acker", r"ernte", r"\bfeld\b",
                            r"traktor", r"schlepper", r"obstgeh"]),
    ("040_Steuer",         [r"steuer", r"elster", r"finanzamt", r"\best\b"]),
    ("950_Geldanlagen",    [r"geldanlage", r"rente", r"aktie", r"depot",
                            r"crypto", r"broker"]),
    ("800_Gesundheit",     [r"gesundheit", r"\barzt\b", r"medizin", r"gicht",
                            r"purin"]),
    ("015_Kochen_Rezepte", [r"koch", r"rezept", r"gew\u00fcrz", r"gewuerz"]),
    ("500_EDV_IT",         [r"\bedv\b", r"ebook", r"buecher", r"b\u00fccher",
                            r"colab", r"software", r"\bit\b", r"computer",
                            r"edocs"]),
    ("200_Info_Wissen",    [r"anleitung", r"fachspezif", r"know[_ ]?how",
                            r"info_tl", r"\binfo\b", r"wissen"]),
    ("400_Dokumente",      [r"wichtige_dokumente", r"dokument", r"vertrag",
                            r"versicherung", r"karten", r"urkunde"]),
    ("900_Bilder_priv",    [r"eltern", r"familie", r"\bmoni\b", r"nembach",
                            r"gunter", r"pers\u00f6nlich", r"persoenlich",
                            r"privat", r"hochzeit", r"geburtstag"]),
    ("600_Wetter",         [r"wetter", r"klima"]),
    ("070_LKW",            [r"\blkw\b", r"f\u00fchrerschein", r"fuehrerschein"]),
    # Urlaub-Schluesselwoerter ZULETZT unter den Keywords (nach Aktivitaeten):
    ("550_Urlaub",         [r"urlaub", r"reise", r"safari", r"ecotraining",
                            r"\bzar\b"]),
]

# --- 3) Gazetteer: Laender/Regionen -> 550_Urlaub (nach Keywords geprueft) ----
LAENDER = [
    "südafrika","suedafrika","botswana","namibia","nambia","kenia","tansania",
    "yemen","jemen","malta","schottland","islay","speyside","tobermory",
    "irland","island","norwegen","lofoten","schweden","finnland","dänemark",
    "daenemark","italien","spanien","mallorca","balearen","belearen",
    "frankreich","portugal","griechenland","kroatien","österreich",
    "oesterreich","schweiz","niederlande","belgien","polen","tschechien",
    "ungarn","budapest","bulgarien","rumänien","rumaenien","\\brum\\b",
    "\\bgr\\b","\\bbul\\b","türkei","tuerkei","marokko","ägypten","aegypten",
    "\\busa\\b","kanada","mexiko","brasilien","thailand","vietnam","indien",
    "nepal","japan","china","australien","neuseeland","nordsee","ostsee",
    "alpen","mittelmeer",
]
_RE_LAND = re.compile("|".join(sorted(LAENDER, key=len, reverse=True)),
                      re.IGNORECASE)
_RE_JAHR = re.compile(r"(19|20)\d{2}")

def zuordnen(pfad_low: str, segmente_low: list[str]) -> tuple[str, str]:
    """Liefert (E1, regelquelle). Kaskade gemaess Prioritaet."""
    for seg in segmente_low:
        if seg in ALT_PRAEFIX:
            return ALT_PRAEFIX[seg], "alt_praefix"
    for e1, keys in THEMA_KEYWORDS:
        for k in keys:
            if re.search(k, pfad_low):
                return e1, f"keyword:{k}"
    m = _RE_LAND.search(pfad_low)
    if m:
        return "550_Urlaub", f"gazetteer:{m.group(0).lower()}"
    return "999_Unsortiert", "fallback"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="inventar.db")
    ap.add_argument("--quelle", default=None)
    ap.add_argument("--csv", default=None, help="Rest-Segmente als CSV")
    ap.add_argument("--rest", type=int, default=40,
                    help="Top-N Segmente der NICHT zugeordneten Dateien")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    if args.quelle:
        quelle = args.quelle.rstrip("/")
    else:
        r = con.execute("SELECT v FROM meta WHERE k='quelle'").fetchone()
        if not r:
            sys.exit("Quelle unbekannt – bitte --quelle angeben.")
        quelle = r[0].rstrip("/")

    thema = Counter()
    rest_segmente = Counter()
    urlaub_land = Counter()
    n = 0
    for (pfad,) in con.execute(
            "SELECT pfad FROM dateien WHERE status!='duplikat'"):
        n += 1
        try:
            rel = PurePosixPath(pfad).relative_to(quelle)
        except ValueError:
            continue
        ordner = rel.parts[:-1]
        pfad_low = "/".join(ordner).lower()
        seg_low = [s.lower() for s in ordner]
        e1, regel = zuordnen(pfad_low, seg_low)
        thema[e1] += 1
        if e1 == "999_Unsortiert":
            for s in ordner:
                rest_segmente[s] += 1
        if e1 == "550_Urlaub" and regel.startswith("gazetteer:"):
            urlaub_land[regel.split(":", 1)[1]] += 1

    zugeordnet = n - thema.get("999_Unsortiert", 0)
    print(f"Analysierte Nicht-Duplikat-Dateien: {n}\n")
    print("=== Themen-Zuordnung V2 (Kaskade: Alt-Praefix > Keywords > Gazetteer) ===")
    print(f"{'Ziel-E1':<28}{'Dateien':>10}{'Anteil':>9}")
    for e1, c in thema.most_common():
        print(f"{e1:<28}{c:>10}{100*c/n:>8.1f}%")
    print("-" * 47)
    print(f"{'zugeordnet (semantisch)':<28}{zugeordnet:>10}{100*zugeordnet/n:>8.1f}%")
    r999 = thema.get('999_Unsortiert', 0)
    print(f"{'999_Unsortiert (Fallback)':<28}{r999:>10}{100*r999/n:>8.1f}%")
    print(f"\nM1 technische Zuordnung: 100.0 %  (jede Datei hat einen Zielort)")
    print(f"M2 semantische Quote:    {100*zugeordnet/n:.1f} %")

    print(f"\n=== Gazetteer-Zuordnungen nach 550_Urlaub (Top 25) ===")
    for land, c in urlaub_land.most_common(25):
        print(f"{land:<22}{c:>10}")

    print(f"\n=== REST-ANALYSE: groesste Segmente in 999_Unsortiert (Top {args.rest}) ===")
    print("(Diese Liste iterativ pruefen -> Keywords ergaenzen -> erneut messen)")
    print(f"{'Segment':<45}{'Dateien':>10}")
    for seg, c in rest_segmente.most_common(args.rest):
        print(f"{seg[:44]:<45}{c:>10}")

    if args.csv:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["segment", "dateien_unzugeordnet"])
            w.writerows(rest_segmente.most_common())
        print(f"\nRest-Segmente geschrieben: {args.csv}")

if __name__ == "__main__":
    main()