# DESIGN.md (paste into every UI prompt)

1. **Palette with roles.** Red `#E30613` for actions, the vertical rule and the active tab only (hover `#B8000F`, tint `#FCE3E5`). Black `#111111` text, grey `#5E5E5E` secondary text, hairlines `#D9D9D9`, panel `#FAFAFA`, paper `#FFFFFF`. No other hues, no gradients, no yellow (not a Bernese colour).
2. **Type with roles.** `"Helvetica Neue", Helvetica, Arial, sans-serif`. Bold (Neue Helvetica 85) for titles and emphasis, regular (45) for text. Scale 28 / 24 / 21 / 18 / 15 / 13 / 12 px.
3. **Layout concept.** Swiss two column grid: labels left, content right, flush left, generous white space. Rows and tables, not cards. Square corners (2 px on inputs only), buttons 44 px tall.
4. **Signature element.** One thin vertical red rule dividing the two columns (the Canton's Linienelement), on every main screen and down the A4 teaser PDF.
5. **One place for personality.** Mutz, the black bear head with white ear centres and a red scarf bar, inline SVG bottom right. Everything else is quiet. Footer: "Prototype, not an official Canton of Bern tool".

## Checked against the Canton of Bern manual (kanton-bern-grundlagen-visuelles-erscheinungsbild.pdf, §2.1 to 2.3)

- Red: the manual defines it only as Pantone 032 C / CMYK 0 M100 Y100 K0 and gives no RGB or hex. `#E30613` is the usual sRGB rendering of CMYK 0/100/100/0, so the design red is consistent with the manual and is kept. (The playbook's `#EF3340` is the Pantone 032 C swatch approximation; we do not use it.)
- Type: manual says Neue Helvetica 85 for titles, 45 for body, Arial as the office fallback. Matches the design stack.
- Rule line: the manual prints the Linienelement **black, 0.15 mm**, red only on the Regierungsrat letterhead, and the coat of arms is always black. The designs use a red rule. We follow the designs on screen; the PDF teaser may use black. Flagged to the human.
- The app loads the logo (`assets/private/Logo_SF_BE_EN.png`) from the gitignored folder. A copy is committed in `docs/brand/` for the README header only; the organizers allow it only for this challenge.
