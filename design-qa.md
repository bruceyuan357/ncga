# NCGA Round 2 Design QA

## Evidence

- Source visual truth: `/Users/bruce/.codex/generated_images/019f8367-d036-7643-84a4-c9a265aacb3e/exec-af606adc-f201-4692-86ac-8fa5999259da.png`
- Implementation capture: `/private/tmp/ncga-round2-implementation-result.png`
- Full-view comparison: `.design-qa/ncga-round2-full-comparison.png`
- Workbench comparison: `.design-qa/ncga-round2-workbench-comparison.png`
- Desktop viewport: 1440 × 1024
- Tested state: populated Beijing-style rewrite, local fallback notice visible, original comparison open, four history versions present

## Mandatory fidelity surfaces

| Surface | Result | Notes |
| --- | --- | --- |
| Deep-jade lacquer frame | Pass | Persistent jade rail, command bar, and history tray recreate the reference frame and depth. |
| Seasonal silk-screen scene | Pass | A generated raster landscape is used as the actual page artwork; it remains atmospheric behind the workspace. |
| Open-book workspace | Pass | Cream paper, center seam, restrained rules, and left-to-right writing flow preserve the reference composition. |
| Original / rewrite / comparison hierarchy | Pass | Original and rewritten copy remain primary, with an always-available margin comparison and an interactive split view. |
| Version strip | Pass | The bottom tray is backed by real local history and supports restoring individual versions. |
| Controls and states | Pass | Rewrite, save draft, copy, audio, export, comparison, keyboard focus, warnings, and degraded mode all remain legible and functional. |
| Responsive behavior | Pass | 900 px and 390 px checks showed no horizontal overflow; the navigation becomes an off-canvas drawer and the workbench stacks. |
| Accessibility motion/material | Pass | Focus-visible styling, reduced-motion handling, reduced-transparency handling, and contrast overrides are retained. |

## Interaction verification

- Completed the rewrite flow through the rendered UI with `北京话（简体）` selected.
- Confirmed the rewritten result, degraded fallback state, and synchronized original-comparison copy.
- Created four distinct versions through repeated real rewrites and confirmed the version tray updated.
- Confirmed save-draft and comparison controls respond.
- Browser console: no errors or warnings.

## Findings

- P0: none.
- P1: none.
- P2: none.
- P3: the implementation intentionally keeps NCGA's richer production controls and fallback diagnostics, so the result column is denser than the concept image.
- P3: the generated landscape is more painterly and less graphic than the reference's exact silk-screen texture, but the palette, depth, and seasonal mood remain aligned.

## Comparison history

1. Initial implementation exposed an older high-specificity theme rule that compressed the new layout and hid the artwork.
2. Final cascade locks restored the 84 px rail, full landscape, command overlay, open-book proportions, comparison rail, and bottom version strip.
3. Populated-state comparison found no remaining P0, P1, or P2 mismatch.

final result: passed
