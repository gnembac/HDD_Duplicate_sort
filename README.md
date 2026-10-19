# DatenArchiv — Safe Deduplication & Restructuring Tool

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-inventory-003B57?logo=sqlite&logoColor=white">
  <img alt="Pillow" src="https://img.shields.io/badge/Pillow-image%20analysis-ffcc00">
  <img alt="Hashing" src="https://img.shields.io/badge/Hash-BLAKE2b-6f42c1">
  <img alt="Platform" src="https://img.shields.io/badge/OS-Windows%20%7C%20Linux%20%7C%20macOS-success">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
</p>

A command-line tool (Python >= 3.9, Windows/Linux/macOS) that analyses deeply
nested backup folders, finds **exact** duplicate files, reports image variants
(RAW / print / web) **without touching them**, proposes a clean target folder
structure, and copies the cleaned result onto a **new** drive — leaving the
source completely untouched.

> **Core promise:** No data loss and no quality loss. Only byte-identical files
> are treated as duplicates. Different-quality versions of the same photo are
> reported for your review, never deleted or modified automatically.

---

## Table of Contents

- [DatenArchiv — Safe Deduplication \& Restructuring Tool](#datenarchiv--safe-deduplication--restructuring-tool)
  - [Table of Contents](#table-of-contents)
  - [1 · What this tool does](#1--what-this-tool-does)
  - [2 · Safety guarantees](#2--safety-guarantees)
  - [3 · Requirements \& installation (beginner-friendly)](#3--requirements--installation-beginner-friendly)
    - [3.1 What you need](#31-what-you-need)
    - [3.2 Step-by-step setup](#32-step-by-step-setup)
  - [4 · Quick start — the full workflow](#4--quick-start--the-full-workflow)
  - [5 · Command reference](#5--command-reference)
    - [Copy phase (`kopieren`) options in detail](#copy-phase-kopieren-options-in-detail)
  - [6 · How the target structure is built](#6--how-the-target-structure-is-built)
  - [7 · Google Drive sync (rclone)](#7--google-drive-sync-rclone)
  - [8 · Git \& GitHub notes](#8--git--github-notes)
  - [9 · Limitations](#9--limitations)
  - [License](#license)

---

## 1 · What this tool does

The tool works in **phases**. Phases 1-4 are **read-only** — they never change
your data. Only the copy phase (6) writes, and it writes **only to the new
target drive**.

| Phase | Command | What happens | Writes anything? |
|-------|---------|--------------|------------------|
| 1 | `scan` | Builds an inventory (path, size, date, category) in a SQLite database | DB only |
| 2 | `duplikate` | Finds exact duplicates via a 3-stage hash cascade | DB only |
| 2b | `bilder` | Reads photo EXIF, resolution, perceptual hash (RAW is **never** opened) | DB only |
| 3 | `struktur` | Proposes a clean target folder layout | nothing |
| 4 | `report` | Generates an HTML report for you to review | `report.html` |
| 6 | `kopieren` | Copies the cleaned structure to a **new** drive | target drive only |

There is also an optional Phase 5 (`quarantaene`) that *moves* duplicates into a
reversible quarantine folder — only needed if you clean **in place** instead of
copying to a new drive.

---

## 2 · Safety guarantees

| Principle | Implementation |
|---|---|
| Never deletes | Duplicates are **moved** to a quarantine folder (relative path preserved -> fully reversible via JSON log), or simply skipped when copying to a new drive |
| No quality loss | A duplicate = **byte-identical** (BLAKE2b full hash, RFC 7693). Image variants (RAW/print/web) are only reported, never changed |
| RAW protected | RAW files (CR2/NEF/ARW/DNG ...) are never opened or decoded — only byte-identical copies are made |
| Resumable | SQLite inventory; interrupted runs continue without re-reading already-hashed files |
| Preview before action | Every writing operation requires an explicit `--ausfuehren` flag; the default is a `--dry-run` that shows exactly what *would* happen |
| Free-space check | The copy phase aborts if the target has less than the required volume + 5 % reserve |

Sources for the methods used: BLAKE2b — RFC 7693 (<https://www.rfc-editor.org/rfc/rfc7693>) ·
Pillow (<https://pillow.readthedocs.io>) · ImageHash (<https://pypi.org/project/ImageHash/>) ·
EXIF standard CIPA DC-008 (<https://www.cipa.jp/e/std/std-sec.html>).

---

## 3 · Requirements & installation (beginner-friendly)

### 3.1 What you need

| Item | Why | How to check |
|---|---|---|
| Python >= 3.9 | runs the tool | `python --version` (or `python3 --version`) |
| pip | installs libraries | `pip --version` |
| Pillow + ImageHash | **only** for the image analysis phase (2b) | see below |

Everything else (`argparse`, `sqlite3`, `hashlib`, `shutil`) is part of Python's
standard library — no extra install needed.

### 3.2 Step-by-step setup

A **virtual environment** (venv) keeps this project's libraries separate from
your system Python. Recommended, but optional.

```bash
# 1. Go to the project folder
cd ~/Documents/HDD_sort          # adjust to your path

# 2. Create a virtual environment (once)
python -m venv .venv

# 3. Activate it
#    Linux / macOS:
source .venv/bin/activate
#    Windows (PowerShell):
.venv\Scripts\Activate.ps1

# --> your prompt now shows (.venv) at the front. While it does, use `python`.

# 4. Install the image-analysis libraries (only needed for phase 2b)
pip install Pillow ImageHash
```

> **Note on the `python` vs `python3` command:** Inside an active venv the
> command is `python`. On some Linux systems *outside* a venv you must type
> `python3`. If `python` reports "command not found", use `python3` instead.

> **"externally-managed-environment" error on Linux?** That happens when
> installing outside a venv. Either activate a venv (recommended, see above) or
> run `pip install --user Pillow ImageHash`.

---

## 4 · Quick start — the full workflow

Replace the example paths with your own. **Quote any path that contains a
space** (e.g. `"/media/user/Extreme SSD"`).

```bash
# Phase 0 — MANDATORY unless already done: keep an untouched 1:1 copy of the
#           original drive as your fallback. If you already copied the data to
#           another disk, that copy IS your fallback — skip this step.
#   Linux/macOS example:  rsync -a --info=progress2 /source/ /backup/
#   Windows example:      robocopy E:\ F:\HDD_Backup /MIR /R:1 /W:1 /LOG:copy.log

# Phase 1 — inventory (read-only)
python datenarchiv.py scan "/media/user/Data/Backup" --db inventar.db

# Phase 2 — exact duplicates (size -> partial hash -> full BLAKE2b hash)
python datenarchiv.py duplikate --db inventar.db

# Phase 2b — image EXIF / resolution / visual variants (RAW never opened)
python datenarchiv.py bilder --db inventar.db

# Phase 3 — proposed target structure (Fotos/YYYY/YYYY-MM_<Location>/)
python datenarchiv.py struktur --db inventar.db

# Phase 4 — HTML report; open it and review before copying
python datenarchiv.py report --db inventar.db --out report.html
xdg-open report.html            # Linux    (macOS: open / Windows: start)

# Phase 6a — DRY-RUN copy: writes a plan CSV, copies NOTHING
python datenarchiv.py kopieren --db inventar.db --ziel "/media/user/Extreme SSD" --wurzel "Archiv"

# Phase 6b — actual copy + per-file hash verification (source stays untouched)
python datenarchiv.py kopieren --db inventar.db --ziel "/media/user/Extreme SSD" --wurzel "Archiv" --ausfuehren --verifizieren
```

---

## 5 · Command reference

All commands take `--db inventar.db` (the inventory database).

| Command | Key options | Read-only? | Purpose |
|---|---|---|---|
| `scan <source>` | — | yes | Inventory every file into SQLite |
| `duplikate` | — | yes | Detect byte-identical duplicates |
| `bilder` | — | yes | Photo metadata + variant grouping (needs Pillow/ImageHash) |
| `struktur` | — | yes | Print the proposed target layout |
| `report` | `--out report.html` | writes HTML | Human-readable review report |
| `quarantaene` | `--ziel <folder>` + `--dry-run` \| `--ausfuehren` | move op | Reversibly move duplicates (in-place cleanup) |
| `kopieren` | `--ziel <drive>` `--wurzel <name>` `--ausfuehren` `--verifizieren` | writes target only | Copy cleaned structure to a NEW drive |

### Copy phase (`kopieren`) options in detail

| Option | Meaning | If omitted |
|---|---|---|
| `--ziel "..."` | target drive/folder | interactive menu of all mounts with free-space display |
| `--wurzel "Archiv"` | name of the top folder on the target | defaults to `Archiv` |
| *(no `--ausfuehren`)* | **dry-run** (default) | shows plan + CSV, copies nothing |
| `--ausfuehren` | actually copy via `shutil.copy2` (preserves timestamps) | — |
| `--verifizieren` | compare BLAKE2b hash source<->target after each copy | size check only |

---

## 6 · How the target structure is built

| File type | Target folder | Date source |
|---|---|---|
| Photos (JPEG/PNG/TIFF...) | `Fotos/YYYY/YYYY-MM_<Location>/` | EXIF `DateTimeOriginal`, fallback file mtime |
| RAW photos | `Fotos-RAW/YYYY/YYYY-MM_<Location>/` | file mtime (RAW is never decoded) |
| Videos | `Videos/YYYY/YYYY-MM_<Location>/` | file mtime |
| Documents / audio / archives | `Dokumente/`, `Musik/`, `Archive/` + `/YYYY/` | file mtime |

**`<Location>` (vacation label):** taken from the deepest **non-generic** folder
name in the source path (e.g. `Urlaub_Italien`, `Norwegen_Lofoten`). Generic
names (Backup, DCIM, `100CANON`, bare years, Kopie, Neu ...) are skipped. This is
a **heuristic** — verify it in the plan CSV before copying. To adjust, edit the
`GENERISCH` set near the top of `datenarchiv.py`.

---

## 7 · Google Drive sync (rclone)

Syncing is intentionally **not** built into this tool. Use
[rclone](https://rclone.org) — a mature open-source tool that handles conflicts,
partial uploads and API quotas.

```bash
rclone config          # once: create a remote named "gdrive" (type: drive)

# Mirror the cleaned structure to Drive (additive, change-based):
rclone sync "/media/user/Extreme SSD/Archiv" gdrive:Archiv \
       --backup-dir gdrive:_Trash --dry-run          # preview first
rclone sync "/media/user/Extreme SSD/Archiv" gdrive:Archiv \
       --backup-dir gdrive:_Trash                    # then real run
```

Sources: <https://rclone.org/drive/> · <https://rclone.org/commands/rclone_bisync/>

> **Security:** never commit `rclone.conf` to Git — it contains OAuth tokens.
> It is already listed in `.gitignore`.

---

## 8 · Git & GitHub notes

This repository should contain **only** the code and documentation — never your
private inventory database, reports or plan files (they list all your file
paths). Those are excluded via `.gitignore`.

```bash
# Stage only the files meant to be public:
git add README.md datenarchiv.py .gitignore
git status                          # confirm ONLY these appear
git commit -m "DatenArchiv tool + docs"
git push origin main                # or 'master'

# If a private file was accidentally committed earlier, untrack it (keeps it locally):
git rm --cached inventar.db
git commit -m "Stop tracking private inventory database"
```

---

## 9 · Limitations

| Limitation | Detail |
|---|---|
| Variant detection | Uses exact perceptual-hash match (conservative). Cropped/edited copies may not be grouped. **k.A.** on real-world hit rate until tested on your data |
| RAW variants | RAW files are inventoried but not decoded -> no perceptual hash for RAW (by design, to protect them) |
| Runtime (1 TB HDD) | Scan < 30 min; full hashing of duplicate candidates typically 1-4 h depending on duplicate share — **estimated** values |
| Original selection | Per duplicate group, the kept file = shortest path, then oldest timestamp (heuristic, editable in `cmd_duplikate`) |

---

## License

MIT — see `LICENSE` (add one if you intend to publish; `.gitignore` does not
exclude it).