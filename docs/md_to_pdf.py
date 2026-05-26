# -*- coding: utf-8 -*-
"""
Generate PHASE1_PROCEDURES.pdf from PHASE1_PROCEDURES.md
Review-meeting quality: cover page, section headers, color blocks.
Pure ASCII source file - no Unicode in comments or strings.
"""

import re
from fpdf import FPDF

MD_PATH  = "PHASE1_PROCEDURES.md"
PDF_PATH = "PHASE1_PROCEDURES.pdf"

# Colour palette (R, G, B)
C_NAVY       = (15,  40,  90)
C_BLUE       = (30,  80, 180)
C_BLUE_LIGHT = (70, 120, 200)
C_GREEN      = (16, 120,  80)
C_GREEN_BG   = (230, 248, 238)
C_AMBER      = (160,  90,   0)
C_AMBER_BG   = (255, 248, 220)
C_RED        = (160,  30,  30)
C_RED_BG     = (255, 235, 235)
C_SLATE      = (55,  65,  81)
C_MUTED      = (107, 114, 128)
C_BORDER     = (200, 210, 225)
C_CODE_BG    = (245, 247, 250)
C_CODE_FG    = (30,  50,  80)
C_WHITE      = (255, 255, 255)
C_BLACK      = (20,  20,  20)
C_ROW_ALT    = (245, 248, 255)


REPLACEMENTS = [
    (u"—", "--"),
    (u"–", "-"),
    (u"‘", "'"),
    (u"’", "'"),
    (u"“", '"'),
    (u"”", '"'),
    (u"•", "*"),
    (u"…", "..."),
    (u"→", "->"),
    (u"←", "<-"),
    (u"✓", "OK"),
    (u"✔", "[x]"),
    (u"▶", ">"),
    (u"▼", "v"),
    (u"▲", "^"),
    (u"±", "+/-"),
    (u"×", "x"),
    (u"÷", "/"),
    (u"─", "-"),
    (u"│", "|"),
]


def esc(text):
    for src, dst in REPLACEMENTS:
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def strip_inline(text):
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*",     r"\1", text)
    text = re.sub(r"`(.+?)`",        r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    return text


class ReviewPDF(FPDF):
    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "B", 7)
        self.set_text_color(*C_MUTED)
        self.cell(0, 6,
            "ResearchFlow AI  |  Phase 1 Sprint Review Report  |  19 May 2026",
            align="L")
        self.ln(0)
        self.set_draw_color(*C_BORDER)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def footer(self):
        if self.page_no() == 1:
            return
        self.set_y(-14)
        self.set_draw_color(*C_BORDER)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(1)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*C_MUTED)
        self.cell(0, 6, "Page %d" % (self.page_no() - 1), align="C")

    def cover(self):
        self.add_page()

        # Top accent bar
        self.set_fill_color(*C_NAVY)
        self.rect(0, 0, 210, 28, "F")

        self.set_xy(10, 8)
        self.set_font("Helvetica", "B", 16)
        self.set_text_color(*C_WHITE)
        self.cell(0, 10, "ResearchFlow AI", align="L")

        self.set_xy(10, 18)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(180, 200, 230)
        self.cell(0, 6, "Agentic Research Automation  |  Human-in-the-Loop Orchestration",
                  align="L")

        self.set_xy(10, 48)
        self.set_font("Helvetica", "B", 28)
        self.set_text_color(*C_NAVY)
        self.multi_cell(0, 12, "Phase 1\nSprint Review Report", align="L")

        self.set_xy(10, 82)
        self.set_font("Helvetica", "", 12)
        self.set_text_color(*C_SLATE)
        self.cell(0, 8, "Query & Discovery  |  End-to-End HITL Workflow", align="L")

        self.set_fill_color(*C_BLUE)
        self.rect(10, 96, 60, 2, "F")

        # Status badge
        self.set_xy(10, 104)
        self.set_fill_color(*C_GREEN_BG)
        self.set_draw_color(*C_GREEN)
        self.set_line_width(0.5)
        self.rect(10, 104, 70, 10, "FD")
        self.set_xy(12, 106)
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*C_GREEN)
        self.cell(66, 6, "STATUS: COMPLETE", align="L")

        meta = [
            ("Review Date",   "19 May 2026"),
            ("Branch",        "feature/phase-1"),
            ("Total Commits", "24"),
            ("LLM Model",     "Gemini 2.0 Flash"),
            ("Status",        "All B1-B4 blockers resolved"),
        ]
        y = 122
        for label, value in meta:
            self.set_xy(10, y)
            self.set_font("Helvetica", "B", 9)
            self.set_text_color(*C_SLATE)
            self.cell(48, 7, label, align="L")
            self.set_font("Helvetica", "", 9)
            self.set_text_color(*C_BLACK)
            self.cell(0, 7, value, align="L")
            y += 7

        # Bottom accent bar
        self.set_fill_color(*C_NAVY)
        self.rect(0, 280, 210, 17, "F")
        self.set_xy(10, 283)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(160, 180, 220)
        self.cell(0, 6,
            "Confidential - Internal Review Document  |  ResearchFlow AI  |  2026",
            align="C")

    # Helpers
    def section_h1(self, text):
        self.ln(6)
        y = self.get_y()
        self.set_fill_color(*C_NAVY)
        self.rect(15, y, 180, 9, "F")
        self.set_xy(17, y + 0.5)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*C_WHITE)
        self.multi_cell(176, 8, esc(strip_inline(text)), align="L")
        self.ln(2)
        self.set_text_color(*C_BLACK)

    def section_h2(self, text):
        self.ln(5)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*C_BLUE)
        self.set_x(self.l_margin)
        self.multi_cell(self._body_w(), 7, esc(strip_inline(text)))
        y = self.get_y()
        self.set_draw_color(*C_BLUE_LIGHT)
        self.set_line_width(0.4)
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(3)
        self.set_text_color(*C_BLACK)

    def section_h3(self, text):
        self.ln(3)
        self.set_font("Helvetica", "B", 10)
        self.set_text_color(*C_SLATE)
        self.set_x(self.l_margin)
        self.multi_cell(self._body_w(), 6, esc(strip_inline(text)))
        self.ln(1)
        self.set_text_color(*C_BLACK)

    def section_h4(self, text):
        self.ln(2)
        self.set_font("Helvetica", "BI", 9)
        self.set_text_color(*C_AMBER)
        self.set_x(self.l_margin)
        self.multi_cell(self._body_w(), 5, esc(strip_inline(text)))
        self.ln(1)
        self.set_text_color(*C_BLACK)

    def _body_w(self):
        return self.w - self.l_margin - self.r_margin

    def para(self, text):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*C_BLACK)
        self.set_x(self.l_margin)
        self.multi_cell(self._body_w(), 5, esc(strip_inline(text)))
        self.ln(1)

    def bullet_item(self, text, indent=0):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*C_BLACK)
        prefix = "  " * indent + "- "
        self.set_x(self.l_margin)
        self.multi_cell(self._body_w(), 5, esc(strip_inline(prefix + text)))

    def numbered_item(self, text, n, indent=0):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*C_BLACK)
        prefix = "  " * indent + ("%d. " % n)
        self.set_x(self.l_margin)
        self.multi_cell(self._body_w(), 5, esc(strip_inline(prefix + text)))

    def hr(self):
        self.ln(2)
        self.set_draw_color(*C_BORDER)
        self.set_line_width(0.3)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def code_block(self, lines):
        h_per_line = 4.5
        total_h = len(lines) * h_per_line + 5
        if self.get_y() + total_h > 270:
            self.add_page()
        y_start = self.get_y()
        self.set_fill_color(*C_CODE_BG)
        self.set_draw_color(*C_BORDER)
        self.set_line_width(0.3)
        self.rect(10, y_start, 190, total_h, "FD")
        self.set_xy(13, y_start + 2)
        self.set_font("Courier", "", 7.5)
        self.set_text_color(*C_CODE_FG)
        for cl in lines:
            self.set_x(13)
            self.multi_cell(184, h_per_line, esc(cl))
        self.ln(3)
        self.set_text_color(*C_BLACK)

    def table_block(self, rows):
        if not rows:
            return
        col_count = max(len(r) for r in rows)
        col_w = 190.0 / col_count
        header_done = False
        alt = False
        for row in rows:
            joined = "|".join(row)
            if re.match(r"^[-| :]+$", joined):
                continue
            if not header_done:
                self.set_fill_color(*C_NAVY)
                self.set_text_color(*C_WHITE)
                self.set_font("Helvetica", "B", 8)
                for cell in row:
                    self.cell(col_w, 6, esc(strip_inline(cell)), border=1, fill=True)
                self.ln()
                header_done = True
            else:
                self.set_fill_color(*(C_ROW_ALT if alt else C_WHITE))
                self.set_text_color(*C_BLACK)
                self.set_font("Helvetica", "", 8)
                for cell in row:
                    self.cell(col_w, 6, esc(strip_inline(cell)), border=1, fill=True)
                self.ln()
                alt = not alt
        self.ln(3)
        self.set_text_color(*C_BLACK)


