# Motion finishing guide: `faultline-six-section-deck.pptx`

The deck ships with **no transitions and no animations**. Every slide is complete as a static slide, so it presents correctly even if you skip this guide. This guide adds the motion by hand in PowerPoint. It takes about five minutes.

Requires PowerPoint for Microsoft 365, or PowerPoint 2019 or later, on the Mac. Morph is not available in older versions; use Fade wherever this guide says Morph.

## Slide map

| # | Section | Role |
|---|---|---|
| 1 | 1 · Pipeline | Cover. Morph start state: six step dots in a row |
| 2 | 1 · Pipeline | The six-step pipeline |
| 3 | 2 · Architecture | Blackboard diagram |
| 4 | 2 · Architecture | Same diagram, with the Risk agent crashed (Morph end state) |
| 5 | 3 · Data quality | Four checks and a real experiment JSON excerpt |
| 6 | 4 · Report | Findings, with the scorecard bars at zero (Morph start state) |
| 7 | 4 · Report | Findings, with the scorecard bars at their real scores |
| 8 | 5 · Tools | Capability grid |
| 9 | 6 · Known issues | Four edges |

Slides 1, 3 and 6 exist only so that Morph has a "before" state. Without Morph, each of them still reads as an ordinary build step.

## Transitions: apply these, in order

A transition belongs to the slide you are moving **to**. Select that slide in the thumbnail pane, then use the **Transitions** tab.

| Slide | Transition | Effect Options | Duration | What it does, and why |
|---|---|---|---|---|
| 2 | **Morph** | Objects | 1.25 s | The six numbered dots on the cover move from a horizontal row into the left column of the pipeline, and each step's text fades in beside its number. The audience watches the one-line summary open up into the whole system. |
| 3 | Fade | Smoothly | 0.5 s | Section change. |
| 4 | **Morph** | Objects | 1.0 s | Everything stays still except the Risk agent: it turns pink, its arrow turns dashed, `experiments/<id>.json` gets a teal "intact on disk" outline, and the callout changes to the re-run command. The motion is the point of the slide: one agent fails and nothing else on the board moves. |
| 5 | Fade | Smoothly | 0.5 s | Section change. |
| 6 | Fade | Smoothly | 0.5 s | Section change. |
| 7 | **Morph** | Objects | 1.5 s | The four scorecard bars grow from zero to 49.7, 81.8, 93.3 and 99.0. The worst service is left visibly short. |
| 8 | Fade | Smoothly | 0.5 s | Section change. |
| 9 | Fade | Smoothly | 0.5 s | Section change. |

Leave slide 1 with no transition. Leave **On Mouse Click** checked and **After** unchecked on every slide, so the presenter controls the pace.

## Optional click-builds (only these two)

If you want the content to appear one item at a time, do it only on slides 5 and 9. Neither is part of a Morph pair, so grouping shapes there cannot break anything.

- **Slide 5, the four check cards.** Each card is four shapes: the panel, the number, the heading and the detail. For each card, Shift-click those four shapes, then press ⌘⌥G to group them. Select the four groups in order, then choose **Animations → Fade**, Start: **On Click**, Duration 0.5 s. Leave the JSON panel static, because it is the evidence and should stay on screen throughout.
- **Slide 9, the four edge cards.** Do the same: group each card's shapes, then add **Fade**, **On Click**.

Do not add entrance animations anywhere else. In particular, never add them on slides 1–4, 6 or 7: animations on top of Morph slides cause double movement.

## Rules that keep Morph working

- **Do not rename any shape whose name starts with `!!`.** To see them, open **Home → Arrange → Selection Pane**. The `!!` prefix forces PowerPoint to pair a shape on one slide with the same-named shape on the next: `!!step1`–`!!step6`, `!!agent0`–`!!agent6`, `!!bar0`–`!!bar3`, `!!callout`, and the rest. A renamed shape fades in and out instead of moving.
- **Do not group, ungroup or delete shapes on slides 1–4, 6 or 7.** A shape that is grouped on one slide and not on the next does not pair.
- If you edit a number on slide 7, make the same edit on slide 6. Only the bar widths should differ between the two slides.

## Rehearse

1. **Slide Show → From Beginning.** Click through all nine slides. Each Morph should look like one continuous motion, with no flash.
2. Open **Presenter View** and confirm the speaker notes appear on every slide. The notes on slides 5 and 7 say exactly which numbers are captured and which are illustrative. Read them before presenting.
3. Fallback: if Morph stutters on the demo machine, select slides 2, 4 and 7 and switch them to **Fade**. Nothing on the slides depends on the motion.

## Static-only version

To present with no motion at all, right-click slides 1 and 6 and choose **Hide Slide**. The deck then runs as seven slides: pipeline, architecture, crash, data quality, report, tools and known issues. Keep slides 3 and 4 as they are: they make two distinct points, the blackboard and crash isolation.
