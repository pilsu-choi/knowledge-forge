"""One-time generator for tests/fixtures/sample.xlsx.

Re-run with `uv run python tests/fixtures/generate_xlsx_fixture.py` if the
fixture needs to be regenerated.
"""

import io
from pathlib import Path

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from PIL import Image as PILImage

FIXTURE_PATH = Path(__file__).parent / "sample.xlsx"


def main() -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(["id", "name", "amount"])
    sheet.append([1, "Ada", 100])
    sheet.append([2, "Grace", 200])

    img_buf = io.BytesIO()
    PILImage.new("RGB", (80, 50), color=(0, 120, 200)).save(img_buf, format="PNG")
    img_buf.seek(0)
    sheet.add_image(XLImage(img_buf), "E1")

    workbook.save(FIXTURE_PATH)
    print(f"wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
