"""Format session retrospective and profile reports.

Data-forward: shows what happened, not what we think it means.
Sessions evaluated against their own goal, not a universal bar.
No composite scores. Known biases named in output.
"""

from collections import Counter
from fluency.models import SessionAnalysis
from fluency.git_tracer import DurabilityReport
from fluency.sequence import SessionChain
from fluency.insights import detect_insights, format_insights


# Goals where zero commits is expected behavior
_NON_SHIP_GOALS = {"investigate", "review", "explore", "plan", "learn"}


def generate_narrative(sa: SessionAnalysis, dr: DurabilityReport | None = None) -> str:
    """Generate a one-paragraph human-readable summary of the session.

    Describes the *flow* — how the session unfolded — not just counts.
    No judgment language. States what happened and contextualizes against the goal.
    """
    goal = sa.session_goal if sa.session_goal != "unknown" else "general"
    goal_label = {
        "ship": "build", "investigate": "investigation", "review": "review",
        "explore": "exploration", "plan": "planning", "learn": "learning",
        "general": "",
    }.get(goal, "")

    # Duration
    duration = f"{sa.active_min:.0f}-minute" if sa.active_min > 0 else ""
    segments = f" across {len(sa.segments)} segments" if len(sa.segments) > 1 else ""
    label = f" {goal_label}" if goal_label else ""
    intro = f"{duration}{label} session{segments}.".strip().capitalize()

    # Flow description from steering events
    flow_str = _describe_flow(sa)

    # Tests
    test_str = ""
    if sa.test_arc:
        arc = sa.test_arc
        if arc[0] == "FAIL" and arc[-1] == "PASS":
            test_str = f" Tests went from failing to passing ({len(arc)} runs)."
        elif all(r == "PASS" for r in arc):
            test_str = f" Tests passed throughout ({len(arc)} runs)."
        else:
            test_str = f" Test trajectory: {' → '.join(arc)}."

    # Commits and goal context
    commit_str = ""
    if sa.commit_count > 0:
        commit_str = f" Produced {sa.commit_count} commits."
    elif sa.session_goal in _NON_SHIP_GOALS:
        commit_str = f" No code was committed — typical for {goal} sessions."
    elif sa.session_goal == "ship":
        commit_str = " No commits were produced."

    # Durability (only for ship-goal sessions)
    dur_str = ""
    if dr and dr.total_lines_added > 0 and sa.session_goal in ("ship", "unknown"):
        dur_str = f" {dr.raw_survival_pct*100:.0f}% of lines added still in the codebase."

    # Subagents
    sub_str = " Used subagents for parallel work." if sa.has_subagents else ""

    return f"{intro} {flow_str}{test_str}{commit_str}{dur_str}{sub_str}".strip()


def _describe_flow(sa: SessionAnalysis) -> str:
    """Describe how the session flowed based on steering events.

    Classifies the interaction pattern:
    - Long autonomous run (1-2 prompts, many tool calls)
    - Conversational (many short exchanges)
    - Guided (moderate steering with autonomous stretches)
    - Redirected (human course-corrected mid-session)
    """
    events = sa.steering_events
    if not events:
        # Fall back to basic counts
        parts = []
        if sa.human_prompt_count:
            parts.append(f"You sent {sa.human_prompt_count} prompts")
        if sa.tool_call_count:
            parts.append(f"the agent made {sa.tool_call_count} tool calls")
        if sa.edit_count:
            parts.append(f"edited {sa.edit_count} locations ({sa.lines_changed} lines)")
        return " " + ", ".join(parts) + "." if parts else ""

    n_events = len(events)
    max_run = max(e.run_after for e in events) if events else 0
    avg_run = sum(e.run_after for e in events) / n_events if n_events else 0
    total_ai_turns = sum(e.run_after for e in events)

    # Detect pattern
    if n_events <= 2 and max_run >= 30:
        # Long autonomous run
        edit_note = f", editing {sa.edit_count} locations" if sa.edit_count else ""
        return f" You gave a single direction and the agent ran autonomously for {total_ai_turns} tool calls{edit_note}."

    if n_events <= 3 and max_run >= 15:
        # Mostly autonomous with light steering
        edit_note = f", making {sa.edit_count} edits" if sa.edit_count else ""
        return f" {n_events} steering points, with the agent running up to {max_run} tool calls between prompts{edit_note}."

    if avg_run < 3 and n_events >= 5:
        # Conversational
        return f" Conversational pattern — {n_events} back-and-forth exchanges, the agent averaging {avg_run:.0f} actions per prompt."

    # Check for redirect pattern: a long run followed by shorter ones
    if n_events >= 3:
        runs = [e.run_after for e in events]
        # Find if there's a big run followed by smaller runs (course correction)
        for i in range(len(runs) - 1):
            if runs[i] >= 15 and i > 0:
                before = sum(runs[:i]) / i
                after_runs = runs[i+1:]
                if after_runs and max(after_runs) < runs[i] * 0.5:
                    edit_note = f" Edited {sa.edit_count} locations total." if sa.edit_count else ""
                    return f" The agent ran {runs[i]} tool calls on one stretch before you redirected. {n_events} total steering points.{edit_note}"

    # Guided pattern (default for moderate steering)
    edit_note = f", {sa.edit_count} edits across {sa.lines_changed} lines" if sa.edit_count else ""
    return f" {n_events} steering points guiding {total_ai_turns} agent actions{edit_note}."


