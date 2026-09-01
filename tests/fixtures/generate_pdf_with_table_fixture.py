"""One-time generator for tests/fixtures/sample_with_table.pdf.

Re-run with `uv run python tests/fixtures/generate_pdf_with_table_fixture.py`
if the fixture needs to be regenerated. Unlike sample.pdf (plain text only),
this fixture has a real gridded table and an embedded image so the PDF
parser's VLM-hybrid table/figure path (see parsers/pdf.py) has something to
detect via pdfplumber's find_tables()/page.images.
"""

import io
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FIXTURE_PATH = Path(__file__).parent / "sample_with_table.pdf"


def main() -> None:
    styles = getSampleStyleSheet()

    img_buf = io.BytesIO()
    PILImage.new("RGB", (100, 60), color=(200, 30, 30)).save(img_buf, format="PNG")
    img_buf.seek(0)

    doc = SimpleDocTemplate(str(FIXTURE_PATH), pagesize=letter)
    data = [["Company", "Sales", "Growth"], ["A", "100M", "20%"], ["B", "80M", "15%"]]
    table = Table(data)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))

    story = [
        Paragraph("Revenue Report", styles["Title"]),
        Spacer(1, 12),
        Paragraph("The table below summarizes quarterly revenue by company.", styles["Normal"]),
        Spacer(1, 12),
        table,
        Spacer(1, 12),
        RLImage(img_buf, width=100, height=60),
    ]
    doc.build(story)
    print(f"wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
