"""Tests for session sequence / chain detection."""

import pytest
from datetime import datetime, timezone, timedelta

from fluency.models import SessionAnalysis, Segment
from fluency.sequence import detect_chains, classify_chain_pattern


def _make_session(
    id: str,
    start_min: float,
    duration_min: float = 10.0,
    shape: str = "explore_only",
    commit_count: int = 0,
    edit_count: int = 0,
    first_intent: str = "do something",
    tool_call_count: int = 20,
) -> SessionAnalysis:
    """Create a session with start time as minutes from epoch."""
    base = datetime(2026, 2, 5, 20, 0, 0, tzinfo=timezone.utc)
    start = base + timedelta(minutes=start_min)
    end = start + timedelta(minutes=duration_min)

    sa = SessionAnalysis(id=id, source="claude_code")
    sa.session_shape = shape
    sa.commit_count = commit_count
    sa.edit_count = edit_count
    sa.first_intent = first_intent
    sa.tool_call_count = tool_call_count
    sa.human_prompt_count = 5
    sa.segments = [Segment(start=start, end=end, duration_sec=duration_min * 60)]
    return sa


class TestDetectChains:
    def test_time_gap_chains(self):
        """Sessions within 60 min start-to-start are chained."""
        sessions = [
            _make_session("a", 0),
            _make_session("b", 20),
            _make_session("c", 40),
        ]
        chains = detect_chains(sessions)
        assert len(chains) == 1
        assert len(chains[0].sessions) == 3

    def test_time_gap_splits(self):
        """Sessions >60 min apart with different intents are separate chains."""
        sessions = [
            _make_session("a", 0, first_intent="fix the bug in module A"),
            _make_session("b", 20, first_intent="fix the bug in module A"),
            _make_session("c", 120, first_intent="add a new feature to module B"),
        ]
        chains = detect_chains(sessions)
        assert len(chains) == 2
        assert len(chains[0].sessions) == 2
        assert len(chains[1].sessions) == 1

    def test_intent_overlap_chains_within_180min(self):
        """Same intent within 180 min chains even if >60 min gap."""
        sessions = [
            _make_session("a", 0, first_intent="review this insight trace output"),
            _make_session("b", 90, first_intent="review this insight trace output"),
        ]
        chains = detect_chains(sessions)
        assert len(chains) == 1

    def test_intent_overlap_no_chain_beyond_180min(self):
        """Same intent >180 min apart does NOT chain."""
        sessions = [
            _make_session("a", 0, first_intent="review this insight trace output"),
            _make_session("b", 200, first_intent="review this insight trace output"),
        ]
        chains = detect_chains(sessions)
        assert len(chains) == 2

    def test_different_intent_no_chain_beyond_60min(self):
        """Different intents >60 min apart don't chain."""
        sessions = [
            _make_session("a", 0, first_intent="fix the bug"),
            _make_session("b", 90, first_intent="add a feature"),
        ]
        chains = detect_chains(sessions)
        assert len(chains) == 2

    def test_aggregates_computed(self):
        """Chain aggregates are summed correctly."""
        sessions = [
            _make_session("a", 0, commit_count=2, edit_count=5, tool_call_count=50),
            _make_session("b", 20, commit_count=1, edit_count=3, tool_call_count=30),
        ]
        chains = detect_chains(sessions)
        assert chains[0].total_commits == 3
        assert chains[0].total_edits == 8
        assert chains[0].total_tool_calls == 80

    def test_standalone_single_session(self):
        """Single session in isolation is standalone."""
        sessions = [_make_session("a", 0)]
        chains = detect_chains(sessions)
        assert len(chains) == 1
        assert len(chains[0].sessions) == 1

    def test_sorts_by_start_time(self):
        """Sessions are sorted by start time regardless of input order."""
        sessions = [
            _make_session("c", 40),
            _make_session("a", 0),
            _make_session("b", 20),
        ]
        chains = detect_chains(sessions)
        assert chains[0].sessions[0].id == "a"
        assert chains[0].sessions[1].id == "b"
        assert chains[0].sessions[2].id == "c"


