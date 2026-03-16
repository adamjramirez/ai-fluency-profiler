# Plan: Intent-Aware Reframe (v2 — post-review)

**Date:** 2026-03-12
**Source:** Maru's feedback + Adam's observations
**Goal:** Remove the implicit "commits = good" bias, evaluate sessions against their own intent, and amplify workflow visibility as the core product.

---

## Problem Summary

1. **Intent blindness** — `review_only` gets 0% ship rate when the user never intended to ship.
2. **Bugfix overfitting** — Bugfix sessions score high on every metric. Analysis says "be more like bugfix."
3. **Quantitative-only lens** — Line survival is a proxy, not a measure of value.
4. **Gaming dynamic** — Metrics create an implicit reward function favoring high-autonomy, low-prompt sessions.
5. **No qualitative layer** — Named but not solved (no LLM dependency).

## Design Decisions (from review)

- **`session_goal` replaces `session_shape` as the primary lens.** Shape stays as secondary detail but goal drives evaluation, reporting, and chain classification.
- **Classifier validation before integration.** Manual label 25 sessions, compare, proceed only at ≥60% accuracy.
- **Session narrative** added to retro output — one human-readable paragraph per session. This is the "workflow visibility" product.
- **Report changes consolidated** — Tasks 3/4/5/7 become one report rewrite.
- **Retro command included** in scope.
- **README updated** with new sample output.

---

## Task 1: Add `session_goal` to model

**File:** `src/fluency/models.py` (modify)

Add field with default so existing code doesn't break.

**Verify:** existing tests pass.

---

## Task 2: Goal classifier

**File:** `src/fluency/parser.py` (modify)

Add `classify_session_goal(first_intent: str) -> str`. Keyword-based, conservative, defaults to "unknown".

| Goal | Signals |
|------|---------|
| `ship` | implement, build, add, create, fix, wire up, set up |
| `investigate` | why, what's happening, look into, debug, figure out, diagnose |
| `review` | review, check, audit, PR, code review |
| `explore` | what if, how would, try, experiment, spike, could we |
| `plan` | plan, design, architect, spec, outline, strategy |
| `learn` | explain, how does, teach, understand, onboard, walk me through |

Wire into `parse_session()` so every parsed session gets a goal.

**Tests in:** `tests/test_fluency/test_parser.py`

---

## Task 2.5: Validate classifier

**Manual step.** After Task 2, run classifier on 25 real sessions, manually compare. If <60% → simplify to 3 goals (ship / non-ship / unknown). Document result in this plan file.

---

## Task 3: Reframe chains

**File:** `src/fluency/sequence.py` (modify)

- Rename `SessionChain.shipped` → `SessionChain.has_commits`
- `classify_chain_pattern()`: thrashing requires goal=ship (or unknown) AND 0 commits across ≥3 sessions. An investigate/review/explore chain with 0 commits is normal.
- Update all references in report.py and cli.py.

**Tests:** `tests/test_fluency/test_sequence.py` — add `test_investigate_chain_not_thrashing`

---

## Task 4: Rewrite reports

**Files:** `src/fluency/report.py`, `src/fluency/cli.py` (modify)

All report changes in one pass:

### 4a. `format_profile_report` (report.py)

**SESSION SHAPES** — drop "Ship rate" column. Show goal distribution within each shape:
```
  explore_build           22 (44%)  goal: ship 20, explore 2     22 with commits
  review_only              8 (16%)  goal: review 7, unknown 1
```

**BEHAVIORAL SIGNALS** — group by goal instead of shipped/zero:
```
BY GOAL (median values):
                          ship (n=85)   investigate (n=42)   explore (n=28)
  active min                   62              14                  31
  prompts                      19               5                   8
  tool calls                  280              48                 120
  commits                      11               0                   2
```

**DURABILITY** — add scope note:
```
  Note: Durability metrics favor well-scoped tasks (bugfixes). Compare within goal categories.
```

Only show durability for goal=ship sessions. Other goals' durability is noise.

**CHAINS** — replace "shipped" with "has_commits", use goal-aware pattern names.

**Add NOTE footer:**
```
NOTE: Sessions evaluated against inferred goal. A review with 0 commits is
working as intended. Intent inferred from opening prompt — may be imperfect.
Durability favors well-scoped tasks over ambiguous ones.
```

### 4b. `format_session_report` (report.py)

Add goal to header. Add session narrative (see Task 5).

### 4c. `cmd_report` markdown (cli.py)

- Session Shapes: goal distribution instead of ship rate
- Behavioral Signals: by goal instead of shipped/zero
- Chains: "has commits" instead of "ship rate"
- Add "Methodology Notes" section at bottom

### 4d. `cmd_retro` (cli.py)

Show goal in output. Include session narrative.

---

## Task 5: Session narrative

**File:** `src/fluency/report.py` (modify)

Add `generate_narrative(sa: SessionAnalysis) -> str` — one paragraph describing the session in plain language.

Example outputs:
- "45-minute investigation session. You asked 6 questions across 2 segments, the agent read 23 files and ran 4 test commands. No code was changed — typical for investigate sessions."
- "90-minute build session. Started from a plan, produced 14 commits touching 8 files. Tests went FAIL → PASS → PASS. 89% of lines still in the codebase."
- "12-minute review. Looked at 3 files, found 2 issues. No commits — review sessions rarely produce commits."

Rules:
- State the goal and duration
- Summarize what happened (prompts, tool calls, edits, tests)
- If goal is non-ship, explicitly note that zero commits is expected
- If durability data exists and goal=ship, include survival %
- No judgment language ("good", "bad", "should have")

**Tests:** `tests/test_fluency/test_report.py`

---

## Task 6: Bias resistance tests

**File:** `tests/test_fluency/test_report.py` (create)

```python
def test_review_sessions_not_penalized():
    """Report should not frame review+0 commits as failure."""

def test_investigate_chain_not_thrashing():
    """3 investigate sessions with 0 commits = research, not thrashing."""

def test_durability_note_present():
    """Profile report includes scope bias note."""

def test_methodology_notes_in_markdown():
    """Markdown report includes methodology notes section."""

def test_no_universal_ship_rate():
    """Report should not show ship rate across all shapes."""

def test_narrative_no_judgment():
    """Narrative should not contain judgment words."""

def test_narrative_explains_zero_commits():
    """Narrative for non-ship goal should explain 0 commits is expected."""

def test_goal_classification_edge_cases():
    """Ambiguous prompts, multi-intent, plan execution phrasing."""
```

---

## Task 7: Update README

**File:** `README.md` (modify)

Update sample output to reflect new format. Remove "ship rate" from examples. Add session narrative to retro example.

---

## Execution Order

1. Task 1 — model field
2. Task 2 — classifier + tests
3. Task 2.5 — manual validation (may adjust classifier)
4. Task 6 — write failing tests for new behavior
5. Task 3 — chain reframe
6. Task 5 — session narrative
7. Task 4 — report rewrite (biggest change, depends on all above)
8. Task 7 — README

## Kill Criteria

- Goal classifier <60% on 25-session manual review → simplify to ship/non-ship/unknown
- Reports become harder to read than current → revert formatting, keep goal field
