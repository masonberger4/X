# Card design spec (house style: dark)

Cards are the pictures attached to a piece. They are what stops the scroll, and the
ones people screenshot and forward, so they get the same care as the text. The
reference cards in `exemplars/*/cards/` are the bar: look at them before you design.
The CTLA-4 and Merck cards are the house style. The SMMT cards are light-themed and
are there for their layouts only.

## Canvas
- 4:5 portrait, 1080 x 1350 CSS pixels, rendered at 2x (2160 x 2700 PNG). 4:5 shows
  uncropped in the X feed on phones. Use 16:9 (1600 x 900) only for a wide timeline
  that cannot work in portrait, and say why in piece.json.
- 46 to 56 px side padding. Nothing within 20 px of an edge.
- Build each card as one self-contained HTML file: inline CSS and inline SVG, no
  scripts, no external images, fonts or stylesheets. The renderer blocks the network,
  so anything external simply does not load.
- For a 16:9 card add `<meta name="card-size" content="1600x900">` to its head; every
  other card is drawn at 1080 x 1350.
- Size the page to the card: `html, body { margin: 0; width: 1080px; height: 1350px; }`
  and lay out inside it. Nothing may make the page larger than the card.
- Fill the canvas. A card with two rows of numbers floating in a tall empty panel reads
  as unfinished. When there is less to show, make it bigger (a hero number, larger type,
  stat tiles) or add what helps the reading (the caveat, a "why it matters" line), and
  let the panel end where its content ends. The app reports any empty band taller than
  a quarter of the card.
- You do not render the cards yourself. When you finish writing, the app draws every
  card (2x PNG) and checks it: text cut off in its box, text overlapping other text,
  text off the canvas or within 20 px of an edge, and large empty bands. It sends you the report and the
  PNG paths; open the PNGs with Read and look at them before you call a card done.

## Type
- `font-family: Inter` (installed for the renderer, weights 100 to 900) for
  everything; `'IBM Plex Mono'` (weights 400 and 600) for small labels, kickers and
  source lines when you want a technical feel. No other fonts are available.
- Headline 44 to 56 px, bold, tight letter-spacing. A hero headline (one per piece at
  most, on the headline card) may go up to 170 px uppercase.
- Body 18 to 22 px. Labels 13 to 16 px. Never below 13 px: the card is read on a phone.

## Colour
Surfaces and text
- page `#0d1117`, panels `#151a22` with 18 px rounded corners and a 1 px `#252c37`
  border, gridlines `#252c37` (hairlines), axis `#3a4250`
- text `#ffffff` (primary), `#c6ccd6` (secondary), `#8a93a3` (muted)

Series (these passed a colour-blind separation check on the dark page)
- orange `#d95926`: the subject of the piece (the drug, the company, the buyer)
- blue `#3987e5`: approved, on market, reported
- dark blue `#1c5cab`: pivotal stage, interpolated or second series
- green `#199e70`: a third group
- grey `#6b7484`: the comparator, everything else
- status icons: good `#0ca30c`, critical `#d03b3b`
- money: upfront `#dfe3ea`, contingent ("up to") `#5f6876`

One accent per card does the work. The subject is orange; everything that is
context is grey or blue. Never use the series colours for text values; values sit at
bar tips in the text colours.

## Marks
- Bars at most 24 px thick with 4 px rounded ends. Values at the bar tips.
- Reported milestones are filled diamonds; estimates are hollow diamonds or dashed
  bars and are labelled as your estimate.
- A "today" line on any timeline that crosses the present.
- Stat tiles: a big number (60 to 80 px), a one-line label, a muted qualifier.

## Anatomy of every card
1. Kicker row: series name and card number on the left ("CTLA-4 DEEP DIVE · 2/4"),
   the as-of date or the data source on the right, in small caps, letter-spaced.
2. Headline that states the finding, not the topic ("Merck is starting this race from
   the back", not "KRAS G12D landscape").
3. One-line subtitle that says what is plotted.
4. The panel: one chart, matrix, timeline or set of tiles.
5. Footer: caveats that change the reading (n, stage, cross-trial), sources, and
   "Not investment advice" (plus "or medical advice" when the card shows clinical
   data). Muted, 13 to 15 px.

## Card types the angles ask for
- headline_stat: hero headline, the one result as a two-bar comparison with the gap
  bracketed, three stat tiles, a one-sentence "why it matters" panel.
- pipeline_matrix / competitive_landscape: rows of asset and company with ticker,
  grouped by kind, a stage bar across Ph1 / Ph2 / Ph3 / Market, lead indication and
  the latest news with a win, setback or update icon.
- race_timeline / catalyst_timeline / regulatory_timeline / what_happened_timeline:
  programs or events as rows, years or quarters across, the today line, filled vs
  hollow markers.
- history_timeline: a centre spine, breakthroughs one side and setbacks the other.
- money / deal_ledger / deal_terms: bars split into upfront and contingent money;
  revenue columns; sizing bars against a reference line.
- scenarios / bull_bear_columns / bar_for_success: labelled scenario bars or two
  columns with the number that defines each case.
- results_table / side_by_side / cross_trial_context: a compact table of the numbers
  that decide the story, with the cross-trial caveat in the footer.
- mechanism_diagram: a simple inline-SVG schematic, three to five labelled parts, no
  decoration.
- market_growth / revenue_at_risk: two to three measures, latest vs projection, with
  the change and the source on each row.
- prior_data: the earlier data the readout will be judged against (the phase 2, the
  competitor, the standard of care) as rows of the one or two numbers that set the bar,
  each with its trial name and n.
- read_through_map: the failed or approved program in the centre, the exposed programs
  around it, each tagged with the shared reason (same target, same design, same
  population) and how exposed it is.
- label_vs_expectations: two columns, what the market expected and what the label or the
  letter says (population, line, boxed warning, post-marketing requirement), with the
  differences marked.
- replacement_pipeline: the candidates that could fill the hole as rows with stage, first
  possible launch year and a peak-sales range where one is published, against the year the
  exclusivity ends.
- where_it_plays: the indications or lines of therapy where the mechanism fits, sized by
  patients or market, with the reason it fits (or does not) on each row.
- the_overlooked_number: one big number the consensus misses, the number everyone quotes
  beside it for contrast, and one line on why the first one matters more.
- signposts: dated events that would move the debate, each marked as bull or bear
  evidence, on a short timeline or as a dated list.
- watchlist_schedule: the sessions that matter at the meeting, by day and time, with the
  presenter, the abstract or session number and the bar for each.
- bar_vs_result: the bar that was set beforehand (quoting the earlier piece or the
  guidance) beside what came out, row by row, with met, missed or mixed on each.

## Accuracy on the card
Every number on a card is in the fact base with its source. Estimates say so on the
card. A card is checked in the cold fact-check exactly like the post.
