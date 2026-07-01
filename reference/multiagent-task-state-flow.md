# Multi-Agent CLI Orchestration — Task 状态流转

> `multi-agent-cli-orchestration` skill 中，task 的"状态"就是它所在的**队列目录**——目录本身是状态机，迁移靠**原子 `mv`（`rename(2)`）**。没有中央调度器、没有 server，整套系统因此是 crash-only：任何时刻把四个目录数一遍就是完整真相。

![Task 状态流转图](multiagent-task-state-flow.png)

---

## 状态流转图（Mermaid）

```mermaid
flowchart TD
    START([orchestrator.py create-task]):::action
    PENDING[("queue/pending/<br/>task-xxxx.json")]:::pending
    CLAIMED[("queue/claimed/<br/>agent--task.json<br/>= In Progress")]:::claimed
    COMPLETED[("queue/completed/<br/>+ .result.json<br/>= Done · QA")]:::completed
    FAILED[("queue/failed/<br/>+ .result.json<br/>= Failed")]:::failed
    QAGATE{{"Claude-lead QA 闸门<br/>read-result → 逐条核对<br/>acceptance_criteria（真去跑）"}}:::gate
    QAPASS[("completed/qa-passed/<br/>= Approved ✓（累计列）")]:::approved
    COMMIT([Claude-lead：审 diff<br/>→ commit / merge]):::action

    START -->|atomic write| PENDING
    PENDING -->|"watch.sh claim_one()<br/>原子 mv · exactly-once"| CLAIMED
    CLAIMED -->|"rc==0 且产物非空<br/>（artifact-first 判定）"| COMPLETED
    CLAIMED -->|"log 命中 auth 失败<br/>pause-not-fail · 回队 + backoff"| PENDING
    CLAIMED -->|"其它失败 / auth 重试超限"| FAILED
    CLAIMED -.->|"watcher 重启：只回收<br/>自己名字前缀的陈旧 claim"| PENDING
    COMPLETED --> QAGATE
    FAILED -.->|"qa-fail 亦可对 failed 重试"| QAGATE
    QAGATE -->|"全部达标 · qa-pass"| QAPASS
    QAGATE -->|"不达标 · qa-fail --reason<br/>clone 重试任务 + 归档旧记录"| PENDING
    QAPASS --> COMMIT

    classDef action   fill:#eef2ff,stroke:#6366f1,stroke-width:1px,color:#1e1b4b;
    classDef pending  fill:#f3f4f6,stroke:#9ca3af,stroke-width:1px,color:#111827;
    classDef claimed  fill:#dbeafe,stroke:#3b82f6,stroke-width:1px,color:#0c2c63;
    classDef completed fill:#dcfce7,stroke:#22c55e,stroke-width:1px,color:#14532d;
    classDef failed   fill:#fee2e2,stroke:#ef4444,stroke-width:1px,color:#7f1d1d;
    classDef gate     fill:#fef9c3,stroke:#eab308,stroke-width:1.5px,color:#713f12;
    classDef approved fill:#bbf7d0,stroke:#16a34a,stroke-width:2px,color:#14532d;
```

---

## 状态流转图（ASCII，终端友好）