def format_session_report(
    sa: SessionAnalysis,
    dr: DurabilityReport | None = None,
) -> str:
    """Format a single session retrospective."""
    lines = []

    # Header — includes goal
    lines.append(f"{'=' * 70}")
    lines.append(f"  SESSION: {sa.id}")
    goal_str = f"  |  Goal: {sa.session_goal}" if sa.session_goal != "unknown" else ""
    lines.append(f"  Source: {sa.source}  |  Shape: {sa.session_shape}{goal_str}")
    lines.append(f"{'=' * 70}")
    lines.append("")

    # Narrative
    narrative = generate_narrative(sa, dr)
    lines.append(f"SUMMARY: {narrative}")
    lines.append("")

    # Intent
    intent = sa.first_intent[:200].replace("\n", " ")
    lines.append(f"INTENT: {intent}")
    lines.append("")

    # Time
    lines.append("TIME:")
    lines.append(f"  Wall clock:    {sa.wall_clock_min:6.1f} min")
    lines.append(f"  Active time:   {sa.active_min:6.1f} min  ({len(sa.segments)} segment{'s' if len(sa.segments) != 1 else ''})")
    for i, seg in enumerate(sa.segments):
        lines.append(f"    Segment {i+1}:  {seg.duration_sec/60:5.1f} min")
    lines.append(f"  AI working:    {sa.ai_working_sec/60:6.1f} min  ({_pct(sa.ai_working_sec, _total_active(sa))} of active)")
    lines.append(f"  Human waiting: {sa.human_waiting_sec/60:6.1f} min")
    lines.append(f"  Human acting:  {sa.human_acting_sec/60:6.1f} min  (~{sa.human_chars} chars typed)")
    lines.append("")

    # Output
    ratio = sa.tool_call_count / max(sa.human_prompt_count, 1)
    lines.append("OUTPUT:")
    lines.append(f"  Prompts:       {sa.human_prompt_count:4d}")
    lines.append(f"  Tool calls:    {sa.tool_call_count:4d}  (ratio 1:{ratio:.0f})")
    lines.append(f"  Edits:         {sa.edit_count:4d}  ({sa.lines_changed} lines)")
    lines.append(f"  Commits:       {sa.commit_count:4d}")
    lines.append(f"  Tests:         {' → '.join(sa.test_arc) if sa.test_arc else 'none'}")
    if sa.has_subagents:
        lines.append(f"  Subagents:     yes")
    lines.append("")

    # Durability (only meaningful for ship-goal sessions)
    if dr and dr.total_lines_added > 0:
        lines.append("DURABILITY:")
        lines.append(f"  Lines added:     {dr.total_lines_added:,}")
        lines.append(f"  Surviving:       {dr.total_lines_surviving:,}  ({dr.raw_survival_pct*100:.0f}% raw)")
        adj_denom = dr.total_lines_added - dr.lines_lost_to_architecture
        if adj_denom > 0 and dr.lines_lost_to_architecture > 0:
            lines.append(f"  Adjusted:        {dr.adjusted_survival_pct*100:.0f}%  (excl. architecture)")
        losses = []
        if dr.lines_lost_to_bugs:
            losses.append(f"{dr.lines_lost_to_bugs:,} bug-fix ({dr.bug_count} commits)")
        if dr.lines_lost_to_architecture:
            losses.append(f"{dr.lines_lost_to_architecture:,} architecture")
        if dr.lines_lost_to_evolution:
            losses.append(f"{dr.lines_lost_to_evolution:,} evolution")
        if dr.lines_lost_to_refactor:
            losses.append(f"{dr.lines_lost_to_refactor:,} refactor")
        if losses:
            lines.append(f"  Losses:          {' · '.join(losses)}")
        lines.append(f"  Merged:          {'yes' if dr.branch_merged else 'no'}")
        if sa.session_goal not in ("ship", "unknown"):
            lines.append(f"  Note:            Durability is less meaningful for {sa.session_goal} sessions.")
        lines.append("")

    # System leverage
    leverage = []
    if sa.skills_invoked:
        leverage.append(f"Skills: {', '.join(sa.skills_invoked)}")
    if sa.slash_commands:
        leverage.append(f"Commands: {', '.join(sa.slash_commands)}")
    if sa.has_subagents:
        leverage.append("Subagents: yes")
    if sa.skill_files_edited:
        leverage.append(f"Skills edited: {', '.join(sa.skill_files_edited)}")
    if sa.claude_md_edited:
        leverage.append("CLAUDE.md updated")

    if leverage:
        lines.append("SYSTEM LEVERAGE:")
        for l in leverage:
            lines.append(f"  {l}")
        lines.append("")

    return "\n".join(lines)


