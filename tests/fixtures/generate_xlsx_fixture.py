"""One-time generator for tests/fixtures/sample.xlsx.

Re-run with `uv run python tests/fixtures/generate_xlsx_fixture.py` if the
fixture needs to be regenerated.
"""

from pathlib import Path

import openpyxl

FIXTURE_PATH = Path(__file__).parent / "sample.xlsx"


def main() -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(["id", "name", "amount"])
    sheet.append([1, "Ada", 100])
    sheet.append([2, "Grace", 200])

    workbook.save(FIXTURE_PATH)
    print(f"wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
