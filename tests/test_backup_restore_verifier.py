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


if __name__ == "__main__":
    unittest.main()
