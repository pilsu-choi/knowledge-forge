"""One-time generator for tests/fixtures/sample.hwpx.

Re-run with `uv run python tests/fixtures/generate_hwpx_fixture.py` if the
fixture needs to be regenerated (e.g. after a python-hwpx upgrade).
"""

import io
from pathlib import Path

from hwpx.document import HwpxDocument
from PIL import Image as PILImage

FIXTURE_PATH = Path(__file__).parent / "sample.hwpx"


def main() -> None:
    doc = HwpxDocument.new()
    doc.add_heading("Knowledge Forge Sample", level=1)
    # inherit_style=False: add_paragraph() otherwise inherits the preceding
    # heading's Outline-N style, which would make this line indistinguishable
    # from a heading in the fixture.
    doc.add_paragraph("This is a short paragraph used to exercise the HWPX parser.", inherit_style=False)

    table = doc.add_table(2, 2)
    table.set_cell_text(0, 0, "Name")
    table.set_cell_text(0, 1, "Role")
    table.set_cell_text(1, 0, "Ada")
    table.set_cell_text(1, 1, "Engineer")

    img_buf = io.BytesIO()
    PILImage.new("RGB", (80, 50), color=(50, 100, 200)).save(img_buf, format="PNG")
    doc.add_picture(img_buf.getvalue(), "png")

    doc.save_to_path(FIXTURE_PATH)
    print(f"wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
