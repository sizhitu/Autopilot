import tempfile
import unittest
from pathlib import Path

from scripts.verify_supabase_restore import copy_row_counts


class CopyRowCountTests(unittest.TestCase):
    def test_counts_multiple_copy_blocks_and_quoted_identifiers(self):
        dump = '''COPY public.assets (id, name) FROM stdin;
1\tfirst
2\tsecond
\\.
COPY "MixedSchema"."Odd""Table" (id) FROM stdin;
7
\\.
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.sql"
            path.write_text(dump, encoding="utf-8")
            self.assertEqual(
                copy_row_counts([path]),
                {("public", "assets"): 2, ("MixedSchema", 'Odd"Table'): 1},
            )

    def test_zero_row_copy_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.sql"
            path.write_text("COPY public.empty_table (id) FROM stdin;\n\\.\n", encoding="utf-8")
            self.assertEqual(copy_row_counts([path]), {("public", "empty_table"): 0})

    def test_unterminated_copy_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.sql"
            path.write_text("COPY public.assets (id) FROM stdin;\n1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unterminated COPY"):
                copy_row_counts([path])


class InventoryComparisonTests(unittest.TestCase):
    def setUp(self):
        self.before = {
            "table_count": 2, "index_count": 3, "row_count_total": 5,
            "tables": [
                {"schema": "public", "table": "a", "row_count": 2, "index_count": 2},
                {"schema": "public", "table": "b", "row_count": 3, "index_count": 1},
            ],
        }

    def test_identical_inventory_passes_even_when_storage_sizes_differ(self):
        from scripts.verify_supabase_restore import compare_reports
        after = {
            **self.before, "database_bytes": 999999,
            "tables": [dict(row, total_bytes=1234) for row in self.before["tables"]],
        }
        self.assertEqual(compare_reports(self.before, after), [])

    def test_missing_table_and_row_or_index_mismatch_fail(self):
        from scripts.verify_supabase_restore import compare_reports
        after = {
            "table_count": 1, "index_count": 1, "row_count_total": 4,
            "tables": [{"schema": "public", "table": "a", "row_count": 1, "index_count": 1}],
        }
        errors = compare_reports(self.before, after)
        self.assertTrue(any("Missing restored table: public.b" in item for item in errors))
        self.assertTrue(any("row_count mismatch" in item for item in errors))
        self.assertTrue(any("index_count mismatch" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
