#!/usr/bin/env python3
"""Capture and compare PostgreSQL backup/restore inventory reports."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

COPY_HEADER = re.compile(
    r'^COPY\s+'
    r'(?P<schema>"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)\.'
    r'(?P<table>"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)'
    r'\s+\(.*\)\s+FROM stdin;$'
)
EXCLUDED_SCHEMAS = (
    "pg_catalog", "information_schema", "auth", "storage", "realtime",
    "extensions", "vault", "graphql", "graphql_public", "supabase_functions",
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
                match = COPY_HEADER.match(line.rstrip("\r\n"))
                if not match:
                    raise ValueError(f"Unrecognized COPY statement in {path}:{line_number}")
                key = (unquote_identifier(match.group("schema")), unquote_identifier(match.group("table")))
                if key in counts:
                    raise ValueError(f"Duplicate COPY block for {key[0]}.{key[1]}")
                row_count = 0
                for data_line in lines:
                    if data_line.rstrip("\r\n") == r"\.":
                        break
                    row_count += 1
                else:
                    raise ValueError(f"Unterminated COPY block for {key[0]}.{key[1]}")
                counts[key] = row_count
    return counts


def collect_database_report(database_url: str) -> dict:
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary] to collect database reports") from exc

    tables = []
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT n.nspname, c.relname,
                       pg_relation_size(c.oid) AS table_bytes,
                       pg_indexes_size(c.oid) AS index_bytes,
                       pg_total_relation_size(c.oid) AS total_bytes,
                       (SELECT count(*) FROM pg_index i WHERE i.indrelid = c.oid) AS index_count
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p')
                  AND n.nspname <> ALL(%s)
                ORDER BY n.nspname, c.relname
                """,
                (list(EXCLUDED_SCHEMAS),),
            )
            inventory = cursor.fetchall()
            for schema, table, table_bytes, index_bytes, total_bytes, index_count in inventory:
                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                )
                row_count = cursor.fetchone()[0]
                tables.append({
                    "schema": schema, "table": table, "row_count": row_count,
                    "index_count": index_count, "table_bytes": table_bytes,
                    "index_bytes": index_bytes, "total_bytes": total_bytes,
                })
            cursor.execute("SELECT pg_database_size(current_database())")
            database_bytes = cursor.fetchone()[0]

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "database_bytes": database_bytes,
        "table_count": len(tables),
        "index_count": sum(item["index_count"] for item in tables),
        "row_count_total": sum(item["row_count"] for item in tables),
        "tables": tables,
    }


def write_report(report: dict, output_dir: Path, prefix: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{prefix}.json"
    csv_path = output_dir / f"{prefix}.csv"
    md_path = output_dir / f"{prefix}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = ["schema", "table", "row_count", "index_count", "table_bytes", "index_bytes", "total_bytes"]
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report["tables"])
    lines = [
        f"# {prefix.replace('_', ' ').title()}",
        "",
        f"- Generated (UTC): {report['generated_at_utc']}",
        f"- Database size: {report['database_bytes']:,} bytes ({report['database_bytes'] / 1024 / 1024:.2f} MiB)",
        f"- Tables: {report['table_count']:,}",
        f"- Indexes: {report['index_count']:,}",
        f"- Total rows: {report['row_count_total']:,}",
        "",
        "| Schema | Table | Rows | Indexes | Table MiB | Index MiB | Total MiB |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for item in report["tables"]:
        lines.append(
            f"| {item['schema']} | {item['table']} | {item['row_count']:,} | {item['index_count']:,} | "
            f"{item['table_bytes'] / 1048576:.2f} | {item['index_bytes'] / 1048576:.2f} | "
            f"{item['total_bytes'] / 1048576:.2f} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def compare_reports(baseline: dict, restored: dict) -> list[str]:
    errors = []
    before = {(t["schema"], t["table"]): t for t in baseline["tables"]}
    after = {(t["schema"], t["table"]): t for t in restored["tables"]}
    for key in sorted(before.keys() - after.keys()):
        errors.append(f"Missing restored table: {key[0]}.{key[1]}")
    for key in sorted(after.keys() - before.keys()):
        errors.append(f"Unexpected restored table: {key[0]}.{key[1]}")
    for key in sorted(before.keys() & after.keys()):
        left, right = before[key], after[key]
        for field in ("row_count", "index_count"):
            if left[field] != right[field]:
                errors.append(
                    f"{field} mismatch for {key[0]}.{key[1]}: before={left[field]}, restored={right[field]}"
                )
    if baseline["table_count"] != restored["table_count"]:
        errors.append(f"Table count mismatch: before={baseline['table_count']}, restored={restored['table_count']}")
    if baseline["index_count"] != restored["index_count"]:
        errors.append(f"Index count mismatch: before={baseline['index_count']}, restored={restored['index_count']}")
    return errors


def verify_copy_counts(database_url: str, counts: dict[tuple[str, str], int]) -> None:
    import psycopg
    from psycopg import sql
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            for (schema, table), expected in sorted(counts.items()):
                cursor.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(schema), sql.Identifier(table)
                ))
                actual = cursor.fetchone()[0]
                if actual != expected:
                    raise AssertionError(
                        f"Dump row count mismatch for {schema}.{table}: dump={expected}, restored={actual}"
                    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--data-file", action="append", default=[], type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--prefix", default="backup_inventory")
    parser.add_argument("--baseline-json", type=Path)
    args = parser.parse_args()

    if args.data_file:
        counts = copy_row_counts(args.data_file)
        verify_copy_counts(args.database_url, counts)

    report = collect_database_report(args.database_url)
    if args.report_dir:
        write_report(report, args.report_dir, args.prefix)

    if args.baseline_json:
        baseline = json.loads(args.baseline_json.read_text(encoding="utf-8"))
        errors = compare_reports(baseline, report)
        result = {
            "status": "FAILED" if errors else "PASSED",
            "baseline": str(args.baseline_json),
            "restored_report": f"{args.prefix}.json",
            "errors": errors,
            "before": {k: baseline[k] for k in ("database_bytes", "table_count", "index_count", "row_count_total")},
            "restored": {k: report[k] for k in ("database_bytes", "table_count", "index_count", "row_count_total")},
            "storage_size_note": "Database/table/index byte sizes are informational; physical layout may differ after restore.",
        }
        if args.report_dir:
            (args.report_dir / "restore_comparison.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            lines = [
                "# Backup / restore validation",
                "",
                f"**Status: {result['status']}**",
                "",
                "| Metric | Before backup | After restore |",
                "|---|---:|---:|",
            ]
            for label, key in (("Database size (bytes)", "database_bytes"), ("Tables", "table_count"),
                               ("Indexes", "index_count"), ("Rows (total)", "row_count_total")):
                lines.append(f"| {label} | {baseline[key]:,} | {report[key]:,} |")
            lines += ["", "## Differences"]
            lines += [f"- {error}" for error in errors] if errors else ["- No table, index-count, or row-count differences."]
            lines += ["", "Storage sizes are informational and are not required to match byte-for-byte."]
            (args.report_dir / "restore_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(f"Restore inventory comparison passed: {report['table_count']} tables, {report['index_count']} indexes, {report['row_count_total']} rows.")
    elif args.data_file:
        print(f"Restore COPY row-count verification passed for {len(counts)} dump tables.")
    else:
        print(f"Database inventory captured: {report['table_count']} tables, {report['index_count']} indexes, {report['row_count_total']} rows.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
