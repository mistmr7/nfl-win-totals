# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "numpy",
#     "pandas",
#     "lxml",
# ]
# ///
"""Fetch NFL season win totals once and append to data/snapshots.csv.

Designed to run daily via GitHub Actions cron. Targets RotoWire's
DraftKings win-totals page. If the page changes structure or moves
to client-side rendering, the debug output will tell you what to
switch to.
"""
from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd


URL = "https://www.rotowire.com/betting/nfl/win-totals.php"
BOOK = "DraftKings"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

OUT_PATH = Path(__file__).parent / "data" / "snapshots.csv"


def fetch_html(url: str) -> str:
    """GET the page with a realistic User-Agent and return the HTML text."""
    resp = httpx.get(url, headers=HEADERS, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _promote_first_row_if_unnamed(df: pd.DataFrame) -> pd.DataFrame:
    """If the table had no <thead>, pandas leaves columns as integers.

    In that case, treat row 0 as the header and drop it from the data.
    """
    if all(isinstance(c, (int, np.integer)) for c in df.columns):
        df = df.copy()
        df.columns = [str(v).strip() for v in df.iloc[0].tolist()]
        df = df.iloc[1:].reset_index(drop=True)
    return df


def find_win_totals_table(html: str) -> pd.DataFrame:
    """Locate the win-totals table among all <table> elements on the page.

    If no table matches the expected schema, prints summaries of every
    table found so you can pick the right one or pivot to a different
    source.
    """
    tables = pd.read_html(io.StringIO(html))
    if not tables:
        raise RuntimeError(
            "No <table> elements found on the page. "
            "The site may be JS-rendered; try a different source."
        )

    for t in tables:
        t = _promote_first_row_if_unnamed(t)
        cols = " ".join(str(c).lower() for c in t.columns)
        if "team" in cols and ("win" in cols or "total" in cols):
            return t

    print("Tables found but none matched expected schema. Dumping summaries:", file=sys.stderr)
    for i, t in enumerate(tables):
        print(f"  [{i}] columns={list(t.columns)} shape={t.shape}", file=sys.stderr)
    raise RuntimeError("Could not identify the win totals table; inspect output above.")


def _split_combined_odds(value: object) -> tuple[str, str]:
    """Split '+115 / -140' into ('+115', '-140').

    First value is the over price, second is the under price. Returns
    empty strings for missing or malformed input.
    """
    if pd.isna(value):
        return ("", "")
    parts = str(value).split("/", 1)
    if len(parts) != 2:
        return (str(value).strip(), "")
    return (parts[0].strip(), parts[1].strip())


def normalize(df: pd.DataFrame, snapshot_date: str, fetched_at: str, book: str) -> pd.DataFrame:
    """Coerce the scraped table into the long-format schema.

    Handles two RotoWire layouts: separate Over and Under columns, or a
    single combined 'Odds (O/U)' column with values like '+115 / -140'.
    """
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    team_col = next((c for c in df.columns if "team" in c), None)
    wt_col = next((c for c in df.columns if "win" in c and "total" in c), None)
    over_col = next((c for c in df.columns if c.startswith("over")), None)
    under_col = next((c for c in df.columns if c.startswith("under")), None)
    combined_col = next(
        (c for c in df.columns if "odds" in c and "/" in c and not over_col),
        None,
    )

    if not team_col or not wt_col:
        raise RuntimeError(
            f"Missing team or win-total column. Got: {list(df.columns)}"
        )

    out = pd.DataFrame()
    out["team"] = df[team_col].astype(str).str.strip()
    out["win_total"] = df[wt_col]

    if over_col and under_col:
        out["over_odds"] = df[over_col]
        out["under_odds"] = df[under_col]
    elif combined_col:
        pairs = df[combined_col].apply(_split_combined_odds)
        out["over_odds"] = [p[0] for p in pairs]
        out["under_odds"] = [p[1] for p in pairs]
    else:
        raise RuntimeError(
            f"Could not find odds columns (separate over/under or combined O/U). "
            f"Got: {list(df.columns)}"
        )

    out.insert(0, "snapshot_date", snapshot_date)
    out.insert(1, "book", book)
    out["fetched_at"] = fetched_at
    return out


def append_snapshot(df: pd.DataFrame, path: Path) -> None:
    """Append today's rows, replacing any existing rows for the same (date, book)."""
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = pd.read_csv(path)
        snapshot_date = df["snapshot_date"].iloc[0]
        book = df["book"].iloc[0]
        dupe = (existing["snapshot_date"] == snapshot_date) & (existing["book"] == book)
        kept = existing.loc[~dupe]
        combined = pd.concat([kept, df], ignore_index=True)
    else:
        combined = df

    combined.to_csv(path, index=False)


def main() -> int:
    now = datetime.now(timezone.utc)
    snapshot_date = now.strftime("%Y-%m-%d")
    fetched_at = now.isoformat()

    print(f"Fetching {URL}")
    html = fetch_html(URL)

    raw = find_win_totals_table(html)
    print(f"Found table with {len(raw)} rows and columns: {list(raw.columns)}")

    snapshot = normalize(raw, snapshot_date=snapshot_date, fetched_at=fetched_at, book=BOOK)
    print(f"\nNormalized snapshot ({len(snapshot)} rows):")
    print(snapshot.to_string(index=False))

    append_snapshot(snapshot, OUT_PATH)
    print(f"\nAppended to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
