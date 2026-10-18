# Expertise Navigator designs

Exported from two Claude Design canvases. Open `index.html` to browse.

## What to build from

`flows/final/` is the chosen design: 17 screens, each one HTML file with inline styles at 1280 px wide. Build the app to match these. `flows/explorations/` holds the 10 alternative flows explored first; use them only for ideas.

| # | File | Screen |
|---|------|--------|
| 1 | `final/X1-Home.dc.html` | 1 Home: leads and drafts |
| 2 | `final/X2-Brief.dc.html` | 2 Brief |
| 3 | `final/X3-Matches.dc.html` | 3 Matches |
| 4 | `final/X5-Pitch.dc.html` | 5 Pitch builder |
| 5 | `final/X6-Review.dc.html` | 6 Review and export |
| 6 | `final/X7-Archives.dc.html` | 7 Archives |
| 7 | `final/X4a-Map-Vertical.dc.html` | 4a Evidence map: vertical |
| 8 | `final/X12-Eval.dc.html` | 8 Evaluation |
| 9 | `final/X2b-Brief-Sources.dc.html` | 2b Brief from PDF and website |
| 10 | `final/X8-States.dc.html` | 9 Empty, searching, no match, error |
| 11 | `final/X9-Mutz.dc.html` | 10 Mutz open with tools |
| 12 | `final/X10-Lead.dc.html` | 11 After sending: timeline and pitch v2 |
| 13 | `final/X11-Follow.dc.html` | 12 Visit accepted: contact researchers |
| 14 | `final/X13-WhyNot.dc.html` | 13 Why not X? Mutz explains a ranking |
| 15 | `final/X14-Pipeline.dc.html` | 14 Case pipeline |
| 16 | `final/X15-Gaps.dc.html` | 15 Insights: gap report |
| 17 | `final/X16-Capability.dc.html` | 16 Insights: Bern capability map |

## Notes from the canvas

- Home holds every lead and draft. Each case runs Brief, Matches, Evidence map, Pitch. Archives sits next to Home in the top bar.
- PDF and website input show used, ignored and struck passages. States around the happy path. Mutz open with sourced answers and confirm buttons. After sending: timeline, reevaluate, pitch v2, then intro drafts and the visit day.
- Why not X makes the ranking inspectable. Pipeline is a second view of Leads. Every triaged lead leaves an anonymous trace that feeds the gap report and the capability map under Insights.

## Tokens used in the screens

- Red `#E30613` (actions, the vertical rule, active tab), hover `#B8000F`, tint `#FCE3E5`
- Black `#111111`, grey text `#5E5E5E`, hairlines `#D9D9D9`, panel `#FAFAFA`, paper `#FFFFFF`
- Type: "Helvetica Neue", Helvetica, Arial, sans-serif; 28 / 24 / 21 / 18 / 15 / 13 / 12 px
- Square corners (2 px on inputs), buttons 44 px tall, one vertical red rule between the two columns
- Check the red against the Canton of Bern manual; the manual wins if they differ.

## Mascot

`mascots/mascot-options.dc.html` shows 10 options. The final flow uses the black bear head with white ear centres and a red scarf bar, drawn as a small inline SVG at the bottom right of every screen.

## Technical note

The files come from the Design tool. The `<x-dc>`, `<helmet>` and `support.js` parts are that tool's wrapper and can be ignored: browsers render the markup inside as plain HTML.
