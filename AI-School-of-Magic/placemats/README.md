# Placemat generator (proof of concept)

Generates printable "placemat" pages styled after the Engineering with Bricks
placemat template, using the Challenge / Think Like / Building Ideas content
that already lives in `../index.html` — so a placemat regenerates from
whatever's currently in the app instead of a separately maintained copy.

## Usage

```
python3 generate_placemats.py            # render every lesson found in index.html
python3 generate_placemats.py l2         # render just one lesson (by its page-<id>)
python3 generate_placemats.py --pdf      # also export PDF via headless Chrome (macOS, needs Chrome installed)
```

Output goes to `out/` (gitignored — regenerate rather than commit).

## How it works

`extract_lessons()` scans `index.html` for each lesson's
`<div class="page-content" id="page-...">` block and pulls out its `<h2>`
title, `.challenge-box` (heading + paragraphs + optional image), the two
`.think-like-box` cards, and an optional `.building-ideas-grid`. No separate
content file — the app's own markup is the source of truth. This relies on
those specific elements never containing nested `<div>`s, which holds today.

## Box sizing

Matches the reference template's fixed dimensions rather than stretching to
fill the page: Challenge box 6.39in × 4.55in, each Think Like box 3.12in ×
1.3in, each Building Ideas slot 3in × 2.12in (three stacked in the sidebar,
truncating extra images past three — only Lesson 0 has that many today).
Lessons with no Building Ideas images render three empty slots (a bottom
rule, like the reference template's blank lines) so the sidebar height stays
consistent across lessons. The Challenge illustration and each Building
Ideas photo scale to fit their box (`object-fit: contain`) rather than being
cropped, and the Challenge illustration grows to fill whatever height the
challenge text doesn't use.

The Challenge and Think Like headings use `<fieldset>`/`<legend>` so the
border breaks around the label (matching the reference template's notched
heading style) instead of the label sitting inside a solid box.

## Known limitations (proof of concept, not production)

- Only the template's front side (Challenge / Think Like / Building Ideas) is
  implemented. The back side (Building Tips / Code It! / Challenge Yourself)
  isn't, since that content doesn't exist in `index.html` yet.
- No hand-drawn icons (the circular logo mark, the lightbulb) — "Building
  Ideas" uses a plain emoji bulb instead of the reference's teal line-art one.
- PDF export shells out to a hardcoded macOS Chrome path; no PDF export on
  other platforms without adjusting `CHROME`.