def render(pdf, path):
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    in_code   = False
    code_buf  = []
    table_buf = []
    in_table  = False
    num_ctr   = 0

    def flush_table():
        nonlocal table_buf, in_table
        if table_buf:
            pdf.table_block(table_buf)
        table_buf = []
        in_table = False

    for raw in lines:
        line = raw.rstrip("\n")

        # code fence
        if line.strip().startswith("```"):
            if in_table:
                flush_table()
            if not in_code:
                in_code = True
                code_buf = []
            else:
                in_code = False
                pdf.code_block(code_buf)
            continue

        if in_code:
            code_buf.append(line)
            continue

        # table row
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            table_buf.append(cells)
            in_table = True
            continue
        else:
            if in_table:
                flush_table()

        # headings
        if re.match(r"^#### ", line):
            num_ctr = 0
            pdf.section_h4(line[5:])
            continue
        if re.match(r"^### ", line):
            num_ctr = 0
            pdf.section_h3(line[4:])
            continue
        if re.match(r"^## ", line):
            num_ctr = 0
            pdf.section_h2(line[3:])
            continue
        if re.match(r"^# ", line):
            num_ctr = 0
            pdf.section_h1(line[2:])
            continue

        # horizontal rule
        if re.match(r"^---+\s*$", line):
            pdf.hr()
            continue

        # bullet
        m = re.match(r"^(\s*)[-*] (.+)", line)
        if m:
            num_ctr = 0
            pdf.bullet_item(m.group(2), indent=len(m.group(1)) // 2)
            continue

        # numbered list
        m = re.match(r"^(\s*)\d+\. (.+)", line)
        if m:
            num_ctr += 1
            pdf.numbered_item(m.group(2), num_ctr, indent=len(m.group(1)) // 2)
            continue

        # blank line
        if not line.strip():
            num_ctr = 0
            pdf.ln(2)
            continue

        # paragraph
        num_ctr = 0
        pdf.para(line)

    if in_table:
        flush_table()


def main():
    pdf = ReviewPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(15, 20, 15)

    pdf.cover()
    pdf.add_page()
    render(pdf, MD_PATH)

    pdf.output(PDF_PATH)
    print("PDF saved -> " + PDF_PATH)


if __name__ == "__main__":
    main()