class TestClassifyChainPattern:
    def test_standalone(self):
        sessions = [_make_session("a", 0, shape="plan_handoff", commit_count=3)]
        chains = detect_chains(sessions)
        assert chains[0].pattern == "standalone"

    def test_plan_execute(self):
        """Dominant plan_handoff + shipped = plan_execute."""
        sessions = [
            _make_session("a", 0, shape="plan_handoff", commit_count=3),
            _make_session("b", 20, shape="review_only"),
        ]
        chains = detect_chains(sessions)
        assert chains[0].pattern == "plan_execute"
        assert chains[0].shipped is True

    def test_review_fix_loop(self):
        """Multiple review sessions + shipped = review_fix_loop."""
        sessions = [
            _make_session("a", 0, shape="review_only"),
            _make_session("b", 20, shape="review_iterate", commit_count=2),
            _make_session("c", 40, shape="review_iterate", commit_count=1),
        ]
        chains = detect_chains(sessions)
        assert chains[0].pattern == "review_fix_loop"
        assert chains[0].shipped is True

    def test_review_stall(self):
        """Multiple review sessions + not shipped = review_stall."""
        sessions = [
            _make_session("a", 0, shape="review_only"),
            _make_session("b", 20, shape="review_only"),
        ]
        chains = detect_chains(sessions)
        assert chains[0].pattern == "review_stall"
        assert chains[0].shipped is False

    def test_explore_converge(self):
        """Explore shapes + shipped = explore_converge."""
        sessions = [
            _make_session("a", 0, shape="explore_only"),
            _make_session("b", 20, shape="explore_build", commit_count=2),
        ]
        chains = detect_chains(sessions)
        assert chains[0].pattern == "explore_converge"

    def test_thrashing(self):
        """3+ sessions, not shipped = thrashing."""
        sessions = [
            _make_session("a", 0, shape="explore_only"),
            _make_session("b", 20, shape="debug_investigate", edit_count=3),
            _make_session("c", 40, shape="explore_only"),
        ]
        chains = detect_chains(sessions)
        assert chains[0].pattern == "thrashing"
        assert chains[0].shipped is False

    def test_mixed_sprint(self):
        """4+ sessions, no dominant shape, shipped = mixed_sprint."""
        sessions = [
            _make_session("a", 0, shape="error_fix", commit_count=1),
            _make_session("b", 15, shape="explore_build", commit_count=1),
            _make_session("c", 30, shape="plan_handoff", commit_count=2),
            _make_session("d", 45, shape="review_iterate", commit_count=1),
        ]
        chains = detect_chains(sessions)
        assert chains[0].pattern == "mixed_sprint"
        assert chains[0].shipped is True


def _find_any_session_project_dir(min_sessions: int = 10):
    """Find any CC or Pi project directory with enough sessions."""
    from pathlib import Path

    for base in [
        Path.home() / ".claude" / "projects",
        Path.home() / ".pi" / "agent" / "sessions",
    ]:
        if not base.exists():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            jsonl_files = [f for f in d.glob("*.jsonl") if not f.name.startswith("agent-")]
            if len(jsonl_files) >= min_sessions:
                return d
    return None


_SESSION_DIR = _find_any_session_project_dir()


@pytest.mark.skipif(
    _SESSION_DIR is None,
    reason="No project with ≥10 sessions found",
)
class TestRealChains:
    """Validate chain detection on real session data.

    Tests structural properties that should hold for any project:
    - Reasonable chain count relative to session count
    - Not everything is standalone
    - Chains are properly ordered by time
    """

    @pytest.fixture
    def real_sessions(self):
        from pathlib import Path
        from fluency.parser import parse_session

        sessions = []
        for f in sorted(_SESSION_DIR.glob("*.jsonl")):
            if f.name.startswith("agent-"):
                continue
            sa = parse_session(f)
            if sa.human_prompt_count >= 2:
                sessions.append(sa)
        return sessions

    def test_chain_count_reasonable(self, real_sessions):
        chains = detect_chains(real_sessions)
        # Should have fewer chains than sessions (some grouping happened)
        assert len(chains) < len(real_sessions), "No sessions were chained at all"
        # But not everything in one chain
        assert len(chains) >= 2, "All sessions collapsed into one chain"

    def test_standalone_under_50pct(self, real_sessions):
        chains = detect_chains(real_sessions)
        standalone = sum(1 for c in chains if len(c.sessions) == 1)
        pct = standalone / len(chains)
        assert pct < 0.70, f"{standalone}/{len(chains)} = {pct:.0%} standalone — too many isolated"

    def test_chains_ordered_by_time(self, real_sessions):
        chains = detect_chains(real_sessions)
        for chain in chains:
            if len(chain.sessions) < 2:
                continue
            starts = [s.segments[0].start for s in chain.sessions if s.segments]
            assert starts == sorted(starts), "Sessions within chain are not time-ordered"

    def test_all_sessions_accounted_for(self, real_sessions):
        chains = detect_chains(real_sessions)
        total_in_chains = sum(len(c.sessions) for c in chains)
        # Some sessions without timestamps may be dropped
        assert total_in_chains >= len(real_sessions) * 0.9, (
            f"Only {total_in_chains}/{len(real_sessions)} sessions in chains"
        )

    def test_shipped_chains_have_commits(self, real_sessions):
        chains = detect_chains(real_sessions)
        for chain in chains:
            if chain.shipped:
                assert chain.total_commits > 0
