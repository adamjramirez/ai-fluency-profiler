"""Insight detection — surface interesting patterns from session data.

Each insight: states a fact, shows a contrast, includes a caveat.
Only fires when the data shows a meaningful difference.
Returns 4-6 strongest insights per project.
"""

from dataclasses import dataclass
from collections import Counter

from fluency.models import SessionAnalysis
from fluency.git_tracer import DurabilityReport
from fluency.sequence import SessionChain


@dataclass
class Insight:
    """A single detected pattern worth surfacing."""
    text: str
    strength: float  # 0-1, used to rank and select top insights
    category: str    # for deduplication: test, subagent, chain, leverage, etc.


def detect_insights(
    sessions: list[tuple[SessionAnalysis, DurabilityReport | None]],
    chains: list[SessionChain] | None = None,
    max_insights: int = 6,
) -> list[Insight]:
    """Detect and return the strongest insights from session data.

    Only surfaces patterns with meaningful contrasts.
    Returns at most max_insights, at least 0.
    """
    scored = [(sa, dr) for sa, dr in sessions if sa.session_shape != "abandoned"]
    if len(scored) < 5:
        return []

    candidates: list[Insight] = []

    candidates.extend(_test_correlation(scored))
    candidates.extend(_subagent_leverage(scored))
    candidates.extend(_chain_patterns(chains or []))
    candidates.extend(_thrashing_diagnosis(chains or [], scored))
    candidates.extend(_leverage_ratio(scored))
    candidates.extend(_session_duration(scored))
    candidates.extend(_review_efficiency(scored))
    candidates.extend(_plan_execution(scored, chains or []))
    candidates.extend(_warm_start(chains or []))
    candidates.extend(_goal_contrast(scored))

    # Deduplicate by category (keep strongest per category)
    best_per_category: dict[str, Insight] = {}
    for ins in candidates:
        if ins.category not in best_per_category or ins.strength > best_per_category[ins.category].strength:
            best_per_category[ins.category] = ins

    # Sort by strength, return top N
    ranked = sorted(best_per_category.values(), key=lambda i: -i.strength)
    return ranked[:max_insights]


