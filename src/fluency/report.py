"""Format session retrospective and profile reports.

Data-forward: shows what happened, not what we think it means.
No composite scores. Durability is a fact. Behavioral signals shown directly.
"""

from collections import Counter
from fluency.models import SessionAnalysis
from fluency.git_tracer import DurabilityReport
from fluency.sequence import SessionChain


def format_session_report(
    sa: SessionAnalysis,
    dr: DurabilityReport | None = None,
) -> str:
    """Format a single session retrospective."""
    lines = []

    # Header
    lines.append(f"{'=' * 70}")
    lines.append(f"  SESSION: {sa.id}")
    lines.append(f"  Source: {sa.source}  |  Shape: {sa.session_shape}")
    lines.append(f"{'=' * 70}")
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

    # Durability
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
    
    Data-forward: durability facts, behavioral signals, shapes, chains.
    No composite scores.
    """
    lines = []

    scored = [(sa, dr) for sa, dr in sessions if sa.session_shape != "abandoned"]
    if not scored:
        return "No scoreable sessions found."

    # Count session types
    types = Counter(sa.session_shape for sa, _ in scored)
    type_str = " · ".join(f"{v} {k}" for k, v in types.most_common())

    # Header
    lines.append(f"{'═' * 70}")
    title = f"  {project_name}" if project_name else "  PROFILE"
    if source_label:
        title += f" — {source_label}"
    lines.append(title)
    lines.append(f"  {len(scored)} sessions ({type_str})")
    lines.append(f"{'═' * 70}")
    lines.append("")

    # ── DURABILITY ──
    dur_sessions = [(sa, dr) for sa, dr in scored if dr and dr.total_lines_added > 0]
    git_matched = sum(1 for sa, dr in scored if dr is not None)

    if dur_sessions:
        total_added = sum(dr.total_lines_added for _, dr in dur_sessions)
        total_surviving = sum(dr.total_lines_surviving for _, dr in dur_sessions)
        total_bugs = sum(dr.lines_lost_to_bugs for _, dr in dur_sessions)
        total_arch = sum(dr.lines_lost_to_architecture for _, dr in dur_sessions)
        total_evolve = sum(dr.lines_lost_to_evolution for _, dr in dur_sessions)
        total_refactor = sum(dr.lines_lost_to_refactor for _, dr in dur_sessions)
        total_maintenance = sum(dr.lines_lost_to_maintenance for _, dr in dur_sessions)
        bug_count = sum(dr.bug_count for _, dr in dur_sessions)

        raw_pct = total_surviving / total_added * 100 if total_added else 0
        adj_denom = total_added - total_arch
        adj_pct = min(100, total_surviving / adj_denom * 100) if adj_denom > 0 else 100

        lines.append(f"DURABILITY ({len(dur_sessions)} sessions with git data)")
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
        lines.append("")

    # ── SESSION SHAPES ──
    lines.append("SESSION SHAPES:")
    for shape, count in types.most_common():
        pct = 100 * count // len(scored)
        shape_sessions = [(sa, dr) for sa, dr in scored if sa.session_shape == shape]
        with_commits = sum(1 for sa, _ in shape_sessions if sa.commit_count > 0)
        if with_commits > 0:
            avg_commits = sum(sa.commit_count for sa, _ in shape_sessions if sa.commit_count > 0) / with_commits
            commit_str = f"  {with_commits} with commits   avg {avg_commits:.1f} commits"
        else:
            commit_str = ""
        lines.append(f"  {shape:22s} {count:3d} ({pct:2d}%){commit_str}")
    lines.append("")

    # ── CHAINS ──
    if chains:
        multi = [c for c in chains if len(c.sessions) > 1]
        if multi:
            pattern_counts = Counter(c.pattern for c in multi)
            lines.append(f"CHAINS ({len(multi)} multi-session)")
            for pattern, count in pattern_counts.most_common():
                pattern_chains = [c for c in multi if c.pattern == pattern]
                shipped = sum(1 for c in pattern_chains if c.shipped)
                total_sess = sum(len(c.sessions) for c in pattern_chains)
                warning = ""
                if pattern == "thrashing":
                    warning = f"  ⚠️ {total_sess} sessions, 0 output"
                elif pattern == "review_stall":
                    warning = f"  ⚠️ found problems but didn't fix"
                lines.append(f"  {pattern:22s} {count:2d} chains → {shipped} shipped{warning}")
            lines.append("")

    # ── BEHAVIORAL SIGNALS ──
    shipped = [(sa, dr) for sa, dr in scored if sa.commit_count > 0]
    zero = [(sa, dr) for sa, dr in scored if sa.commit_count == 0]

    if shipped and zero:
        lines.append(f"BEHAVIORAL SIGNALS (shipped vs zero-commit)")
        lines.append(f"  {'':24s} {'Shipped':>10s} (n={len(shipped)})    {'Zero':>6s} (n={len(zero)})")

        def _median(vals):
            if not vals:
                return 0
            s = sorted(vals)
            return s[len(s) // 2]

        signals = [
            ("active min", [sa.active_min for sa, _ in shipped], [sa.active_min for sa, _ in zero]),
            ("prompts", [sa.human_prompt_count for sa, _ in shipped], [sa.human_prompt_count for sa, _ in zero]),
            ("tool calls", [sa.tool_call_count for sa, _ in shipped], [sa.tool_call_count for sa, _ in zero]),
            ("edits", [sa.edit_count for sa, _ in shipped], [sa.edit_count for sa, _ in zero]),
            ("has tests", None, None),  # special: percentage
            ("has subagents", None, None),  # special: percentage
        ]

        for name, s_vals, z_vals in signals:
            if name == "has tests":
                s_pct = sum(1 for sa, _ in shipped if sa.test_arc) * 100 // max(len(shipped), 1)
                z_pct = sum(1 for sa, _ in zero if sa.test_arc) * 100 // max(len(zero), 1)
                lines.append(f"  {name:24s} {s_pct:>7d}%            {z_pct:>4d}%")
            elif name == "has subagents":
                s_pct = sum(1 for sa, _ in shipped if sa.has_subagents) * 100 // max(len(shipped), 1)
                z_pct = sum(1 for sa, _ in zero if sa.has_subagents) * 100 // max(len(zero), 1)
                lines.append(f"  {name:24s} {s_pct:>7d}%            {z_pct:>4d}%")
            else:
                s_med = _median(s_vals)
                z_med = _median(z_vals)
                if isinstance(s_med, float):
                    lines.append(f"  {name:24s} {s_med:>8.0f}            {z_med:>5.0f}")
                else:
                    lines.append(f"  {name:24s} {s_med:>8d}            {z_med:>5d}")
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
        lines.append(f"  {'Date':<12} {'Shape':>20} {'Cmts':>5} {'Surv':>6}  Intent")
        lines.append(f"  {'─'*12} {'─'*20} {'─'*5} {'─'*6}  {'─'*30}")
        for sa, dr in recent:
            date_str = str(sa.segments[0].start)[:10] if sa.segments else "?"
            surv = f"{dr.raw_survival_pct*100:.0f}%" if dr and dr.total_lines_added > 0 else "  —"
            intent = sa.first_intent[:30].replace("\n", " ")
            lines.append(f"  {date_str:<12} {sa.session_shape:>20} {sa.commit_count:5d} {surv:>6s}  {intent}")
        lines.append("")

    return "\n".join(lines)


def _total_active(sa: SessionAnalysis) -> float:
    return sa.ai_working_sec + sa.human_waiting_sec + sa.human_acting_sec


def _pct(num: float, denom: float) -> str:
    if denom <= 0:
        return "0%"
    return f"{100 * num / denom:.0f}%"
