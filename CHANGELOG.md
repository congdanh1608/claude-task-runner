# Changelog

## 2.36.0
- Per-row effort (Codex by row, Gemini in model name) and new aliases: hydrogen-low/high, carbon-high, neon, neon-medium; rows deep, critical, escalate; Carbon high writes specs and plans, Neon medium reviews them, Neon high takes Carbon FAILs; Sol out of code row; release.sh creates the GitHub release from the changelog

## 2.35.6
- task quota shows only workers with a real quota source; no log scan, no opencode stats (7 s instead of 28 s)

## 2.35.5
- task quota overlaps the OpenCode log scan, opencode stats and free-model scan

## 2.35.4
- task quota checks its sources in parallel

## 2.35.3
- Docs: prod SSH allowed on any owner-paid worker; third-party gateway and free workers excluded

## 2.35.2
- Docs: PATH setup for macOS, Linux and Windows; model assignment guide (docs/models.md)

## 2.35.1
- Names scan uses depth-1 folders, globs and an allow list; add the commands guide

## 2.35.0
- Initial public release.
