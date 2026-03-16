# AI Fluency Profiler

Analyze your AI coding sessions against git history. Two layers of analysis:

1. **Session analytics** — how you interact with the AI (shapes, chains, thrashing, behavioral signals)
2. **Code durability** — what happened to the code after the session ended (survival, bugs, churn)

Works with **Claude Code** and **Pi** session transcripts. Zero external dependencies — just Python and git.

## Install

```bash
pip install git+https://github.com/YOUR_ORG/ai-fluency-profiler.git
```

## Quick Start

```bash
# Fast scan — session analytics only (~2 seconds)
fluency scan --no-git

# Full scan — session analytics + code durability (~2-7 min for large repos)
fluency scan

# Generate a shareable markdown report
fluency report --anonymize --output report.md

# Single session deep dive
fluency retro path/to/session.jsonl --repo path/to/repo
```

If your repos aren't in `~/Code/`, point to them:

```bash
fluency scan --code-root ~/projects
```

## What It Analyzes

### Session Analytics (no git required)

Parsed from your local session transcripts — no API calls, everything runs locally.

- **Session shapes** — classifies each session by what happened: `explore_build` (100% ship rate), `debug_investigate` (0% ship rate), `plan_handoff`, `review_iterate`, etc.
- **Chain detection** — groups related sessions by time proximity and intent overlap. Flags **thrashing** (repeated sessions on the same files with zero output).
- **Behavioral signals** — shipped vs zero-commit sessions compared: prompts, tool calls, test usage, subagent usage, active time.
- **Timing breakdown** — AI working time vs human waiting time vs human typing time.

### Code Durability (requires git)

Connects session commits to `git blame` at HEAD to measure what survived.

- **Line survival** — how many lines from each session still exist in the codebase.
- **Loss categorization** — lines lost to bugs vs architecture changes vs feature evolution vs refactoring. These are very different and should be evaluated differently.
- **Bug rate** — lines removed by `fix:` commits as a percentage of lines added.
- **Adjusted survival** — raw survival corrected for intentional architecture changes (file deletions, rewrites).
- **Per-project breakdown** — durability stats by repo.

## How It Finds Your Data

**Sessions** are auto-discovered from standard locations:
- Claude Code: `~/.claude/projects/*/`
- Pi: `~/.pi/agent/sessions/*/`

**Git repos** are matched by extracting the project name from the session directory name and looking for it in `~/Code/`. Override with `--code-root`:

```bash
fluency scan --code-root ~/dev        # repos in ~/dev/
fluency scan --code-root ~/projects   # repos in ~/projects/
```

If a repo can't be found, session analytics still run — you just won't get durability data for that project.

## Sample Output

### Scan

```
══════════════════════════════════════════════════════════════════════
  my-project — 12 CC + 38 Pi
  50 sessions (18 ship · 14 unknown · 8 investigate · 6 review · 4 explore)
══════════════════════════════════════════════════════════════════════

DURABILITY (32 sessions with git data)
  48,210 lines added → 38,568 surviving (80% raw, 89% adjusted)
  Losses: 312 bug-fix (8 commits) · 5,102 architecture · 2,840 evolution
  Bug rate: 0.6% of lines, 8 fix commits
  Note: Durability favors well-scoped tasks (bugfixes). Compare within goal categories.

SESSION SHAPES:
  explore_build           22 (44%)  goal: ship 18, unknown 4  22 with commits
  debug_investigate       15 (30%)  goal: investigate 8, ship 4, unknown 3
  review_only              8 (16%)  goal: review 6, unknown 2
  explore_only             5 (10%)  goal: explore 3, learn 2

CHAINS (6 multi-session)
  explore_converge         3 chains  3 with commits
  plan_execute             2 chains  2 with commits
  investigation            1 chains  3 sessions (0 commits expected)

BY GOAL (median values):
                                   ship (n=18)   investigate (n=8)   review (n=6)
  active min                              62                  14             20
  prompts                                 19                   5              4
  tool calls                             280                  48             35
  commits                                 11                   0              0
  has tests                              72%                  4%             0%

NOTE: Sessions evaluated against inferred goal. A review with 0 commits is
working as intended. Intent inferred from opening prompt — may be imperfect.
Durability favors well-scoped tasks over ambiguous ones.
```

### Retro

```
======================================================================
  SESSION: abc123
  Source: pi  |  Shape: debug_investigate  |  Goal: investigate
======================================================================

SUMMARY: 45-minute investigation session. You sent 6 prompts, the agent
made 48 tool calls. No code was committed — typical for investigate sessions.

INTENT: why is the test failing on CI but passing locally?
```

## Commands

| Command | What it does | Speed |
|---------|-------------|-------|
| `fluency scan --no-git` | Session analytics only | ~2 seconds |
| `fluency scan` | Session analytics + code durability | ~2-7 min |
| `fluency report` | Full markdown report to stdout | ~2-7 min |
| `fluency report --anonymize -o report.md` | Anonymized report to file | ~2-7 min |
| `fluency retro SESSION --repo REPO` | Deep dive on one session | ~10 sec |

## Requirements

- Python ≥ 3.11
- Git (for code durability analysis)
- Claude Code and/or Pi session history on local disk

## Also works as a module

```bash
python -m fluency scan --no-git
```
