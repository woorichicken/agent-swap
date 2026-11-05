# Backlog

Ideas parked for later. Each item says what is true today and what would trigger the work.

| # | Idea | Today | Trigger |
|---|---|---|---|
| 1 | Notify when Codex 5h / weekly usage crosses a threshold (e.g. 80%) | Menu shows the number only (`weekly 35%`); Claude has auto-switch, Codex has nothing | Running out of Codex weekly quota without noticing |
| 2 | Show reading age on agent rows (`· 3m ago`) and mark stale values | A failed read keeps the previous lines silently | A stale number is mistaken for a live one |
| 3 | Menu bar title source: Claude / Codex / highest % | Title shows Claude usage only | Codex becomes the main daily agent |
| 4 | Muse: 7-day estimated cost next to today | Today's estimate only | Weekly cost review |
| 5 | Codex multi-account save & switch (`~/.codex/auth.json` per account) | One Codex login; "Sign in" replaces it | Needs a decision: refresh-token rotation may log the saved copy out |

Measured 2026-11-05: one Codex usage read takes ~1.0 s (app-server start), Muse local read ~0 s — refresh cost is not a concern.
