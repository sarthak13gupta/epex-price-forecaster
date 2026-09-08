"""
Render docs/DESIGN.md to a Word document.

Handles the Markdown subset the design doc uses: ATX headings, paragraphs,
bullet and ordered lists, fenced code blocks, GFM pipe tables, standalone
images, blockquote callouts, horizontal rules, and inline code / bold / links.

Usage:
    python scripts/md_to_docx.py docs/DESIGN.md docs/DESIGN.docx
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

# Palette carried over from the HTML design so both renderings match.
INK = RGBColor(0x13, 0x1B, 0x1E)
INK_MUTED = RGBColor(0x56, 0x6A, 0x6E)
ACCENT = RGBColor(0x0E, 0x5D, 0x6E)
WARM = RGBColor(0xA9, 0x54, 0x1F)

BODY_FONT = "Georgia"
HEAD_FONT = "Segoe UI"
MONO_FONT = "Consolas"

CODE_SHADE = "F1F4F4"
HEADER_SHADE = "E7ECEC"
CALLOUT_SHADE = "DCEAED"

CONTENT_WIDTH_IN = 6.5


# --------------------------------------------------------------------- helpers


def _shade(element, hex_fill: str) -> None:
    """Applies a solid background fill to a paragraph or table cell."""
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    element.get_or_add_pPr().append(shd) if element.tag.endswith("}p") else None


def _shade_paragraph(paragraph, hex_fill: str) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    pPr.append(shd)


def _shade_cell(cell, hex_fill: str) -> None:
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def _left_bar(paragraph, hex_color: str) -> None:
    """Draws a single left border, used for blockquote callouts."""
    pPr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), "18")
    left.set(qn("w:space"), "8")
    left.set(qn("w:color"), hex_color)
    borders.append(left)
    pPr.append(borders)


# Inline markup: `code`, **bold**, *italic*, [text](url)
_INLINE = re.compile(
    r"(`[^`]+`)"
    r"|(\*\*[^*]+\*\*)"
    r"|(\*[^*]+\*)"
    r"|(\[[^\]]+\]\([^)]+\))"
)


def add_inline(paragraph, text: str, base_size: float = 10.5,
               base_color: RGBColor = INK, base_font: str = BODY_FONT) -> None:
    """Writes `text` into `paragraph`, honouring inline Markdown markup."""
    pos = 0
    for match in _INLINE.finditer(text):
        if match.start() > pos:
            run = paragraph.add_run(text[pos:match.start()])
            run.font.name = base_font
            run.font.size = Pt(base_size)
            run.font.color.rgb = base_color

        token = match.group(0)
        if token.startswith("`"):
            run = paragraph.add_run(token[1:-1])
            run.font.name = MONO_FONT
            run.font.size = Pt(base_size - 1)
            run.font.color.rgb = ACCENT
        elif token.startswith("**"):
            run = paragraph.add_run(token[2:-2])
            run.font.name = base_font
            run.font.size = Pt(base_size)
            run.font.color.rgb = base_color
            run.bold = True
        elif token.startswith("["):
            label = token[1 : token.index("]")]
            run = paragraph.add_run(label)
            run.font.name = base_font
            run.font.size = Pt(base_size)
            run.font.color.rgb = ACCENT
            run.underline = True
        else:  # *italic*
            run = paragraph.add_run(token[1:-1])
            run.font.name = base_font
            run.font.size = Pt(base_size)
            run.font.color.rgb = base_color
            run.italic = True

        pos = match.end()

    if pos < len(text):
        run = paragraph.add_run(text[pos:])
        run.font.name = base_font
        run.font.size = Pt(base_size)
        run.font.color.rgb = base_color


# ---------------------------------------------------------------- block writers


def write_heading(doc: Document, level: int, text: str) -> None:
    sizes = {1: 21, 2: 15.5, 3: 12.5, 4: 10.5}
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(20 if level > 1 else 0)
    paragraph.paragraph_format.space_after = Pt(6)
    paragraph.paragraph_format.keep_with_next = True

    add_inline(
        paragraph, text,
        base_size=sizes.get(level, 10.5),
        base_color=ACCENT if level <= 2 else INK,
        base_font=HEAD_FONT,
    )
    for run in paragraph.runs:
        run.bold = True
        run.font.name = HEAD_FONT

    if level == 2:
        pPr = paragraph._p.get_or_add_pPr()
        borders = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "8")
        bottom.set(qn("w:space"), "4")
        bottom.set(qn("w:color"), "0E5D6E")
        borders.append(bottom)
        pPr.append(borders)


def write_paragraph(doc: Document, text: str) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(9)
    paragraph.paragraph_format.line_spacing = 1.25
    add_inline(paragraph, text)


def write_list_item(doc: Document, text: str, ordered: bool) -> None:
    style = "List Number" if ordered else "List Bullet"
    paragraph = doc.add_paragraph(style=style)
    paragraph.paragraph_format.space_after = Pt(5)
    paragraph.paragraph_format.line_spacing = 1.2
    add_inline(paragraph, text)


def write_code_block(doc: Document, lines: list[str]) -> None:
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    cell = table.rows[0].cells[0]
    _shade_cell(cell, CODE_SHADE)

    cell.paragraphs[0]._p.getparent().remove(cell.paragraphs[0]._p)
    for line in lines:
        paragraph = cell.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = 1.0
        run = paragraph.add_run(line if line else " ")
        run.font.name = MONO_FONT
        run.font.size = Pt(8)
        run.font.color.rgb = INK

    doc.add_paragraph().paragraph_format.space_after = Pt(6)


def write_callout(doc: Document, lines: list[str]) -> None:
    """Renders a blockquote as a shaded, left-barred callout."""
    body = "\n".join(lines).strip()
    blocks = [b.strip() for b in body.split("\n\n") if b.strip()]

    for index, block in enumerate(blocks):
        flat = " ".join(block.split())
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.left_indent = Inches(0.12)
        paragraph.paragraph_format.space_before = Pt(8 if index == 0 else 3)
        paragraph.paragraph_format.space_after = Pt(3 if index < len(blocks) - 1 else 10)
        paragraph.paragraph_format.line_spacing = 1.2
        _shade_paragraph(paragraph, CALLOUT_SHADE)
        _left_bar(paragraph, "0E5D6E")
        add_inline(paragraph, flat, base_size=10)
        if index == 0:
            for run in paragraph.runs:
                run.bold = True


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def write_table(doc: Document, rows: list[list[str]]) -> None:
    header, *body = rows
    table = doc.add_table(rows=1, cols=len(header))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = True

    for i, label in enumerate(header):
        cell = table.rows[0].cells[i]
        _shade_cell(cell, HEADER_SHADE)
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_after = Pt(2)
        add_inline(paragraph, label, base_size=8.5, base_font=HEAD_FONT,
                   base_color=INK_MUTED)
        for run in paragraph.runs:
            run.bold = True

    for record in body:
        cells = table.add_row().cells
        for i, value in enumerate(record[: len(header)]):
            paragraph = cells[i].paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(2)
            paragraph.paragraph_format.line_spacing = 1.1
            add_inline(paragraph, value, base_size=9)

    doc.add_paragraph().paragraph_format.space_after = Pt(6)


def write_image(doc: Document, alt: str, path: Path, base_dir: Path) -> None:
    resolved = (base_dir / path).resolve()
    if not resolved.exists():
        write_paragraph(doc, f"[missing image: {path}]")
        return

    doc.add_picture(str(resolved), width=Inches(CONTENT_WIDTH_IN))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

    caption = doc.add_paragraph()
    caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
    caption.paragraph_format.space_after = Pt(12)
    run = caption.add_run(alt)
    run.font.name = HEAD_FONT
    run.font.size = Pt(8)
    run.font.color.rgb = INK_MUTED
    run.italic = True


def write_rule(doc: Document) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(4)
    pPr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "CBD6D7")
    borders.append(bottom)
    pPr.append(borders)


# ------------------------------------------------------------------- the parser


IMAGE_RE = re.compile(r"^!\[(?P<alt>.*?)\]\((?P<src>[^)]+)\)\s*$")
HEADING_RE = re.compile(r"^(?P<hashes>#{1,4})\s+(?P<text>.*)$")
BULLET_RE = re.compile(r"^[-*]\s+(?P<text>.*)$")
ORDERED_RE = re.compile(r"^\d+\.\s+(?P<text>.*)$")


def convert(md_path: Path, docx_path: Path) -> None:
    lines = md_path.read_text().split("\n")
    base_dir = md_path.parent

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Inches(0.85)
    section.bottom_margin = Inches(0.85)
    section.left_margin = Inches(1.0)
    section.right_margin = Inches(1.0)

    style = doc.styles["Normal"]
    style.font.name = BODY_FONT
    style.font.size = Pt(10.5)
    style.font.color.rgb = INK

    i = 0
    pending_list: list[tuple[str, bool]] = []

    def flush_list() -> None:
        for text, ordered in pending_list:
            write_list_item(doc, text, ordered)
        pending_list.clear()

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        # fenced code
        if line.startswith("```"):
            flush_list()
            i += 1
            block: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i].rstrip())
                i += 1
            i += 1
            write_code_block(doc, block)
            continue

        # blockquote callout
        if line.startswith(">"):
            flush_list()
            block = []
            while i < len(lines) and lines[i].startswith(">"):
                block.append(lines[i].lstrip(">").strip())
                i += 1
            write_callout(doc, block)
            continue

        # pipe table
        if line.startswith("|") and i + 1 < len(lines) and set(
            lines[i + 1].replace("|", "").replace(":", "").strip()
        ) <= {"-", " "} and "-" in lines[i + 1]:
            flush_list()
            rows = [_split_row(line)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            write_table(doc, rows)
            continue

        # standalone image
        match = IMAGE_RE.match(line)
        if match:
            flush_list()
            write_image(doc, match.group("alt"), Path(match.group("src")), base_dir)
            i += 1
            continue

        # horizontal rule
        if line.strip() in ("---", "***", "___"):
            flush_list()
            write_rule(doc)
            i += 1
            continue

        # heading
        match = HEADING_RE.match(line)
        if match:
            flush_list()
            write_heading(doc, len(match.group("hashes")), match.group("text"))
            i += 1
            continue

        # list items, joining their continuation lines
        match = BULLET_RE.match(line) or ORDERED_RE.match(line)
        if match:
            ordered = ORDERED_RE.match(line) is not None
            text = match.group("text")
            i += 1
            while (
                i < len(lines)
                and lines[i].strip()
                and lines[i].startswith((" ", "\t"))
                and not BULLET_RE.match(lines[i].strip())
            ):
                text += " " + lines[i].strip()
                i += 1
            pending_list.append((text, ordered))
            continue

        # blank line
        if not line.strip():
            flush_list()
            i += 1
            continue

        # paragraph, joining soft-wrapped lines
        block = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not (
            lines[i].startswith(("#", ">", "|", "```", "---", "!["))
            or BULLET_RE.match(lines[i])
            or ORDERED_RE.match(lines[i])
        ):
            block.append(lines[i].strip())
            i += 1
        flush_list()
        write_paragraph(doc, " ".join(block))

    flush_list()
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(docx_path))
    print(f"[SUCCESS] wrote {docx_path}")


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(1)
    convert(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
