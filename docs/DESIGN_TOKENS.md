# RTSP Studio — design tokens and UI rules

Colours are CSS variables in `frontend/src/index.css` (light under `:root`, dark under `.dark`), exposed to
Tailwind in `frontend/tailwind.config.js`. Use the token classes, never raw colours, so light and dark both work.

| Token | Use | Contrast (min) |
|---|---|---|
| `bg` | page background | — |
| `surface`, `surface-2` | cards, inputs; subtle panels | — |
| `line` | borders, dividers | — |
| `fg` | main text | ≥ 7:1 |
| `muted` | secondary text | ≥ 6.9:1 |
| `subtle` | hints, timestamps, placeholders | ≥ 4.6:1 (WCAG AA) |
| `brand` / `brand-strong` / `brand-soft` | links and selected items / primary buttons / chips behind brand text | text ≥ 5.4:1 on soft, white on strong ≥ 5:1 |

Status colours: Tailwind emerald (ok), amber (warning), red (problem), sky/blue (info); text shades
`-700` in light mode and `-300/-400` in dark mode (`text-emerald-700 dark:text-emerald-400`).

## Rules the dashboard follows
- **Show problems, hide normal states.** No "Edge-ready"-style badges; one amber chip when something needs a look.
- **One main action per item**, everything else in the `⋯` menu (`MoreMenu` in `components/common.tsx`).
- **No buttons that can't act** (no "Start 0").
- **One name per action**: New stream · Copy URLs · Export for edge · Make a copy · Start / Stop / Restart.
- **Reserve space** for anything that loads late (skeletons of the final height): layout shift < 0.05.
- **Redraw only what changed**: read shared data with `useRig()` (tracked per field); live counters only in small
  components (`useLiveStream`); polled objects keep their identity while only counters move.
- **Never silent**: failing polls show the connection banner; a crashing part shows `ErrorBoundary`'s Retry box.
- **Touch**: buttons ≥ 40 px high on touch screens (`@media (pointer: coarse)`), no horizontal scroll at 390 px.
- **Check**: `scripts/ux_audit.py` (budgets: redraws, requests, CLS, console errors, axe) must PASS before deploying.
