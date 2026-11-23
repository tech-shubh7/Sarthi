# Sarthi brand palette

This is Sarthi's standard palette. Use it for the dashboard, the website, screenshots, social images and
anything else that carries the Sarthi name.

| Colour | Hex | Role |
|---|---|---|
| Sarthi blue | `#233A8B` | Primary: logo, links, buttons, the active tab, headings that need colour |
| Amber | `#F4A024` | Highlight: counts that need attention, "fair" matches, calls to look |
| Green | `#20B486` | Success: strong matches, "sent", saved |
| Charcoal | `#1F2937` | Text |
| Cream | `#F9F6EF` | Page background (light only; never pure white for large areas) |

## Using it

- **Light theme only.** Cream page, slightly lighter cards (`#FFFDF8`), soft lines (`#E7E1D4`).
- **Text must stay readable** (WCAG AA: 4.5:1 for normal text). On cream, charcoal is 13.6:1 and Sarthi
  blue 9.5:1. Green (2.5:1) and amber (2.0:1) are too light for text, so they are used as given for fills,
  bars, borders and badges (charcoal on amber is 6.9:1), and small coloured text uses a darker shade of the
  same hue: green `#137A5A` (4.9:1), amber `#9A5A00` (5.1:1). Secondary text is `#4B5563` (7.0:1), hints
  `#6B7280` (4.6:1). White on Sarthi blue, for buttons, is 10.3:1.
- **Errors** use a muted red (`#B4382C`), outside the brand colours on purpose: it should only ever mean
  something went wrong.
- **Soft backgrounds** for pills and notices are light tints of the same hues: blue `#E6EAF6`, green
  `#DFF4EC`, amber `#FDF0DB`.

In code, the palette lives as CSS variables at the top of
[`src/sarthi/web/static/app.css`](../src/sarthi/web/static/app.css) (`--brand-blue`, `--brand-amber`,
`--brand-green`, `--brand-charcoal`, `--brand-cream`); everything else is derived from them.

Generated resumes and cover letters stay neutral (black text, one quiet accent) on purpose: they are the
user's documents, read by recruiters and hiring systems, not Sarthi's brand.
