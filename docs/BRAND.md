# Sarthi brand

<p align="center"><img src="brand/sarthi-wordmark-tagline-light.svg" alt="Sarthi, your AI job assistant" width="420"></p>

Sarthi means *charioteer*: the one who guides. The brand is built on that idea.

## The logo: "Guiding Reins"

- **The S** is two flowing reins: indigo above, teal below. They guide without pulling.
- **The forward arrow** in saffron gold is the job seeker's own agency: Sarthi guides, you move.
- **The peacock feather** is a quiet reference to Shree Krishna guiding Arjuna, without a literal figure.

| Asset | File | Use |
|---|---|---|
| Wordmark | [`brand/sarthi-wordmark-light.svg`](brand/sarthi-wordmark-light.svg) | Headers and navbars on a light background (140-200 px wide) |
| Wordmark, dark | [`brand/sarthi-wordmark-dark.svg`](brand/sarthi-wordmark-dark.svg) | On dark backgrounds |
| Wordmark with tagline | [`brand/sarthi-wordmark-tagline-light.svg`](brand/sarthi-wordmark-tagline-light.svg) | Landing pages, the top of documents |
| The S emblem | [`brand/sarthi-S-light.svg`](brand/sarthi-S-light.svg), [`brand/sarthi-S-dark.svg`](brand/sarthi-S-dark.svg) | Icons, favicons, small spaces (32 px or more) |
| App tile | [`brand/sarthi-app-navy.svg`](brand/sarthi-app-navy.svg) ([512 px PNG](brand/sarthi-app-navy-512.png)), [`brand/sarthi-app-ivory.svg`](brand/sarthi-app-ivory.svg) | Telegram bot photo, GitHub avatar, app icons |
| Social preview | [`brand/sarthi-social-preview.png`](brand/sarthi-social-preview.png) | Link cards (1200 x 630) |

**Rules**

- In the wordmark the custom S is the **only** S, followed by "arthi". Never add a second, typed S.
- The wordmarks here are tuned from the kit's masters: the S is about 17% larger, centred on "arthi", and
  its reins and arrow are slightly bolder (outlined in their own colour; the feather is untouched), so it
  reads as part of the word at small sizes. Use these, not the kit's first versions.
- Use the emblem at 32 px or more where you can; at 16 px the feather detail is lost.
- Don't recolour, stretch, rotate, outline or add effects. On dark backgrounds use the dark versions.
- Tagline: **YOUR AI JOB ASSISTANT**.

The full kit (every size, monochrome versions, headers, landing images, favicons) is kept by the
maintainers; ask in an issue if you need a file that is not here.

## Colours

| Colour | Hex | Meaning | Used for |
|---|---|---|---|
| Indigo | `#233A8B` | Trust, calm guidance | Primary: logo, links, buttons, the active tab |
| Saffron gold | `#F4A024` | Hope, forward action | Highlights: new-match counts, "fair" marks |
| Teal | `#20B486` | Growth, opportunity | Success: strong matches, "sent", saved |
| Charcoal | `#1F2937` | Readable | Text |
| Warm ivory | `#F9F6EF` | Welcoming | Page background |
| Deep navy | `#111C3D` | | Only for a dark canvas (Sarthi's own pages are light only) |

The same values are in [`brand/palette.json`](brand/palette.json) and, for the dashboard, as CSS variables
at the top of [`src/sarthi/web/static/app.css`](../src/sarthi/web/static/app.css) (`--brand-indigo`,
`--brand-saffron`, `--brand-teal`, `--brand-charcoal`, `--brand-ivory`).

## Readable text

Pages are light only: warm ivory, slightly lighter cards (`#FFFDF8`), soft lines (`#E7E1D4`), never large
areas of pure white. Every text colour meets WCAG AA (4.5:1 for normal text).

- On ivory, charcoal is 13.6:1 and indigo 9.5:1.
- Teal (2.5:1) and saffron (2.0:1) are too light for text. They are used as given for fills, bars,
  borders and badges (charcoal on saffron is 6.9:1). Small coloured text uses a darker shade of the same
  hue: teal `#137A5A` (4.9:1), saffron `#9A5A00` (5.1:1).
- Secondary text is `#4B5563` (7.0:1), hints `#6B7280` (4.6:1). White on indigo, for buttons, is 10.3:1.
- Errors use a muted red (`#B4382C`), outside the brand colours on purpose: it only ever means something
  went wrong. Soft backgrounds are light tints of the same hues: indigo `#E6EAF6`, teal `#DFF4EC`,
  saffron `#FDF0DB`.

Resumes and cover letters that Sarthi writes stay neutral on purpose: they are the user's documents, read
by recruiters and hiring systems, not Sarthi's brand.

## Name and logo

The code is AGPL-3.0. The Sarthi name and logo identify this project: you may use them to refer to Sarthi
(articles, talks, "works with Sarthi"), but please don't use them for a fork or another product in a way
that suggests it is the official Sarthi.
