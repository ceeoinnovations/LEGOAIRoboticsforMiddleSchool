#!/usr/bin/env python3
"""Generate printable "placemat" pages from the lesson content already in
index.html, styled to match the "Engineering with Bricks" placemat template
(see /Downloads/LEGO ML and AI Placemats.pdf).

This is a proof of concept: it reads index.html's Challenge / Think Like /
Building Ideas markup directly (no separate content file to keep in sync) so
a placemat regenerates from whatever is currently in the app. Only the
Challenge-box front side of the template is implemented so far; the
Building Tips / Code It / Challenge Yourself back side is future work.

Usage:
    python3 generate_placemats.py            # render every lesson found
    python3 generate_placemats.py l2         # render just lesson id "l2"
    python3 generate_placemats.py --pdf      # also export PDF via headless Chrome (macOS only)
"""
import html
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "index.html"
OUT_DIR = Path(__file__).resolve().parent / "out"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

CORAL = "#e8635f"
TEAL = "#1a8a8a"
TEAL_LIGHT = "#c3ecec"


def strip_tags(fragment: str) -> str:
    """Collapse inline markup (e.g. <em>a programmer</em>) to text, unescape entities."""
    text = re.sub(r"<[^>]+>", "", fragment)
    return html.unescape(" ".join(text.split()))


def extract_lessons(index_html: str):
    """Split index.html on each lesson's <div class="page-content" id="page-...">
    block and pull out its title, challenge box, think-like boxes, and (if
    present) a Building Ideas grid. Assumes challenge-box/think-like-box
    contain no nested <div> tags, which holds for every lesson block today."""
    chunks = re.split(r'(?=<div class="page-content[^"]*" id="page-([\w]+)")', index_html)
    lessons = []
    for chunk in chunks:
        id_match = re.match(r'<div class="page-content[^"]*" id="page-([\w]+)"', chunk)
        if not id_match:
            continue
        lesson_id = id_match.group(1)

        title_match = re.search(r"<h2>(.*?)</h2>", chunk, re.S)
        title = strip_tags(title_match.group(1)) if title_match else lesson_id

        challenge_match = re.search(r'<div class="challenge-box">(.*?)</div>', chunk, re.S)
        challenge_paragraphs, challenge_img = [], None
        if challenge_match:
            body = challenge_match.group(1)
            challenge_paragraphs = [strip_tags(p) for p in re.findall(r"<p>(.*?)</p>", body, re.S)]
            img_match = re.search(r'<img\s+src="([^"]+)"[^>]*alt="([^"]*)"', body, re.S)
            if img_match:
                challenge_img = (img_match.group(1), html.unescape(img_match.group(2)))

        think_boxes = []
        for box in re.findall(r'<div class="think-like-box">(.*?)</div>', chunk, re.S):
            h3_match = re.search(r"<h3>(.*?)</h3>", box, re.S)
            p_match = re.search(r"<p>(.*?)</p>", box, re.S)
            if h3_match and p_match:
                think_boxes.append((strip_tags(h3_match.group(1)), strip_tags(p_match.group(1))))

        building_ideas = []
        bi_match = re.search(
            r"Building Ideas</h3>\s*<div class=\"building-ideas-grid\">(.*?)</div>", chunk, re.S
        )
        if bi_match:
            for src, alt in re.findall(r'<img\s+src="([^"]+)"[^>]*alt="([^"]*)"', bi_match.group(1), re.S):
                building_ideas.append((src, html.unescape(alt)))

        if challenge_match or think_boxes:
            lessons.append(
                {
                    "id": lesson_id,
                    "title": title,
                    "challenge_paragraphs": challenge_paragraphs,
                    "challenge_img": challenge_img,
                    "think_boxes": think_boxes,
                    "building_ideas": building_ideas,
                }
            )
    return lessons