```
                          orchestrator.py create-task
                                    │  (atomic write)
                                    ▼
                          ┌───────────────────┐
                          │  queue/pending/    │   task-xxxx.json
                          └─────────┬─────────┘
                                    │
                   watch.sh claim_one()  ← 原子 mv (rename 系统调用)
                   多个 worker/实例竞争，只有一个赢
                                    │  pending/x.json → claimed/<agent>--x.json
                                    ▼
                          ┌───────────────────┐
                          │  queue/claimed/    │   codex--task-xxxx.json   (= In Progress)
                          └─────────┬─────────┘
                                    │
                  worker 跑 native CLI，写 output_file
                  process() 判定（artifact-first）:
                                    │
            ┌───────────────────────┼───────────────────────┐
            │ rc==0 且产物非空        │ 命中 AUTH_FAIL_RE       │ 其它失败
            ▼                       ▼                        ▼
   ┌─────────────────┐   ┌──────────────────────┐   ┌─────────────────┐
   │ queue/completed/ │   │  requeue_auth()       │   │  queue/failed/   │
   │  + .result.json  │   │  pause-not-fail:      │   │  + .result.json  │
   │  (= Done · QA)   │   │  回 pending、计数+1、    │   │  (= Failed)      │
   └────────┬────────┘   │  backoff 等 token 刷新   │   └────────┬────────┘
            │            │   ──► 回到 pending ──┐   │            │
            │            └──────────────────────┘   │            │
            │                  (重试超限才 → failed)              │
            │                                                    │
   ╔════════╧═══════════════ Claude-lead 的 QA 闸门 ═════════════╧═══════╗
   ║  orchestrator.py read-result <id>  → 逐条核对 acceptance_criteria    ║
   ║  （可执行的真去跑：跑测试、跑 import，不靠肉眼）                          ║
   ╚════════╦═══════════════════════════════════╦═══════════════════════╝
            │ 全部达标                            │ 不达标
            ▼                                    ▼
   ┌──────────────────────────┐    orchestrator.py qa-fail <id> --reason "..."
   │ completed/qa-passed/      │              │
   │  spec + result 都迁入      │    ① clone 成新 pending 任务（带 retry_reason，派回同一 agent）
   │  (= Approved ✓ 累计列)     │    ② 旧 spec + result 归档到 <state>/archive/
   │  qa-pass <id>             │              │
   └──────────────────────────┘              └──► 回到 pending（闭环，直到达标）
            │
            ▼
   Claude-lead 审 diff → commit / merge（唯一掌控版本控制的角色）
```

---

## 迁移语义

| 迁移 | 触发 | 机制 |
|---|---|---|
| `(无)` → `pending` | `create-task` | `_atomic_write`（先写 `.tmp` 再 `rename`）|
| `pending` → `claimed` | worker 抢占 | **原子 `mv`**，并发下 exactly-once；文件名加 `<agent>--` 前缀 |
| `claimed` → `completed` | rc==0 且产物非空 | **artifact-first**：先看产物存在，再看 log，避免把含 "401" 的合法交付误判 |
| `claimed` → `pending`（自愈）| log 命中 auth 失败 | **pause-not-fail**：回队 + 计数 + backoff，token 刷新后自动续 |
| `claimed` → `failed` | 其它失败，或 auth 重试超限 | 写 `.result.json` sidecar |
| `completed` → `completed/qa-passed` | `qa-pass` | spec + result 一起迁入，进入累计 Approved 列 |
| `completed`/`failed` → `pending`（重试）| `qa-fail --reason` | clone 新任务带 reason + 归档旧记录到 `archive/` |
| `claimed` → `pending`（崩溃恢复）| watcher 重启 | 只重排**自己名字前缀**的陈旧 claim，不碰别人的 |

---

## 三个设计要点

1. **没有中央调度器**：状态 = 目录，迁移 = `mv`。整个生命周期由文件系统的原子 `rename(2)` + 几个 shell/python 脚本驱动，所以系统 crash-only——四个目录数一遍即真相。

2. **两条"回到 pending"的边来源不同**：**auth 自愈**（机器层瞬时故障，worker 自处理）与 **QA 重试**（质量层不达标，leader 处理）被刻意分到两个角色 / 两套机制——worker 负责"能不能跑完"，leader 负责"跑得对不对"。

3. **`qa-pass` 迁到独立 `qa-passed/` 而非原地打标**：让看板的 **Approved 累计列**与 **live 队列**物理分离——完成的活留在视野里体现进度，而不是 `qa-pass` 后消失，让长项目显得"空"。

---

*Source: `~/.claude/skills/multi-agent-cli-orchestration/` — `watch.sh`（claim/process/requeue_auth）、`orchestrator.py`（create-task/qa-pass/qa-fail）、`schema.py`（TaskSpec/TaskResult）。*
