# RTSP Studio — UX & quality plan (2026-11-20)

Audit of the live dashboard (current build, live data, read-only), every page at 1440 px, 30 s idle.

| Page | React re-renders /min | Requests /min | Data /min | Layout shift (CLS) | Contrast issues |
|---|---|---|---|---|---|
| Overview | 114 | 116 | 2.2 MB | **0.37 (poor)** | 8 |
| Streams | 122 | **220** | 3.0 MB | 0.00 | 11 |
| Stream details | 138 | 176 | 2.5 MB | 0.00 | 8 |
| Library | 112 | 116 | 2.2 MB | 0.01 | 29 |
| Analytics | 134 | 146 | 3.4 MB | 0.00 | 5 |
| Health | 110 | 116 | 2.2 MB | **0.33 (poor)** | 18 |
| Events | 114 | 136 | 3.6 MB | 0.00 | **303** |

Causes found:
1. `/api/streams` (52 KB) is polled every 2 s on every page and changes on every poll (live frame/byte counters),
   so the "skip if unchanged" guard never fires: the whole app re-renders ~2×/s in every open tab, even idle.
   `/api/system` the same. Plus `/api/mediamtx/status` every 2 s and the analytics tree every 3 s.
2. Streams page: each card's thumbnail is a fresh RTSP frame grab (~56/min per tab): server CPU + a brief "reader".
3. Overview tiles and the Health report arrive after the page is drawn and push content down (CLS 0.37 / 0.33).
4. Events: 300 events re-downloaded every 3 s (71 KB), 1,456 DOM nodes.
5. Muted grey text below WCAG contrast on every page; console noise (404 for `/api/naming` until R2, router warnings).
6. Same numbers shown 3 times (top bar, Overview stat cards, Streams stat cards); mixed verbs for the same action
   ("New stream", "Stream", "Run RTSP"); "Shared video library" + "URLs use" selector are unclear.

## Progress (2026-11-20, all live without restart)
- DONE 1.2 tracked store (components redraw only for what they read; counters only redraw the fps chip) — Streams page DOM changes idle 942 → 58/min
- DONE 1.3 (frontend part) polls slowed: system 3 s, MediaMTX 5 s, tree 5 s — requests idle 116–220 → 80–92/min
- DONE 1.4 stored thumbnails on stream cards (0 frame grabs)
- DONE 1.5 (frontend part) Events: 100 newest, "Show older", poll 5 s
- DONE 1.6 CLS: Overview 0.37 → 0.002, Health 0.33 → 0.002
- DONE 1.7 lazy pages: first JS 379 → 308 KB
- DONE 1.8 router warnings gone (404 for /api/naming disappears with R2)
- DONE Phase 2 navigation: Overview · Streams · Library · System (Health | Events | Settings); /health and /events redirect;
  Streams stat cards → one summary line; Overview numbers open filtered Streams; "New stream" everywhere
- DONE (Phase 3) error boundaries: a crashing page/card shows a small Retry box instead of a blank app
- DONE (Phase 4) contrast: axe colour-contrast + heading-order = 0 on every page, light and dark
- DONE (2026-11-20 afternoon, live): Analytics page Videos | Streams tabs; stream page status strip; Library "Filters" button
  with removable chips + "Same video as …" flag; connection-lost/failing banner with since-time; live preview falls back to
  still frames when the browser can't play it; Library list view by default on phones; Settings tab no longer jumps
- DONE scripts/ux_audit.py quality gate (all pages, light/dark, desktop/phone): PASS
- DONE duplicates: 10 byte-identical copies removed (trash 7 days, log ~/rig02-backups/dedupe/); uploads checked BY CONTENT
  in the browser (live now) and refused by the server (R2)
- R2 backend (built + tested on preview): server refuses duplicate content; app's own RTSP reads tagged and not counted as
  viewers; automatic full health test once a day; quick_hash in the video list
- DROPPED lite stream-list endpoint: responses are already gzip (stream list 53 KB raw = 6 KB on the wire)