def render_placemat(lesson: dict) -> str:
    challenge_p_html = "\n".join(f"<p>{p}</p>" for p in lesson["challenge_paragraphs"]) or "<p></p>"
    challenge_img_html = ""
    if lesson["challenge_img"]:
        src, alt = lesson["challenge_img"]
        challenge_img_html = f'<img class="challenge-img" src="../../images/{src.split("images/")[-1]}" alt="{alt}">'

    think_html = ""
    for heading, question in lesson["think_boxes"]:
        # heading looks like "Think Like a programmer" / "Think like an engineer"
        m = re.match(r"(?i)think\s+like\s+(.*)", heading)
        who = m.group(1) if m else heading
        think_html += f"""
        <fieldset class="think-box">
          <legend>Think Like <em>{who}</em></legend>
          <p>{question}</p>
        </fieldset>"""

    building_html = ""
    if lesson["building_ideas"]:
        for src, alt in lesson["building_ideas"][:3]:
            building_html += f'<img class="building-idea-img" src="../../images/{src.split("images/")[-1]}" alt="{alt}">'
    else:
        building_html = '<div class="building-idea-slot"></div>' * 3

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Placemat &mdash; {lesson['title']}</title>
<style>
  @page {{ size: 11in 8.5in; margin: 0; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    color: #333;
    width: 11in;
    height: 8.5in;
    padding: 0.35in 0.5in;
    display: flex;
    flex-direction: column;
  }}
  .header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }}
  .badge {{
    display: inline-block;
    background: {TEAL_LIGHT};
    color: #14504f;
    font-weight: 700;
    font-size: 0.85rem;
    padding: 4px 12px;
    border-radius: 4px;
    margin-bottom: 6px;
  }}
  h1 {{ font-size: 1.7rem; font-weight: 800; margin: 0; color: #444; }}
  .logo {{ height: 40px; }}
  /* Fixed box dimensions per the reference placemat template:
     Challenge 6.39in x 4.55in, Think Like 3.12in x 1.3in each,
     Building Ideas 3in x 2.12in each (3 stacked in the sidebar). */
  .body {{ display: flex; align-items: flex-start; gap: 0.3in; }}
  .challenge-col {{ width: 6.39in; display: flex; flex-direction: column; gap: 0.15in; flex: none; }}
  /* Both boxes use <fieldset>/<legend> so the border breaks around the
     label like the reference template's notched Challenge/Think Like
     headings, instead of the label sitting inside a solid box. */
  fieldset {{ margin: 0; min-width: 0; }}
  .challenge-box {{
    box-sizing: border-box;
    width: 6.39in;
    height: 4.55in;
    border: 3px solid {CORAL};
    border-radius: 4px;
    padding: 4px 16px 10px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }}
  .challenge-box legend {{ color: {CORAL}; font-weight: 700; font-size: 1.15rem; padding: 0 8px; margin-left: 4px; }}
  .challenge-box p {{ margin: 4px 0 6px; font-size: 1rem; }}
  .challenge-img {{ flex: 1; min-height: 0; width: 100%; object-fit: contain; }}
  .think-row {{ width: 6.39in; display: flex; gap: 0.15in; }}
  .think-box {{
    box-sizing: border-box;
    width: 3.12in;
    height: 1.3in;
    flex: none;
    border: 2px solid {TEAL};
    border-radius: 4px;
    padding: 2px 12px 8px;
    overflow: hidden;
  }}
  .think-box legend {{ color: {TEAL}; font-weight: 700; font-size: 0.95rem; padding: 0 6px; margin-left: 3px; }}
  .think-box legend em {{ text-decoration: underline; font-style: italic; }}
  .think-box p {{ margin: 2px 0 0; font-size: 0.9rem; }}
  .sidebar {{ flex: none; display: flex; flex-direction: column; }}
  .sidebar-header {{
    width: 3in;
    height: auto;
    display: block;
    margin-bottom: 0.1in;
  }}
  .building-idea-img {{
    box-sizing: border-box;
    width: 3in;
    height: 2.12in;
    object-fit: contain;
    margin-bottom: 0.06in;
  }}
  .building-idea-slot {{
    box-sizing: border-box;
    width: 3in;
    height: 2.12in;
    border-bottom: 1px solid #ccc;
    margin-bottom: 0.06in;
  }}
  .footer {{ margin-top: 8px; font-size: 0.65rem; color: #999; text-align: center; }}
</style>
</head>
<body>
  <div class="header">
    <div>
      <div class="badge">Grades 6&ndash;8 / Ages 11&ndash;14</div>
      <h1>&#10024; {lesson['title']}</h1>
    </div>
    <img class="logo" src="../../images/EngineeringWithBricks.png" alt="Engineering with Bricks logo">
  </div>
  <div class="body">
    <div class="challenge-col">
      <fieldset class="challenge-box">
        <legend>Challenge</legend>
        {challenge_p_html}
        {challenge_img_html}
      </fieldset>
      <div class="think-row">
        {think_html}
      </div>
    </div>
    <div class="sidebar">
      <img class="sidebar-header" src="../../images/buildingideas.png" alt="Building Ideas">
      {building_html}
    </div>
  </div>
  <div class="footer">AI School of Magic &mdash; auto-generated from index.html; edit lesson content there, then re-run generate_placemats.py</div>
</body>
</html>
"""


def main():
    args = sys.argv[1:]
    want_pdf = "--pdf" in args
    lesson_ids = [a for a in args if not a.startswith("--")]

    index_html = INDEX_HTML.read_text()
    lessons = extract_lessons(index_html)
    if lesson_ids:
        lessons = [l for l in lessons if l["id"] in lesson_ids]

    OUT_DIR.mkdir(exist_ok=True)
    for lesson in lessons:
        out_html = OUT_DIR / f"placemat_{lesson['id']}.html"
        out_html.write_text(render_placemat(lesson))
        print(f"wrote {out_html.relative_to(ROOT.parent)}")

        if want_pdf:
            out_pdf = OUT_DIR / f"placemat_{lesson['id']}.pdf"
            result = subprocess.run(
                [
                    CHROME,
                    "--headless",
                    "--disable-gpu",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={out_pdf}",
                    out_html.resolve().as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and out_pdf.exists():
                print(f"wrote {out_pdf.relative_to(ROOT.parent)}")
            else:
                print(f"PDF export failed for {lesson['id']}: {result.stderr.strip()}", file=sys.stderr)


if __name__ == "__main__":
    main()
