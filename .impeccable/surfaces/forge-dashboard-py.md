---
version: 1
slug: "forge-dashboard-py"
primary_target: "forge/dashboard.py"
related_targets: []
---

# Forge dashboard

Scope: every page served by forge/dashboard.py (Build an app, build progress, My apps, using an app, Review changes, Health). Mode: Operate.

Audience and job: hobbyist developers and non-technical owners, equally. Start a build, come back to see it pass, use the resulting apps, approve changes. Key moment: watching a build pass its checks.

Constraints: stdlib server-rendered HTML, inline CSS, no external fonts, scripts or images; light and dark; phone width. Avoid: hacker-terminal look, generic AI SaaS look, anything slow or heavy. Type uses the system UI stack (offline, no downloads); this is the Operate allowance, not a fallback.

## Direction contract

THESIS: Every build is a departure. Its checks are rows on a station board whose status word flips as it changes, until the app leaves as live. Refuses the category default of a grey sidebar with green ticks.

OWN-WORLD: Navy top rail (#0B2545) with a signal-yellow underline on the current page; cool grey ground from one tonal ramp shared by both themes; the board itself is a charcoal panel; status words set in flap tiles (one tile per letter, split by a hairline); tabular figures for times, numbers and durations; flat colour only, no gradients, glows or grain. One body size; hierarchy from weight, caps tracking and space.

STORY: The visitor sees what they asked for, where the build is, and what happens next; they can leave and return; the technical record is one click away.

FIRST VIEWPORT: Rail; build number and app name as the heading; the request quoted; the departure board of checks (time, check, status flaps, duration) leading the left column; examples and next step in a right column.

FORM: railway departure board, position 7 of 7 on the ranked list; seed:0fc4ff1f7d816c0d. Raises kept: numbers as lead identifiers; one body size; flat colour; one tonal ramp; fixed card furniture for apps.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

Unresolved: none.