## Phase 1 — Smooth and light (no visible change; biggest effect)
| # | Change | Target | Deploy |
|---|---|---|---|
| 1.1 | Split the stream list from live counters: list (names, status, config) only when it changes (ETag/304); counters in a small `/api/streams/live` | re-renders idle: ~120/min → < 10/min | backend (R2/R3) + frontend |
| 1.2 | Per-stream subscriptions in the store (`useSyncExternalStore`): an fps change re-renders only that chip | no whole-page re-render | frontend |
| 1.3 | One combined status poll (system + mediamtx + capacity) every 5 s; tree/videos only on change | requests idle: 116–220/min → ~20/min; data 2–3.6 MB/min → < 0.3 MB/min | backend + frontend |
| 1.4 | Card thumbnails = the video's library thumbnail (static, cached); live frame only on the stream page / hover | 0 frame grabs on the Streams page | frontend |
| 1.5 | Events: 50 per page, poll only "newer than last id", "Load more" | 71 KB/3 s → < 1 KB | backend + frontend |
| 1.6 | Reserve final sizes for everything that loads late (Overview tiles, Health report) | CLS < 0.05 everywhere | frontend |
| 1.7 | Route-split the bundle (Library, Analytics, Overview, dialogs lazy) | first load JS 379 KB → < 200 KB | frontend |
| 1.8 | Clean console: no 404 before R2, router flags set, no React warnings | 0 console messages | frontend |

## Phase 2 — Organise (information architecture)
- **Navigation**: Overview · Streams · Library · System (Health + Events + capacity/encoding/codecs settings) · Users.
  "Shared video library" → "Library". The "URLs use" box moves into the copy menus / a top-bar setting.
- **One summary, once**: the top bar keeps one health pill (click → System). Overview keeps the 4 stat cards, made
  clickable as filters; the Streams page drops its duplicate stat cards (filter tabs already show the counts).
- **One verb per action**: "New stream" (everywhere: library, analytics, top bar), "Copy URLs", "Export for edge",
  "Make camera copy", "Start / Stop / Restart". One icon each.
- **Analytics page = workspace**: header (name, code, description, live counts) + tabs **Videos | Streams**;
  header actions in one ⋯ menu; upload/drop zone shown only when empty or dragging.
- **Stream page**: status strip (live, fps, readers, uptime, problems) on top; preview + RTSP check side by side;
  Logs / Readers / History tabs; Advanced folded (done).
- **Library**: filters in one "Filters" button with removable chips; duplicates grouped ("same file as …");
  after R2 the name chips; list view default on phones (18,000 px page today).

## Phase 3 — Reliability (no silent failures)
- Connection banner: if polls fail, one banner "Connection lost — retrying" and data marked stale (today it silently freezes).
- Every action: button shows progress, then success/failure; start/stop optimistic with rollback; the ⋯ menus close on scroll.
- Readers: never count the app's own loopback readers (preview copy, snapshots) as viewers.
- Live preview: if the browser can't play it, fall back to snapshots every 2 s instead of spinning forever.
- Health: full test also runs automatically once a day; report age shown; failures pinned to the top bar.
- Logins on (decision): names on every change in Events (24 anonymous deletions on 11-19).

## Phase 4 — Polish and accessibility
- Colour tokens: raise muted/subtle text contrast to ≥ 4.5:1 (axe: 5–303 issues per page → 0); same in dark mode.
- Heading order, visible focus rings, every icon button labelled, Escape/Enter consistent in dialogs and menus.
- Typography and spacing scale: 3 text sizes, 2 card densities; consistent chip styles (status / problem / info).
- Empty, loading and error states designed for every list.
- Phones: sticky bottom action bar, no horizontal scroll, touch targets ≥ 40 px.

## Phase 5 — Keep it that way
- `scripts/ux_audit.py` (this audit) in CI with budgets: re-renders/min, requests/min, CLS, console errors, axe = 0.
- Playwright visual snapshots of each page (light/dark, desktop/phone) to catch layout regressions.
- Short design-tokens page in `docs/`.

## Order and effort
1. Phase 1 (frontend parts hot-deployable; backend parts ride with R2/R3) — 1–2 days.
2. Phase 2 — 2 days, hot-deployable.
3. Phase 3 — 1 day (+ logins decision).
4. Phase 4 — 1 day.
5. Phase 5 — half a day.