def format_profile_report(
    sessions: list[tuple[SessionAnalysis, DurabilityReport | None]],
    project_name: str = "",
    chains: list[SessionChain] | None = None,
    source_label: str = "",
) -> str:
    """Format an aggregate profile across multiple sessions.

    Goal-aware: groups by intent, evaluates against goal-appropriate criteria.
    No composite scores. Known biases named.
    """
    lines = []

    scored = [(sa, dr) for sa, dr in sessions if sa.session_shape != "abandoned"]
    if not scored:
        return "No scoreable sessions found."

    # Count goals and shapes
    goals = Counter(sa.session_goal for sa, _ in scored)
    types = Counter(sa.session_shape for sa, _ in scored)
    goal_str = " · ".join(f"{v} {k}" for k, v in goals.most_common())

    # Header
    lines.append(f"{'═' * 70}")
    title = f"  {project_name}" if project_name else "  PROFILE"
    if source_label:
        title += f" — {source_label}"
    lines.append(title)
    lines.append(f"  {len(scored)} sessions ({goal_str})")
    lines.append(f"{'═' * 70}")
    lines.append("")

    # ── INSIGHTS (top patterns) ──
    insights = detect_insights(scored, chains)
    if insights:
        lines.append(format_insights(insights))

    # ── DURABILITY (ship-goal sessions only) ──
    dur_sessions = [(sa, dr) for sa, dr in scored
                    if dr and dr.total_lines_added > 0
                    and sa.session_goal in ("ship", "unknown")]
    all_dur_sessions = [(sa, dr) for sa, dr in scored if dr and dr.total_lines_added > 0]

    if all_dur_sessions:
        total_added = sum(dr.total_lines_added for _, dr in all_dur_sessions)
        total_surviving = sum(dr.total_lines_surviving for _, dr in all_dur_sessions)
        total_bugs = sum(dr.lines_lost_to_bugs for _, dr in all_dur_sessions)
        total_arch = sum(dr.lines_lost_to_architecture for _, dr in all_dur_sessions)
        total_evolve = sum(dr.lines_lost_to_evolution for _, dr in all_dur_sessions)
        total_refactor = sum(dr.lines_lost_to_refactor for _, dr in all_dur_sessions)
        total_maintenance = sum(dr.lines_lost_to_maintenance for _, dr in all_dur_sessions)
        bug_count = sum(dr.bug_count for _, dr in all_dur_sessions)

        raw_pct = total_surviving / total_added * 100 if total_added else 0
        adj_denom = total_added - total_arch
        adj_pct = min(100, total_surviving / adj_denom * 100) if adj_denom > 0 else 100

        lines.append(f"DURABILITY ({len(all_dur_sessions)} sessions with git data)")
        lines.append(f"  {total_added:,} lines added → {total_surviving:,} surviving ({raw_pct:.0f}% raw, {adj_pct:.0f}% adjusted)")

        loss_parts = []
        if total_bugs:
            loss_parts.append(f"{total_bugs:,} bug-fix ({bug_count} commits)")
        if total_arch:
            loss_parts.append(f"{total_arch:,} architecture")
        if total_evolve:
            loss_parts.append(f"{total_evolve:,} evolution")
        if total_refactor:
            loss_parts.append(f"{total_refactor:,} refactor")
        if total_maintenance:
            loss_parts.append(f"{total_maintenance:,} maintenance")
        if loss_parts:
            lines.append(f"  Losses: {' · '.join(loss_parts)}")
        if bug_count and total_added:
            lines.append(f"  Bug rate: {total_bugs/total_added*100:.1f}% of lines, {bug_count} fix commits")
        lines.append(f"  Note: Durability favors well-scoped tasks (bugfixes). Compare within goal categories.")
        lines.append("")

    # ── SESSION SHAPES (with goal distribution) ──
    lines.append("SESSION SHAPES:")
    for shape, count in types.most_common():
        pct = 100 * count // len(scored)
        shape_sessions = [(sa, dr) for sa, dr in scored if sa.session_shape == shape]

        # Goal distribution within this shape
        shape_goals = Counter(sa.session_goal for sa, _ in shape_sessions)
        goal_parts = ", ".join(f"{g} {c}" for g, c in shape_goals.most_common() if c > 0)
        goal_info = f"  goal: {goal_parts}" if goal_parts else ""

        # Commit info
        with_commits = sum(1 for sa, _ in shape_sessions if sa.commit_count > 0)
        if with_commits > 0:
            avg_commits = sum(sa.commit_count for sa, _ in shape_sessions if sa.commit_count > 0) / with_commits
            commit_str = f"  {with_commits} with commits"
        else:
            commit_str = ""

        lines.append(f"  {shape:22s} {count:3d} ({pct:2d}%){goal_info}{commit_str}")
    lines.append("")

    # ── CHAINS ──
    if chains:
        multi = [c for c in chains if len(c.sessions) > 1]
        if multi:
            pattern_counts = Counter(c.pattern for c in multi)
            lines.append(f"CHAINS ({len(multi)} multi-session)")
            for pattern, count in pattern_counts.most_common():
                pattern_chains = [c for c in multi if c.pattern == pattern]
                with_commits = sum(1 for c in pattern_chains if c.has_commits)
                total_sess = sum(len(c.sessions) for c in pattern_chains)
                note = ""
                if pattern == "thrashing":
                    note = f"  ⚠️ {total_sess} sessions, goal was to ship, 0 output"
                elif pattern == "investigation":
                    note = f"  {total_sess} sessions (0 commits expected)"
                elif pattern == "review_cycle":
                    note = f"  {total_sess} sessions (0 commits expected)"
                elif pattern == "exploration":
                    note = f"  {total_sess} sessions (0 commits expected)"
                elif pattern == "research":
                    note = f"  {total_sess} sessions"
                else:
                    note = f"  {with_commits} with commits" if with_commits else ""
                lines.append(f"  {pattern:22s} {count:2d} chains{note}")
            lines.append("")

    # ── BY GOAL (replaces shipped vs zero-commit) ──
    goal_groups = {}
    for sa, dr in scored:
        goal_groups.setdefault(sa.session_goal, []).append((sa, dr))

    # Only show goals with ≥3 sessions
    displayable_goals = [(g, slist) for g, slist in goal_groups.items() if len(slist) >= 3]
    if len(displayable_goals) >= 2:
        lines.append("BY GOAL (median values):")

        # Build header
        header_parts = []
        for g, slist in sorted(displayable_goals, key=lambda x: -len(x[1])):
            header_parts.append(f"{g} (n={len(slist)})")
        lines.append(f"  {'':24s} " + "  ".join(f"{h:>18s}" for h in header_parts))

        def _median(vals):
            if not vals:
                return 0
            s = sorted(vals)
            return s[len(s) // 2]

        signal_names = ["active min", "prompts", "tool calls", "edits", "commits"]
        for signal in signal_names:
            row_parts = []
            for g, slist in sorted(displayable_goals, key=lambda x: -len(x[1])):
                if signal == "active min":
                    val = _median([sa.active_min for sa, _ in slist])
                    row_parts.append(f"{val:>18.0f}")
                elif signal == "prompts":
                    val = _median([sa.human_prompt_count for sa, _ in slist])
                    row_parts.append(f"{val:>18d}")
                elif signal == "tool calls":
                    val = _median([sa.tool_call_count for sa, _ in slist])
                    row_parts.append(f"{val:>18d}")
                elif signal == "edits":
                    val = _median([sa.edit_count for sa, _ in slist])
                    row_parts.append(f"{val:>18d}")
                elif signal == "commits":
                    val = _median([sa.commit_count for sa, _ in slist])
                    row_parts.append(f"{val:>18d}")
            lines.append(f"  {signal:24s} " + "  ".join(row_parts))

        # Tests and subagents as percentages
        for signal in ["has tests", "has subagents"]:
            row_parts = []
            for g, slist in sorted(displayable_goals, key=lambda x: -len(x[1])):
                if signal == "has tests":
                    pct = sum(1 for sa, _ in slist if sa.test_arc) * 100 // max(len(slist), 1)
                else:
                    pct = sum(1 for sa, _ in slist if sa.has_subagents) * 100 // max(len(slist), 1)
                row_parts.append(f"{pct:>17d}%")
            lines.append(f"  {signal:24s} " + "  ".join(row_parts))

        lines.append("")

    # ── PATTERNS ──
    lines.append("PATTERNS:")
    high_lev = sum(1 for sa, _ in scored
                   if sa.tool_call_count / max(sa.human_prompt_count, 1) >= 20)
    tested = sum(1 for sa, _ in scored if sa.test_arc)
    skilled = sum(1 for sa, _ in scored if sa.has_skills or sa.slash_commands)
    with_commits = sum(1 for sa, _ in scored if sa.commit_count > 0)

    lines.append(f"  High leverage (≥1:20):  {high_lev}/{len(scored)} ({100*high_lev//len(scored)}%)")
    lines.append(f"  Sessions with tests:    {tested}/{len(scored)} ({100*tested//len(scored)}%)")
    lines.append(f"  Using skills/commands:  {skilled}/{len(scored)} ({100*skilled//len(scored)}%)")
    lines.append(f"  Sessions with commits:  {with_commits}/{len(scored)} ({100*with_commits//len(scored)}%)")
    lines.append("")

    # ── EVOLUTION (last 10 sessions) ──
    dated = sorted(
        [(sa, dr) for sa, dr in scored if sa.segments],
        key=lambda x: x[0].segments[0].start if x[0].segments else "",
    )
    if len(dated) >= 3:
        recent = dated[-10:]
        lines.append("EVOLUTION (last 10 sessions):")
        lines.append(f"  {'Date':<12} {'Goal':>12} {'Shape':>20} {'Cmts':>5} {'Surv':>6}  Intent")
        lines.append(f"  {'─'*12} {'─'*12} {'─'*20} {'─'*5} {'─'*6}  {'─'*30}")
        for sa, dr in recent:
            date_str = str(sa.segments[0].start)[:10] if sa.segments else "?"
            surv = f"{dr.raw_survival_pct*100:.0f}%" if dr and dr.total_lines_added > 0 else "  —"
            intent = sa.first_intent[:30].replace("\n", " ")
            lines.append(f"  {date_str:<12} {sa.session_goal:>12} {sa.session_shape:>20} {sa.commit_count:5d} {surv:>6s}  {intent}")
        lines.append("")

    # ── NOTE ──
    lines.append("NOTE: Sessions evaluated against inferred goal. A review with 0 commits is")
    lines.append("working as intended. Intent inferred from opening prompt — may be imperfect.")
    lines.append("Durability favors well-scoped tasks over ambiguous ones.")
    lines.append("")

    return "\n".join(lines)


def _total_active(sa: SessionAnalysis) -> float:
    return sa.ai_working_sec + sa.human_waiting_sec + sa.human_acting_sec


def _pct(num: float, denom: float) -> str:
    if denom <= 0:
        return "0%"
    return f"{100 * num / denom:.0f}%"
