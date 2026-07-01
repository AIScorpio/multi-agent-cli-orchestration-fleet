# Fleet Kanban — Interactive Demo

A self-contained, **simulated** walkthrough of a multi-agent CLI-orchestration *fleet*
kanban board — one hub coordinating several projects, each worked by a pool of
heterogeneous AI CLI agents (codex / kimi / opencode / claude) under a single
orchestrating leader that owns QA.

**This is a front-end simulation for a sharing session — it is not connected to any
live system.** It auto-plays a ~2-minute loop:

1. A scripted tour of the **Overview** tab and three project tabs.
2. On the **05** tab, a fabricated wave of tasks is dispatched, **claimed in parallel**
   by worker instances (kimi×3, opencode×3, codex×1, claude×1), flows through
   **In&nbsp;Progress → Done·Pending&nbsp;QA → Approved ✓**, with per-agent staggered
   completion (no two same-agent tasks finish at once) — and the reserve-tier `codex`
   keeps grinding the hardest task as the loop restarts.

Open `index.html` (or the GitHub Pages link at the top of the repo's main README).
Controls: **⏸ Pause** / **⟲ Replay**.

The visual layout, colour system, and rendering logic are a faithful port of the
real read-only kanban hub. All project names, task titles, phase descriptions, and
telemetry in this demo are **synthetic** — fabricated to match the shape of a real
snapshot for illustration only, with no connection to any live system or real project.