def format_insights(insights: list[Insight]) -> str:
    """Format insights for display."""
    if not insights:
        return ""
    lines = ["INSIGHTS:"]
    for ins in insights:
        lines.append(f"  → {ins.text}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Individual insight detectors
# ---------------------------------------------------------------------------


def _median(vals: list) -> float:
    if not vals:
        return 0
    s = sorted(vals)
    return s[len(s) // 2]


def _test_correlation(scored: list[tuple[SessionAnalysis, DurabilityReport | None]]) -> list[Insight]:
    """Compare test usage between sessions with commits vs without."""
    with_commits = [sa for sa, _ in scored if sa.commit_count > 0]
    without = [sa for sa, _ in scored if sa.commit_count == 0 and sa.session_goal in ("ship", "unknown")]

    if len(with_commits) < 5 or len(without) < 5:
        return []

    pct_with = sum(1 for sa in with_commits if sa.test_arc) * 100 // len(with_commits)
    pct_without = sum(1 for sa in without if sa.test_arc) * 100 // len(without)
    gap = pct_with - pct_without

    if gap < 20:
        return []

    return [Insight(
        text=f"Sessions that produced commits used tests {pct_with}% of the time vs {pct_without}% for ship/unknown sessions that didn't.",
        strength=min(1.0, gap / 60),
        category="test",
    )]


def _subagent_leverage(scored: list[tuple[SessionAnalysis, DurabilityReport | None]]) -> list[Insight]:
    """Compare outcomes with/without subagents."""
    with_sub = [sa for sa, _ in scored if sa.has_subagents and sa.commit_count > 0]
    without_sub = [sa for sa, _ in scored if not sa.has_subagents and sa.commit_count > 0]

    if len(with_sub) < 3 or len(without_sub) < 5:
        return []

    med_with = _median([sa.commit_count for sa in with_sub])
    med_without = _median([sa.commit_count for sa in without_sub])

    if med_without == 0 or med_with / max(med_without, 1) < 1.5:
        return []

    ratio = med_with / max(med_without, 1)

    # Check if subagent sessions are also longer (caveat)
    time_with = _median([sa.active_min for sa in with_sub])
    time_without = _median([sa.active_min for sa in without_sub])
    caveat = ""
    if time_with > time_without * 1.3:
        caveat = f" (Subagent sessions also run {time_with:.0f} vs {time_without:.0f} min — may reflect task size.)"

    return [Insight(
        text=f"Sessions with subagents: {med_with:.0f} median commits. Without: {med_without:.0f}.{caveat}",
        strength=min(1.0, ratio / 4),
        category="subagent",
    )]


def _chain_patterns(chains: list[SessionChain]) -> list[Insight]:
    """Which chain patterns consistently ship?"""
    multi = [c for c in chains if len(c.sessions) > 1]
    if len(multi) < 3:
        return []

    pattern_stats: dict[str, tuple[int, int]] = {}  # pattern → (total, with_commits)
    for c in multi:
        t, s = pattern_stats.get(c.pattern, (0, 0))
        pattern_stats[c.pattern] = (t + 1, s + (1 if c.has_commits else 0))

    insights = []
    for pattern, (total, shipped) in pattern_stats.items():
        if total < 2:
            continue
        rate = shipped / total
        if rate >= 0.75 and total >= 3:
            insights.append(Insight(
                text=f"{shipped} of {total} {pattern} chains produced commits. This workflow pattern works.",
                strength=rate * min(1.0, total / 5),
                category="chain_success",
            ))
        elif rate == 0 and total >= 2 and pattern not in ("investigation", "review_cycle", "exploration", "research", "planning", "thrashing"):
            # Don't duplicate thrashing — that has its own detector
            insights.append(Insight(
                text=f"0 of {total} {pattern} chains produced output.",
                strength=0.4 + (total / 20),
                category="chain_failure",
            ))

    return insights


def _thrashing_diagnosis(chains: list[SessionChain], scored: list[tuple[SessionAnalysis, DurabilityReport | None]]) -> list[Insight]:
    """What's common across thrashing chains?"""
    thrashing = [c for c in chains if c.pattern == "thrashing"]
    if len(thrashing) < 2:
        return []

    total_sessions = sum(len(c.sessions) for c in thrashing)
    thrash_sessions = [sa for c in thrashing for sa in c.sessions]

    # Check for common traits
    has_tests = sum(1 for sa in thrash_sessions if sa.test_arc)
    has_subagents = sum(1 for sa in thrash_sessions if sa.has_subagents)

    traits = []
    if has_tests == 0:
        traits.append("none had tests")
    elif has_tests < len(thrash_sessions) * 0.2:
        traits.append(f"only {has_tests*100//len(thrash_sessions)}% had tests")

    if has_subagents == 0:
        traits.append("none used subagents")

    med_prompts = _median([sa.human_prompt_count for sa in thrash_sessions])
    shipped_sessions = [sa for sa, _ in scored if sa.commit_count > 0]
    if shipped_sessions:
        med_shipped_prompts = _median([sa.human_prompt_count for sa in shipped_sessions])
        if med_prompts < med_shipped_prompts * 0.5:
            traits.append(f"median {med_prompts:.0f} prompts vs {med_shipped_prompts:.0f} in shipped sessions")

    trait_str = "; ".join(traits) if traits else "no distinguishing traits found"

    return [Insight(
        text=f"{len(thrashing)} thrashing chains ({total_sessions} sessions, ship goal, 0 output). Pattern: {trait_str}.",
        strength=min(1.0, len(thrashing) / 5 + 0.3),
        category="thrashing",
    )]


def _leverage_ratio(scored: list[tuple[SessionAnalysis, DurabilityReport | None]]) -> list[Insight]:
    """High-leverage vs low-leverage sessions."""
    shipped = [(sa, dr) for sa, dr in scored if sa.commit_count > 0]
    if len(shipped) < 5:
        return []

    high = [sa for sa, _ in shipped if sa.tool_call_count / max(sa.human_prompt_count, 1) >= 20]
    low = [sa for sa, _ in shipped if sa.tool_call_count / max(sa.human_prompt_count, 1) < 20]

    if len(high) < 3 or len(low) < 3:
        return []

    med_high = _median([sa.commit_count for sa in high])
    med_low = _median([sa.commit_count for sa in low])

    if med_high <= med_low or med_low == 0:
        return []

    ratio = med_high / max(med_low, 1)
    if ratio < 1.5:
        return []

    return [Insight(
        text=f"High-leverage sessions (≥1:20 tool:prompt ratio): {med_high:.0f} median commits vs {med_low:.0f} for lower-leverage. Each prompt kicks off more autonomous work.",
        strength=min(1.0, ratio / 4),
        category="leverage",
    )]


def _session_duration(scored: list[tuple[SessionAnalysis, DurabilityReport | None]]) -> list[Insight]:
    """Is there a productive session length sweet spot?"""
    shipped = [sa for sa, _ in scored if sa.commit_count > 0 and sa.active_min > 0]
    if len(shipped) < 10:
        return []

    # Bucket into short (<20min), medium (20-60), long (>60)
    short = [sa for sa in shipped if sa.active_min < 20]
    medium = [sa for sa in shipped if 20 <= sa.active_min <= 60]
    long = [sa for sa in shipped if sa.active_min > 60]

    if len(medium) < 3:
        return []

    med_commits = {
        "short (<20 min)": (_median([sa.commit_count for sa in short]), len(short)) if short else (0, 0),
        "medium (20-60 min)": (_median([sa.commit_count for sa in medium]), len(medium)),
        "long (>60 min)": (_median([sa.commit_count for sa in long]), len(long)) if long else (0, 0),
    }

    # Find the sweet spot
    best_bucket = max(med_commits.items(), key=lambda x: x[1][0])
    if best_bucket[1][1] < 3:
        return []

    parts = [f"{name}: {med:.0f} median commits (n={n})" for name, (med, n) in med_commits.items() if n >= 2]
    if len(parts) < 2:
        return []

    return [Insight(
        text=f"Session length sweet spot — {'. '.join(parts)}.",
        strength=0.4,
        category="duration",
    )]


def _review_efficiency(scored: list[tuple[SessionAnalysis, DurabilityReport | None]]) -> list[Insight]:
    """How efficient are review sessions?"""
    reviews = [sa for sa, _ in scored if sa.session_goal == "review"]
    if len(reviews) < 3:
        return []

    med_prompts = _median([sa.human_prompt_count for sa in reviews])
    med_active = _median([sa.active_min for sa in reviews])
    with_edits = sum(1 for sa in reviews if sa.edit_count > 0)

    return [Insight(
        text=f"Review sessions: {med_prompts:.0f} median prompts, {med_active:.0f} min active. {with_edits} of {len(reviews)} led to edits.",
        strength=0.35,
        category="review",
    )]


def _plan_execution(scored: list[tuple[SessionAnalysis, DurabilityReport | None]], chains: list[SessionChain]) -> list[Insight]:
    """Do sessions starting with plans produce output?"""
    plan_sessions = [sa for sa, _ in scored if sa.session_goal == "plan"]
    if len(plan_sessions) < 3:
        return []

    # Check plan_execute chains
    plan_chains = [c for c in chains if c.pattern == "plan_execute"]
    if plan_chains:
        with_commits = sum(1 for c in plan_chains if c.has_commits)
        return [Insight(
            text=f"{with_commits} of {len(plan_chains)} plan → execute chains produced commits. {len(plan_sessions)} total planning sessions.",
            strength=0.5,
            category="plan",
        )]

    return []


def _warm_start(chains: list[SessionChain]) -> list[Insight]:
    """Do shipped sessions follow prior exploration?"""
    multi = [c for c in chains if len(c.sessions) > 1 and c.has_commits]
    if len(multi) < 3:
        return []

    started_with_explore = 0
    for c in multi:
        if c.sessions[0].session_shape in ("explore_only", "debug_investigate"):
            started_with_explore += 1

    if started_with_explore < 2:
        return []

    pct = started_with_explore * 100 // len(multi)
    if pct < 40:
        return []

    return [Insight(
        text=f"{started_with_explore} of {len(multi)} multi-session chains that shipped started with exploration. You rarely ship from a cold start.",
        strength=min(1.0, pct / 80),
        category="warm_start",
    )]


def _goal_contrast(scored: list[tuple[SessionAnalysis, DurabilityReport | None]]) -> list[Insight]:
    """Surface interesting contrasts between goals."""
    goal_groups: dict[str, list[SessionAnalysis]] = {}
    for sa, _ in scored:
        goal_groups.setdefault(sa.session_goal, []).append(sa)

    # Need at least 2 goals with enough sessions
    displayable = {g: slist for g, slist in goal_groups.items() if len(slist) >= 5}
    if len(displayable) < 2:
        return []

    # Find the most interesting contrast
    insights = []

    if "ship" in displayable and "investigate" in displayable:
        ship_active = _median([sa.active_min for sa in displayable["ship"]])
        inv_active = _median([sa.active_min for sa in displayable["investigate"]])
        if ship_active > inv_active * 2:
            insights.append(Insight(
                text=f"Ship sessions run {ship_active:.0f} min median vs {inv_active:.0f} min for investigations. Building takes longer than diagnosing.",
                strength=0.35,
                category="goal_contrast",
            ))

    return insights
