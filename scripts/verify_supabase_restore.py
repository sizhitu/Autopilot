#!/usr/bin/env python3
"""Compare restored PostgreSQL row counts with COPY records in Supabase CLI dumps."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

COPY_HEADER = re.compile(
    r'^COPY\\s+'
    r'(?P<schema>"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)\\.'
    r'(?P<table>"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)'
    r'\\s+\\(.*\\)\\s+FROM stdin;$'
)


def unquote_identifier(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value


def copy_row_counts(paths: list[Path]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            lines = iter(stream)
            for line_number, line in enumerate(lines, start=1):
                if not line.startswith("COPY "):
                    continue
                match = COPY_HEADER.match(line.rstrip("\\r\\n"))
                if not match:
                    raise ValueError(f"Unrecognized COPY statement in {path}:{line_number}")
                key = (
                    unquote_identifier(match.group("schema")),
                    unquote_identifier(match.group("table")),
                )
                if key in counts:
                    raise ValueError(f"Duplicate COPY block for {key[0]}.{key[1]}")
                row_count = 0
                for data_line in lines:
                    if data_line.rstrip("\\r\\n") == r"\\.":
                        break
                    row_count += 1
                else:
                    raise ValueError(f"Unterminated COPY block for {key[0]}.{key[1]}")
                counts[key] = row_count
    return counts


def verify(database_url: str, counts: dict[tuple[str, str], int]) -> None:
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary] to verify restored row counts") from exc

    if not counts:
        print("No COPY blocks found; SQL restore succeeded but there were no exported table rows to compare.")
        return

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            for (schema, table), expected in sorted(counts.items()):
                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                )
                actual = cursor.fetchone()[0]
                if actual != expected:
                    raise AssertionError(
                        f"Row count mismatch for {schema}.{table}: dump={expected}, restored={actual}"
                    )
                print(f"OK {schema}.{table}: {actual} rows")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--data-file", action="append", required=True, type=Path)
    args = parser.parse_args()
    counts = copy_row_counts(args.data_file)
    verify(args.database_url, counts)
    print(f"Restore row-count verification passed for {len(counts)} tables.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # surfaced as a failing CI step
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
