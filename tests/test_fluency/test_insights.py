"""Tests for insight detection engine."""

from fluency.models import SessionAnalysis, Segment, SteeringEvent
from fluency.insights import detect_insights, Insight
from fluency.sequence import detect_chains

from datetime import datetime, timezone, timedelta


def _make_session(
    id: str = "s",
    session_goal: str = "ship",
    session_shape: str = "explore_build",
    commit_count: int = 0,
    test_arc: list[str] | None = None,
    has_subagents: bool = False,
    human_prompt_count: int = 10,
    tool_call_count: int = 100,
    active_min: float = 30.0,
    start_min: float = 0.0,
) -> SessionAnalysis:
    base = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
    start = base + timedelta(minutes=start_min)
    sa = SessionAnalysis(id=id, source="pi")
    sa.session_goal = session_goal
    sa.session_shape = session_shape
    sa.commit_count = commit_count
    sa.test_arc = test_arc or []
    sa.has_subagents = has_subagents
    sa.human_prompt_count = human_prompt_count
    sa.tool_call_count = tool_call_count
    sa.active_min = active_min
    sa.first_intent = "build something"
    sa.segments = [Segment(start=start, end=start + timedelta(minutes=active_min), duration_sec=active_min * 60)]
    return sa


class TestInsightDetection:
    def test_no_insights_with_few_sessions(self):
        """Need at least 5 sessions to generate insights."""
        sessions = [(_make_session(id=f"s{i}"), None) for i in range(3)]
        assert detect_insights(sessions) == []

    def test_test_correlation_fires(self):
        """Should detect test usage gap between committed and uncommitted."""
        sessions = []
        # 8 shipped with tests
        for i in range(8):
            sessions.append((_make_session(
                id=f"shipped{i}", commit_count=5, test_arc=["FAIL", "PASS"],
            ), None))
        # 8 unshipped without tests
        for i in range(8):
            sessions.append((_make_session(
                id=f"zero{i}", commit_count=0, session_goal="ship",
            ), None))

        insights = detect_insights(sessions)
        texts = [i.text for i in insights]
        assert any("tests" in t.lower() for t in texts)

    def test_subagent_insight_includes_caveat(self):
        """Subagent insight should mention potential confound."""
        sessions = []
        # With subagents: high commits, longer
        for i in range(5):
            sessions.append((_make_session(
                id=f"sub{i}", commit_count=12, has_subagents=True, active_min=60,
            ), None))
        # Without: lower commits, shorter
        for i in range(8):
            sessions.append((_make_session(
                id=f"nosub{i}", commit_count=3, has_subagents=False, active_min=25,
            ), None))

        insights = detect_insights(sessions)
        sub_insights = [i for i in insights if i.category == "subagent"]
        if sub_insights:
            assert "may reflect" in sub_insights[0].text.lower() or "task size" in sub_insights[0].text.lower()

    def test_thrashing_diagnosis(self):
        """Should diagnose what's common in thrashing chains."""
        sessions = []
        # 3 thrashing chains worth of sessions — spread far apart so they form separate chains
        # Chain gap threshold is 60 min, so 500 min apart = separate chains
        for chain_idx in range(3):
            base = chain_idx * 500
            for i in range(3):
                sessions.append((_make_session(
                    id=f"thrash{chain_idx}_{i}",
                    session_goal="ship",
                    session_shape="explore_only",
                    commit_count=0,
                    human_prompt_count=2,
                    start_min=base + i * 30,
                ), None))
        # Some shipped sessions for contrast
        for i in range(5):
            sessions.append((_make_session(
                id=f"shipped{i}", commit_count=5, human_prompt_count=15,
                test_arc=["PASS"], start_min=3000 + i * 500,
            ), None))

        all_sa = [sa for sa, _ in sessions]
        chains = detect_chains(all_sa)
        insights = detect_insights(sessions, chains)
        texts = [i.text for i in insights]
        assert any("thrashing" in t.lower() for t in texts)

    def test_max_insights_respected(self):
        """Should return at most max_insights."""
        sessions = []
        for i in range(20):
            sessions.append((_make_session(
                id=f"s{i}", commit_count=5, test_arc=["PASS"],
            ), None))
        for i in range(20):
            sessions.append((_make_session(
                id=f"z{i}", commit_count=0, session_goal="ship",
            ), None))

        insights = detect_insights(sessions, max_insights=3)
        assert len(insights) <= 3

    def test_no_judgment_in_insights(self):
        """Insights should not use judgment language."""
        sessions = []
        for i in range(10):
            sessions.append((_make_session(id=f"s{i}", commit_count=5, test_arc=["PASS"]), None))
        for i in range(10):
            sessions.append((_make_session(id=f"z{i}", commit_count=0, session_goal="ship"), None))

        insights = detect_insights(sessions)
        judgment = ["good", "bad", "should", "poor", "excellent", "great", "failure", "success"]
        for ins in insights:
            text_lower = ins.text.lower()
            for word in judgment:
                assert word not in text_lower, f"Found '{word}' in insight: {ins.text}"

    def test_investigate_sessions_not_flagged(self):
        """Investigate sessions with 0 commits should not appear in negative insights."""
        sessions = []
        for i in range(10):
            sessions.append((_make_session(
                id=f"inv{i}", session_goal="investigate", commit_count=0,
                session_shape="debug_investigate",
            ), None))
        for i in range(5):
            sessions.append((_make_session(
                id=f"s{i}", commit_count=5, test_arc=["PASS"],
            ), None))

        insights = detect_insights(sessions)
        for ins in insights:
            # Should not frame investigate sessions negatively
            assert "investigate" not in ins.text.lower() or "expected" in ins.text.lower() or "typical" in ins.text.lower() or "investigate" not in ins.text.split("ship")[0].lower() if "ship" in ins.text else True
