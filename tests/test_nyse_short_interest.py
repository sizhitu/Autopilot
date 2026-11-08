import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "archive" / "nyse_short_interest" / "fetch_and_parse.py"
SPEC = importlib.util.spec_from_file_location("nyse_short_interest_archive", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NyseShortInterestArchiveTests(unittest.TestCase):
    def test_public_report_filename_extracts_publication_date(self):
        match = MODULE.FILE_RE.search("NYSE_Group_Consolidated_Short_Interest_20261026.xls")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "20261026")

    def test_integer_parser_handles_commas_and_missing_values(self):
        self.assertEqual(MODULE._as_int("19,919,374,573"), 19919374573)
        self.assertEqual(MODULE._as_int(2896.0), 2896)
        self.assertIsNone(MODULE._as_int("-"))
        self.assertIsNone(MODULE._as_int(None))

    def test_archive_is_explicitly_aggregate_only(self):
        self.assertEqual(MODULE.EXCHANGE_ALIASES["NYSE GROUP"], "NYSE GROUP")
        self.assertNotIn("symbol", MODULE.FIELDS)
        self.assertNotIn("ticker", MODULE.FIELDS)


if __name__ == "__main__":
    unittest.main()
