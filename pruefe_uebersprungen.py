#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pruefe_uebersprungen.py — Verifiziert nach einem Kopierlauf alle Plan-Zeilen,
deren Zielpfad mehrfach belegt war (Ursache fuer "uebersprungen"). Vergleicht
uebersprungene Quelldateien per BLAKE2b mit der tatsaechlich am Ziel liegenden
Datei. Inhaltlich identisch -> harmlos. Abweichend -> Nachkopie mit Suffix.

Nur lesend, ausser --nachkopieren ist gesetzt (kopiert dann fehlende Inhalte
additiv mit Namenssuffix __K<n>; loescht/ueberschreibt nie).

Aufruf:
  python pruefe_uebersprungen.py --plan inventar.kopierplan.csv            # nur pruefen
  python pruefe_uebersprungen.py --plan inventar.kopierplan.csv --nachkopieren
"""
from __future__ import annotations
import argparse, csv, hashlib, shutil, sys
from collections import defaultdict
from pathlib import Path

def b2(pfad: Path) -> str | None:
    try:
        h = hashlib.blake2b()
        with open(pfad, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError as e:
        print(f"  ! Lesefehler {pfad}: {e}", file=sys.stderr)
        return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="inventar.kopierplan.csv")
    ap.add_argument("--nachkopieren", action="store_true",
                    help="abweichende Inhalte mit Suffix __K<n> nachkopieren")
    args = ap.parse_args()

    ziel_gruppen: dict[str, list[str]] = defaultdict(list)
    with open(args.plan, encoding="utf-8-sig", newline="") as f:
        for z in csv.DictReader(f, delimiter=";"):
            ziel_gruppen[z["ziel"]].append(z["quelle"])

    kollisionen = {z: q for z, q in ziel_gruppen.items() if len(q) > 1}
    betroffen = sum(len(q) - 1 for q in kollisionen.values())
    print(f"Plan-Zeilen gesamt:        {sum(len(q) for q in ziel_gruppen.values())}")
    print(f"Kollisions-Zielpfade:      {len(kollisionen)}")
    print(f"Uebersprungene (erwartet): {betroffen}   "
          f"(Soll-Abgleich mit Kopierlauf-Ausgabe)")

    identisch, abweichend, nachkopiert, fehlend = 0, 0, 0, 0
    bericht = []
    for ziel, quellen in kollisionen.items():
        zp = Path(ziel)
        if not zp.exists():
            fehlend += 1
            bericht.append(("ZIEL_FEHLT", ziel, ""))
            continue
        hz = b2(zp)
        gesehen = {hz}
        k = 0
        for q in quellen:
            hq = b2(Path(q))
            if hq is None:
                continue
            if hq in gesehen:
                identisch += 1
                continue
            # inhaltlich abweichende, uebersprungene Datei
            abweichend += 1
            gesehen.add(hq)
            bericht.append(("ABWEICHEND", q, ziel))
            if args.nachkopieren:
                k += 1
                neu = zp.with_name(zp.stem + f"__K{k}" + zp.suffix)
                while neu.exists():
                    k += 1
                    neu = zp.with_name(zp.stem + f"__K{k}" + zp.suffix)
                shutil.copy2(q, neu)      # additiv, nie ueberschreiben
                if b2(neu) == hq:
                    nachkopiert += 1
                else:
                    print(f"  ! Verifikation fehlgeschlagen: {neu}",
                          file=sys.stderr)

    print(f"\nErgebnis:")
    print(f"  inhaltlich identisch (harmlos): {identisch}")
    print(f"  inhaltlich ABWEICHEND:          {abweichend}")
    if args.nachkopieren:
        print(f"  nachkopiert + verifiziert:      {nachkopiert}")
    if fehlend:
        print(f"  Zielpfade fehlen:               {fehlend}  (unerwartet!)")

    log = Path(args.plan).with_suffix(".kollisionen.csv")
    with open(log, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["status", "quelle", "ziel"])
        w.writerows(bericht)
    print(f"\nProtokoll: {log}")
    if abweichend and not args.nachkopieren:
        print("→ Abweichende Inhalte gefunden. Nachkopie mit:  --nachkopieren")
    elif not abweichend:
        print("→ Alle uebersprungenen Dateien sind inhaltsgleich mit dem Ziel: "
              "KEIN Datenverlust.")

if __name__ == "__main__":
    main()