# Design QA

final result: passed

## Visual target and evidence

- Source: selected first concept from the design review, `/data/wwj/.codex/generated_images/01a20cae-a0b0-7741-8f4c-cfc0ad597122/exec-e2d32ecc-10ce-4dd8-9d39-92a86abccd53.png`.
- Source pixels: 1374 × 1145, normalized to 1440 × 1200 for comparison.
- Implementation: Chromium-rendered `work/ui-final.png`, 1440 × 1200 CSS pixels, device scale factor 1.
- Side-by-side evidence: `work/design-comparison.png`; focused form/action comparison: `work/form-comparison.png`.
- State: solar theme, visible verdict, default descending score, all years. Production contains 25 matching images; source mock contains illustrative rows and a reference-site coverage count, replaced with actual data.
- Responsive evidence: `work/ui-mobile.png`, 390 × 844. Document width stayed within the viewport; result-table horizontal scrolling stays inside its container.

## Comparison history

1. Initial inspection found excessive desktop side margins and an initially cropped world view. Added explicit full container width and full-world fit bounds.
2. Full-world integer zoom left repeated-world gutters. Added fractional zoom snapping; the revised capture shows the entire world across the map panel.
3. Header and form spacing pushed results too far down. Consolidated source links with the introduction, reduced native Gradio spacing, and moved the scoring explanation below the primary content. Captured and compared the revised page.

## Required fidelity surfaces

- Typography: system sans-serif, ordinary bold page heading, readable 14–16 px form/body text; no display typeface or decorative branding.
- Spacing/layout: left controls and right map in 4:7 proportions, results below, purple primary action and gray reset. Additional sorting, pagination and real-image details implement the user's requested behavior.
- Colors: white/light-gray Gradio surfaces, purple selected controls/buttons, restrained Leaflet terrain styling. No dark theme or editorial cards.
- Images: actual Sentinel-2 RGB from pinned source row groups; no generated/placeholder satellite imagery. OSM outlines use the original WKB geometries and match the query's tags.
- Copy: real 248,719-image count, actual 2016–2024 years, original verdict categories and scores. Mock counts and mock IDs are absent.

## Interaction verification

Chromium/CDP exercised theme selection, query submission, result-row selection, next page, category statistics, sample discoveries and responsive layout. A real Leaflet marker click delivered its image ID through the iframe bridge. A selected source RGB loaded at 384 × 384. No application JavaScript exceptions were observed. Eight query/mapping/bootstrap regression tests and full-index validation passed; see `reports/validation.json`.

## Remaining notes

Real network imagery can load after the metadata; first-page previews are packaged, and other images expose retry actions. Result tables intentionally show real paginated records rather than reproducing the mock's three illustrative rows. No outstanding P0/P1/P2 findings.

## Deployment bootstrap verification

A checkout containing real ModelScope LFS pointers downloaded both public indexes, verified their exact sizes and SHA-256 hashes, and resolved them into the cache in 11.57 seconds. `.lfsconfig` excludes these two large files during build-time smudge; application startup resolves them without a management token. The ordinary GitHub checkout path is unchanged.

## Live deployment verification

The ModelScope Studio reached Running on the free 2-vCPU / 8-GB CPU tier. The browser-rendered production page is captured in `docs/screenshot.png`. Authenticated application API checks confirmed the complete 248719-image / 555542-review index, 25 solar-visible results in descending score order, matching full-map counts and a real 384 × 384 source preview. See `reports/online-validation.json`.

## 2026-11-25 mask and preview update

Browser verification at 1440 × 1100 confirmed ten previews, then complete preview-section hiding for a four-result query. Selecting the reported sample 2816821a4079c7348a1a renders both full-image and local-zoom panels, using 345 original mask pixels and an 80 × 80 source-pixel crop. Multiple-object selection defaults to the largest foreground mask. Tests cover exact pixel support and holes, edge-clamped crops, no-mask view, object switching and 0/1/9/10-result boundaries. Evidence: work/mask-pair-ui.png, work/check-mask-ui.log. No JavaScript exceptions observed.
