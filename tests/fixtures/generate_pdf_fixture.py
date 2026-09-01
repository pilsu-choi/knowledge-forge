"""One-time generator for tests/fixtures/sample.pdf.

Re-run with `uv run python tests/fixtures/generate_pdf_fixture.py` if the
fixture needs to be regenerated. Uses a large bold title line followed by a
normal-size paragraph, which is what PDFParser's font-size heuristic
expects to distinguish a Heading from a TextBlock.
"""

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FIXTURE_PATH = Path(__file__).parent / "sample.pdf"


def main() -> None:
    c = canvas.Canvas(str(FIXTURE_PATH), pagesize=letter)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, 720, "Knowledge Forge Sample")
    c.setFont("Helvetica", 11)
    c.drawString(72, 690, "This is a short paragraph used to exercise the PDF parser.")
    c.drawString(72, 675, "It spans two lines that should merge into one text block.")
    c.save()
    print(f"wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
