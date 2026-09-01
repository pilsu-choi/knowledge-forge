TABLE_PROMPT = """\
This image is a cropped region of a document page containing a table. \
Transcribe it as a GitHub-Flavored Markdown table: a header row, a \
`---|---|...` separator row, then one row per data row. Preserve every \
cell's text exactly as shown (numbers, units, Korean/English text). If a \
cell is empty, leave it blank rather than guessing a value. Output ONLY the \
markdown table - no preamble, no explanation, no code fence."""

FIGURE_PROMPT = """\
This image is a cropped region of a document page containing a figure, \
chart, diagram, or photo. Write a concise paragraph (2-5 sentences) \
describing what it shows: for a chart, name the axes/series and the key \
trend or values; for a diagram, describe the components and relationships \
it depicts; for a photo, describe what is depicted. Ground every claim in \
what is visible - do not invent data points or labels you cannot read. \
Output ONLY the description - no preamble."""

TRANSCRIBE_PROMPT = """\
This image is a standalone document page or scanned image (not a cropped \
region of a larger page). If it primarily contains text, transcribe that \
text faithfully, preserving line breaks and paragraph structure. If it \
primarily contains a chart, diagram, or photo rather than text, describe it \
concisely instead (axes/series and key trend for a chart; components and \
relationships for a diagram; what is depicted for a photo). Output ONLY the \
transcription or description - no preamble, no commentary."""
