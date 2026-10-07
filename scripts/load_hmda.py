"""Build the local HMDA warehouse (SQLite) that hmda_summary prefers over the API.

Two ways to feed it:

  # 1) A CSV you already downloaded (e.g. the national LAR snapshot, ~GBs)
  python scripts/load_hmda.py --csv ~/Downloads/2023_public_lar.csv

  # 2) Just the lenders you care about, pulled from the FFIEC Data Browser
  python scripts/load_hmda.py --year 2023 --lei KB1H1DSPRFMYMCUFXT09 --lei 7H6GLXDRUGQFU57RNE97

Only the columns the server queries are kept, so a national file stays manageable.
Rows are streamed, never loaded into memory all at once.
"""

from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
from pathlib import Path

import httpx

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "hmda.sqlite"
CSV_API = "https://ffiec.cfpb.gov/v2/data-browser-api/view/nationwide/csv"
COLUMNS = ["activity_year", "lei", "action_taken", "loan_amount", "state_code"]
BATCH = 50_000


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lar (
               activity_year INTEGER, lei TEXT, action_taken INTEGER,
               loan_amount REAL, state_code TEXT)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_lar_lei_year ON lar (lei, activity_year)")
    return conn


def _rows(reader: csv.DictReader):
    for r in reader:
        try:
            yield (
                int(r["activity_year"]),
                r["lei"],
                int(r["action_taken"]),
                float(r["loan_amount"]) if r.get("loan_amount") not in (None, "", "NA") else None,
                r.get("state_code") or None,
            )
        except (KeyError, ValueError):
            continue  # malformed row; HMDA has a few


def load(conn: sqlite3.Connection, reader: csv.DictReader) -> int:
    missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        sys.exit(f"CSV is missing expected columns: {missing}")
    total, batch = 0, []
    for row in _rows(reader):
        batch.append(row)
        if len(batch) >= BATCH:
            conn.executemany("INSERT INTO lar VALUES (?,?,?,?,?)", batch)
            conn.commit()
            total += len(batch)
            batch.clear()
            print(f"  {total:,} rows", end="\r")
    if batch:
        conn.executemany("INSERT INTO lar VALUES (?,?,?,?,?)", batch)
        conn.commit()
        total += len(batch)
    return total


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--csv", type=Path, help="local LAR CSV to load")
    ap.add_argument("--year", type=int, help="year to pull from the Data Browser")
    ap.add_argument("--lei", action="append", default=[], help="LEI to pull (repeatable)")
    ap.add_argument(
        "--replace", action="store_true", help="delete existing rows for these LEIs/year first"
    )
    args = ap.parse_args()

    conn = init_db(args.db)

    if args.csv:
        with open(args.csv, newline="") as f:
            n = load(conn, csv.DictReader(f))
        print(f"loaded {n:,} rows from {args.csv} into {args.db}")
        return

    if not (args.year and args.lei):
        ap.error("either --csv, or --year with one or more --lei")

    if args.replace:
        conn.executemany(
            "DELETE FROM lar WHERE lei = ? AND activity_year = ?",
            [(lei, args.year) for lei in args.lei],
        )
        conn.commit()

    params = {"years": str(args.year), "leis": ",".join(args.lei)}
    print(f"downloading {args.year} LAR for {len(args.lei)} LEI(s) from FFIEC...")
    with httpx.stream("GET", CSV_API, params=params, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        text = io.TextIOWrapper(_ByteIter(resp.iter_bytes()), encoding="utf-8", newline="")
        n = load(conn, csv.DictReader(text))
    print(f"loaded {n:,} rows into {args.db}")


class _ByteIter(io.RawIOBase):
    """Adapts httpx's byte iterator to a file object so csv can stream it."""

    def __init__(self, it):
        self._it, self._buf = it, b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self._buf:
            try:
                self._buf = next(self._it)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n], self._buf = self._buf[:n], self._buf[n:]
        return n


if __name__ == "__main__":
    main()
