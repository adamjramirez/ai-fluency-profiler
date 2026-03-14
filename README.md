# AI Fluency Profiler

Analyze your AI coding sessions against git history. See what code survived, what thrashed, and what behavioral patterns predict shipping.

Works with **Claude Code** and **Pi** session transcripts. Zero external dependencies — just Python and git.

## Install

```bash
pip install git+https://github.com/YOUR_ORG/ai-fluency-profiler.git
```

## Quick Start

```bash
# Fast scan — session shapes, chains, behavioral signals (~2 seconds)
fluency scan --no-git

# Full scan — adds code durability from git blame (~2-7 min for large repos)
fluency scan

# Generate a shareable markdown report
fluency report --anonymize --output report.md

# Single session deep dive
fluency retro path/to/session.jsonl --repo path/to/repo
```

## Sample Output

```
══════════════════════════════════════════════════════════════════════
  my-project — 12 CC + 38 Pi
  50 sessions (22 explore_build · 15 debug_investigate · 8 explore_only · 5 review_iterate)
══════════════════════════════════════════════════════════════════════

DURABILITY (32 sessions with git data)
  48,210 lines added → 38,568 surviving (80% raw, 89% adjusted)
  Losses: 312 bug-fix (8 commits) · 5,102 architecture · 2,840 evolution
  Bug rate: 0.6% of lines, 8 fix commits

SESSION SHAPES:
  explore_build           22 (44%)  22 with commits   avg 11.2 commits
  debug_investigate       15 (30%)
  explore_only             8 (16%)
  review_iterate           5 (10%)  3 with commits    avg 6.5 commits

CHAINS (6 multi-session)
  explore_converge        3 chains → 3 shipped
  plan_execute            2 chains → 2 shipped
  thrashing               1 chains → 0 shipped  ⚠️ 3 sessions, 0 output

BEHAVIORAL SIGNALS (shipped vs zero-commit)
                              Shipped (n=25)     Zero (n=25)
  active min                      62               14
  prompts                         19                5
  tool calls                     280               48
  has tests                      72%               4%
  has subagents                  28%              12%
```

## What It Reads

- **Claude Code sessions**: `~/.claude/projects/*/`
- **Pi sessions**: `~/.pi/agent/sessions/*/`
- **Git repos**: auto-detected from session directory names, looks in `~/Code/` by default

## What It Produces

- **Code durability** — lines added → lines surviving at HEAD, categorized by why lines were lost (bugs, architecture changes, evolution, refactoring)
- **Session shapes** — classifies each session (explore_build, debug_investigate, plan_handoff, etc.) with ship rates
- **Chain detection** — groups related sessions by time and intent, flags thrashing (repeated work with zero output)
- **Behavioral signals** — shipped vs zero-commit sessions compared on prompts, tool calls, test usage, active time
- **Markdown reports** — `fluency report` generates a full shareable analysis

## Requirements

- Python ≥ 3.11
- Git
- Claude Code and/or Pi session history on local disk

## Also works as a module

```bash
python -m fluency scan --no-git
```
