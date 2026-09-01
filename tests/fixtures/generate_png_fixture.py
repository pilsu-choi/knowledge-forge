"""One-time generator for tests/fixtures/sample.png.

Re-run with `uv run python tests/fixtures/generate_png_fixture.py` if the
fixture needs to be regenerated. Content doesn't need to contain readable
text - ImageParser tests mock pytesseract.image_to_string rather than
running real OCR (this sandbox has no tesseract-ocr binary installed).
"""

from pathlib import Path

from PIL import Image

FIXTURE_PATH = Path(__file__).parent / "sample.png"


def main() -> None:
    image = Image.new("RGB", (100, 40), color="white")
    image.save(FIXTURE_PATH)
    print(f"wrote {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
