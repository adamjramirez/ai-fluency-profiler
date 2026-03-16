"""Session sequence analysis — chain detection and pattern classification."""

from collections import Counter
from dataclasses import dataclass, field

from fluency.models import SessionAnalysis


@dataclass
class SessionChain:
    sessions: list[SessionAnalysis] = field(default_factory=list)
    shape_sequence: list[str] = field(default_factory=list)

    # Aggregates
    total_commits: int = 0
    total_edits: int = 0
    total_lines_changed: int = 0
    total_tool_calls: int = 0
    total_human_prompts: int = 0
    total_active_min: float = 0.0

    # Pattern
    pattern: str = "standalone"
    has_commits: bool = False
    dominant_shape: str = ""
    dominant_goal: str = ""
    topic: str = ""


# Chain detection thresholds
_TIME_GAP_SEC = 60 * 60       # 60 min — always chain
_INTENT_GAP_SEC = 180 * 60    # 180 min — chain if same intent
_INTENT_PREFIX_LEN = 30       # chars to compare for intent overlap


def detect_chains(
    sessions: list[SessionAnalysis],
    file_links: dict[str, set[str]] | None = None,
) -> list[SessionChain]:
    """Group sessions into chains by time proximity and intent overlap.
    
    Rules (any match continues the chain):
    1. Start-to-start gap < 60 min
    2. Same intent prefix (first 30 chars) AND gap < 180 min
    3. File overlap > 20% AND gap < 180 min (if file_links provided)
    """
    if not sessions:
        return []

    # Sort by start time
    def _start(sa: SessionAnalysis):
        if sa.segments:
            return sa.segments[0].start
        return None

    sortable = [(sa, _start(sa)) for sa in sessions if _start(sa) is not None]
    sortable.sort(key=lambda x: x[1])

    if not sortable:
        return [_build_chain(sessions)]

    chains: list[list[SessionAnalysis]] = [[sortable[0][0]]]

    for i in range(1, len(sortable)):
        sa, start = sortable[i]
        prev_sa, prev_start = sortable[i - 1]

        gap_sec = (start - prev_start).total_seconds()

        # Rule 1: time proximity
        if gap_sec < _TIME_GAP_SEC:
            chains[-1].append(sa)
            continue

        # Rule 2: intent overlap within 180 min
        if gap_sec < _INTENT_GAP_SEC:
            intent_a = prev_sa.first_intent.lower()[:_INTENT_PREFIX_LEN]
            intent_b = sa.first_intent.lower()[:_INTENT_PREFIX_LEN]
            if intent_a and intent_b and intent_a == intent_b:
                chains[-1].append(sa)
                continue

        # Rule 3: file overlap within 180 min
        if file_links and gap_sec < _INTENT_GAP_SEC:
            files_a = file_links.get(prev_sa.id, set())
            files_b = file_links.get(sa.id, set())
            if files_a and files_b:
                union = files_a | files_b
                overlap = files_a & files_b
                if len(overlap) / len(union) > 0.20:
                    chains[-1].append(sa)
                    continue

        # No match — start new chain
        chains.append([sa])

    return [_build_chain(group) for group in chains]


def _build_chain(sessions: list[SessionAnalysis]) -> SessionChain:
    """Build a SessionChain from a group of sessions."""
    chain = SessionChain(sessions=sessions)
    chain.shape_sequence = [sa.session_shape for sa in sessions]

    chain.total_commits = sum(sa.commit_count for sa in sessions)
    chain.total_edits = sum(sa.edit_count for sa in sessions)
    chain.total_lines_changed = sum(sa.lines_changed for sa in sessions)
    chain.total_tool_calls = sum(sa.tool_call_count for sa in sessions)
    chain.total_human_prompts = sum(sa.human_prompt_count for sa in sessions)
    chain.total_active_min = sum(sa.active_min for sa in sessions)

    chain.has_commits = chain.total_commits > 0

    # Dominant shape
    shape_counts = Counter(chain.shape_sequence)
    most_common_shape, most_common_count = shape_counts.most_common(1)[0]
    if most_common_count > len(sessions) / 2:
        chain.dominant_shape = most_common_shape

    # Dominant goal
    goal_counts = Counter(sa.session_goal for sa in sessions)
    most_common_goal, most_common_goal_count = goal_counts.most_common(1)[0]
    if most_common_goal_count > len(sessions) / 2:
        chain.dominant_goal = most_common_goal

    # Topic from most common intent prefix
    intent_prefixes = Counter(
        sa.first_intent[:40].lower().replace("\n", " ")
        for sa in sessions
    )
    chain.topic = intent_prefixes.most_common(1)[0][0] if intent_prefixes else ""

    # Classify pattern
    chain.pattern = classify_chain_pattern(chain)

    return chain


def classify_chain_pattern(chain: SessionChain) -> str:
    """Classify chain pattern by dominant shape + goal + outcome.

    Key principle: thrashing only applies when the goal was to ship.
    An investigate/review/explore chain with 0 commits is normal work.
    """
    n = len(chain.sessions)

    if n == 1:
        return "standalone"

    review_count = sum(
        1 for s in chain.shape_sequence
        if s in ("review_only", "review_iterate")
    )
    explore_count = sum(
        1 for s in chain.shape_sequence
        if s in ("explore_only", "explore_build")
    )

    # Non-ship goals with no commits are working as intended
    non_ship_goals = {"investigate", "review", "explore", "plan", "learn"}
    if chain.dominant_goal in non_ship_goals and not chain.has_commits:
        if chain.dominant_goal == "investigate":
            return "investigation"
        if chain.dominant_goal == "review":
            return "review_fix_loop" if chain.has_commits else "review_cycle"
        if chain.dominant_goal == "explore":
            return "exploration"
        if chain.dominant_goal == "plan":
            return "planning"
        return "research"

    # plan_execute: dominant plan_handoff + has commits
    if chain.dominant_shape == "plan_handoff" and chain.has_commits:
        return "plan_execute"

    # review_fix_loop / review_cycle: ≥2 review shapes
    if review_count >= 2:
        return "review_fix_loop" if chain.has_commits else "review_cycle"

    # explore_converge: has explore shapes + has commits
    if explore_count >= 2 and chain.has_commits:
        return "explore_converge"

    # thrashing: ≥3 sessions, goal was ship (or unknown), no commits
    if n >= 3 and not chain.has_commits:
        goal_is_ship = chain.dominant_goal in ("ship", "unknown", "")
        if goal_is_ship:
            return "thrashing"
        # Non-ship goal chains without commits are normal
        return "research"

    # mixed_sprint: ≥4 sessions, no dominant, has commits
    if n >= 4 and not chain.dominant_shape and chain.has_commits:
        return "mixed_sprint"

    # Small chains with commits
    if chain.has_commits:
        return "mixed_sprint" if n >= 3 else "plan_execute"

    # Small unshipped chains with ship/unknown goal
    if n >= 2 and not chain.has_commits:
        goal_is_ship = chain.dominant_goal in ("ship", "unknown", "")
        return "thrashing" if goal_is_ship else "research"

    return "standalone"
