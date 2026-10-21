# Fable: Faultline front end, design brief

Built from scratch, independent of `site/` and of the vanilla control room. Served at `/fable/` by
`faultline dashboard`; dev server `npm run dev` on 5181 proxies `/api` to 127.0.0.1:8777.

## Read

A crash-test lab for cloud software, for hackathon judges with six minutes and engineers after them.
Two acts on one page:

1. **The lab** (light): what Faultline is, in four screens. Hero with a live test rig, the eight-step
   sequence, the rule ("the model proposes, code decides"), the safety barrier, the hand-off.
2. **The camera** (dark, `data-theme="camera"`): the workflow. A sticky rail of eight stations; one
   station on screen at a time; each station is one agent's input, action and output. The narrator
   feed is a drawer at the bottom. Stop everything is always in the header.

Why crash testing: Bosch tests cars by crashing them on purpose, instrumented, repeatable, behind a
barrier. Faultline does that to a running system. The vernacular gives us the signature element.

## The one bold thing: the test marker

The quartered yellow and black crash-test marker (`components/Marker.tsx`). Every service on every
map is a marker sized by criticality. Its ring carries state: yellow ring = the test target, red =
affected or down, green = recovered, dashed violet = predicted. Spend boldness here and in the
hero rig; everything else is quiet, typographic and exact.

## Tokens (`src/styles/index.css`, never hardcode a hex in a component)

| Token | Lab | Camera | Use |
|---|---|---|---|
| `bg` | `#EDEDE8` concrete | `#0E0E0E` | page |
| `sheet` | `#F6F6F3` | `#171717` | instrument surfaces |
| `fg` | `#121212` carbon | `#EDEDE8` | text, primary buttons |
| `fg-2` | `#4B4F55` | `#9A9C98` | secondary text |
| `line` | `#C9CAC4` | `#2C2C2A` | 1px rules |
| `grid` | `#DCDDD7` | `#1E1E1D` | mm grid inside instruments only |
| `marker` | `#FFD60A` | same | THE accent: test target, the launch, the p95 trace. Never decoration |
| `pass` / `fail` | green / red | brighter | status inks, always with a glyph or word |
| `model` | `#5A5AD8` | `#9A9AF2` | anything the LLM wrote or anything predicted, not measured |

Rules: no gradients, no glows, no shadows. Elevation = `sheet` + 1px `line`. Radius 2px everywhere.
Yellow only on the marker, the launch control, the current-station underline and the p95 trace.

## Type

- Archivo Variable, width axis used as the voice: `display-wide` (wdth 118) for the hero,
  `display` (wdth 108) for section and station headlines, `label-condensed` (wdth 78) for labels.
- Azeret Mono, only for instrument readings: numbers, units, timecodes, service names on instruments,
  evidence quoted from config. Always `tabular`.
- Sentence case everywhere. No all-caps labels, no tracked eyebrows, no accenting one word of a
  headline. Body at 15 to 17 px, `max-w-[60ch]`.

## Layout

- Left aligned. Container `max-w-[1400px] px-4 md:px-8`.
- Station: `components/Station.tsx`. Left column (320 to 400 px): number, agent names, status word,
  headline, lede, a two-row ledger "Code decides / The model may", the controls, prev/next. Right:
  the instrument(s) in `<Instrument>` frames. Optional full-width `below`.
- No cards inside cards. An instrument may contain rows separated by 1px rules, never nested boxes.
- Below 1024 px the station stacks; below 768 px everything is one column; no horizontal scroll at
  375 px; touch targets 44 px.

## Motion (`motion/react` only)

Motion explains a cause and its effect, nothing else.
- Lab: one orchestrated hero moment (the rig draws itself, then waits for a click). Clicking a
  service runs a simulated impact: marker hit, blast travels along edges, readout counts up.
- Camera: station swap is a 220 ms fade. Rail underline slides (layoutId). Map edges draw in once.
  Live data moves because the system moved.
- Never animate width/height/top/left except the hold-to-launch fill. Respect reduced motion
  (`MotionConfig reducedMotion="user"` is set globally; check `useReducedMotion` for custom loops).
- No scroll listeners on window. `useScroll`/`useInView` only.

## Copy

- Plain, specific, active. Buttons say what happens ("Map the system", "Capture baseline").
  The same action keeps the same name through running and done states.
- ZERO em or en dashes in UI text. Backend strings pass through `clean()`.
- No filler (elevate, seamless, unleash, supercharge, next-gen).
- No invented numbers. Every number comes from `/api/state`, `/api/catalog`, or the replay files.
- Predicted weaknesses are always labelled "predicted, unconfirmed". LLM text carries `<AiMark>`.
- Errors say what happened and how to fix it. Empty states invite the one action that fills them.

## Data

`src/lib/store.ts`: one poller (1 s idle, 400 ms while running, paused when the tab is hidden),
selector hooks (`useFState`, `useEvents`, `useCatalog`, `useConnection`), `actions.*` for every
endpoint. Falls back to `public/replay/*.json` after two failed polls; every action except stop is
refused in replay with an explanation. Types: `src/lib/types.ts`. Stage model and agent ledger:
`src/lib/stages.ts`. Cross-station hand-offs: `useWorkflow().go(stage, intent)`.
