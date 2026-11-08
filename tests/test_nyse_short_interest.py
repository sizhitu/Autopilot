import importlib.util
from pathlib import Path
import unittest
import tempfile
import xlwt

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

    def test_parse_workbook_handles_title_block_and_merged_settlement_date(self):
        workbook = xlwt.Workbook()
        sheet = workbook.add_sheet("Short Interest")
        sheet.write(0, 0, "NYSE Group Consolidated Short Interest Report")
        headers = [
            "SETTLEMENT DATE", "EXCHANGE", "TOTAL CURRENT SHORT INTEREST",
            "TOTAL PREVIOUS SHORT INTEREST (Revised)",
            "NUMBER of SECURITIES with a SHORT POSITION",
            "NUMBER of SECURITIES with a POSITION >= 5,000 SHARES",
        ]
        for col, value in enumerate(headers):
            sheet.write(3, col, value)
        exchanges = ["NYSE", "NYSE ARCA", "NYSE AMERICAN", "NYSE GROUP"]
        for row, exchange in enumerate(exchanges, start=4):
            sheet.write(row, 0, "10/15/2026" if row == 4 else "")
            sheet.write(row, 1, exchange)
            sheet.write(row, 2, 1000000 + row)
            sheet.write(row, 3, 900000 + row)
            sheet.write(row, 4, 200 + row)
            sheet.write(row, 5, 150 + row)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.xls"
            workbook.save(str(path))
            rows = MODULE.parse_workbook(
                path, "20261026", "https://ftp.nyse.com/sample.xls", "abc123"
            )
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["settlement_date"] for row in rows}, {"2026-10-15"})
        self.assertEqual({row["source_scope"] for row in rows}, {"exchange_aggregate"})
        self.assertEqual({row["exchange"] for row in rows}, set(exchanges))

    def test_archive_is_explicitly_aggregate_only(self):
        self.assertEqual(MODULE.EXCHANGE_ALIASES["NYSE GROUP"], "NYSE GROUP")
        self.assertNotIn("symbol", MODULE.FIELDS)
        self.assertNotIn("ticker", MODULE.FIELDS)


if __name__ == "__main__":
    unittest.main()
