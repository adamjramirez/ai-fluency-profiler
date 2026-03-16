"""Tests for report formatting — bias resistance and goal-awareness."""

from fluency.models import SessionAnalysis, Segment
from fluency.git_tracer import DurabilityReport
from fluency.sequence import SessionChain
from fluency.report import format_session_report, format_profile_report, generate_narrative

from datetime import datetime, timezone, timedelta


def _make_session(
    id: str = "test-session",
    source: str = "pi",
    session_goal: str = "unknown",
    session_shape: str = "explore_only",
    commit_count: int = 0,
    edit_count: int = 0,
    human_prompt_count: int = 5,
    tool_call_count: int = 50,
    active_min: float = 30.0,
    lines_changed: int = 0,
    test_arc: list[str] | None = None,
    has_subagents: bool = False,
) -> SessionAnalysis:
    base = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
    sa = SessionAnalysis(id=id, source=source)
    sa.session_goal = session_goal
    sa.session_shape = session_shape
    sa.commit_count = commit_count
    sa.edit_count = edit_count
    sa.human_prompt_count = human_prompt_count
    sa.tool_call_count = tool_call_count
    sa.active_min = active_min
    sa.lines_changed = lines_changed
    sa.test_arc = test_arc or []
    sa.has_subagents = has_subagents
    sa.first_intent = "do something"
    sa.segments = [Segment(start=base, end=base + timedelta(minutes=active_min), duration_sec=active_min * 60)]
    return sa


class TestNarrative:
    def test_investigate_zero_commits_explained(self):
        """Narrative for investigate session should explain 0 commits is expected."""
        sa = _make_session(session_goal="investigate", commit_count=0)
        text = generate_narrative(sa)
        assert "typical for investigate sessions" in text.lower() or "typical for investigate" in text.lower()

    def test_review_zero_commits_explained(self):
        """Narrative for review session should explain 0 commits is expected."""
        sa = _make_session(session_goal="review", commit_count=0)
        text = generate_narrative(sa)
        assert "typical for review sessions" in text.lower() or "typical for review" in text.lower()

    def test_ship_zero_commits_no_excuse(self):
        """Narrative for ship session with 0 commits should not say 'typical'."""
        sa = _make_session(session_goal="ship", commit_count=0)
        text = generate_narrative(sa)
        assert "typical" not in text.lower()

    def test_no_judgment_words(self):
        """Narrative should not contain judgment words."""
        judgment = ["good", "bad", "should have", "failed", "poor", "excellent", "great"]
        for goal in ["ship", "investigate", "review", "explore", "plan", "learn"]:
            sa = _make_session(session_goal=goal, commit_count=0)
            text = generate_narrative(sa).lower()
            for word in judgment:
                assert word not in text, f"Found '{word}' in {goal} narrative: {text}"

    def test_narrative_includes_duration(self):
        sa = _make_session(active_min=45.0)
        text = generate_narrative(sa)
        assert "45" in text

    def test_narrative_includes_prompts(self):
        sa = _make_session(human_prompt_count=8)
        text = generate_narrative(sa)
        assert "8 prompts" in text


class TestProfileReport:
    def test_no_universal_ship_rate(self):
        """Profile report should not show a 'ship rate' column."""
        sessions = [
            (_make_session(id=f"s{i}", session_goal="review", session_shape="review_only"), None)
            for i in range(5)
        ] + [
            (_make_session(id=f"b{i}", session_goal="ship", session_shape="explore_build", commit_count=3), None)
            for i in range(5)
        ]
        report = format_profile_report(sessions, project_name="test")
        assert "ship rate" not in report.lower()

    def test_review_not_penalized(self):
        """Review sessions with 0 commits should not appear as failures.
        The report should not show 'ship rate' for review sessions."""
        sessions = [
            (_make_session(id=f"r{i}", session_goal="review", session_shape="review_only"), None)
            for i in range(5)
        ] + [
            (_make_session(id=f"b{i}", session_goal="ship", session_shape="explore_build", commit_count=5), None)
            for i in range(5)
        ]
        report = format_profile_report(sessions, project_name="test")
        # Should not have ship rate anywhere
        assert "ship rate" not in report.lower()
        # Review line should not say "0% shipped" or similar failure framing
        review_line = [l for l in report.split("\n") if "review_only" in l][0]
        assert "0% ship" not in review_line.lower()

    def test_durability_scope_note(self):
        """Profile report should include durability scope bias note."""
        dr = DurabilityReport()
        dr.total_lines_added = 1000
        dr.total_lines_surviving = 800
        dr.raw_survival_pct = 0.8
        dr.adjusted_survival_pct = 0.85
        sessions = [
            (_make_session(id="s1", session_goal="ship", commit_count=5), dr),
        ]
        report = format_profile_report(sessions, project_name="test")
        assert "well-scoped tasks" in report.lower() or "bugfix" in report.lower()

    def test_note_footer_present(self):
        """Profile report should include the NOTE footer."""
        sessions = [
            (_make_session(id=f"s{i}", session_goal="ship"), None)
            for i in range(3)
        ]
        report = format_profile_report(sessions, project_name="test")
        assert "NOTE:" in report

    def test_goal_in_evolution(self):
        """Evolution table should show goal column."""
        sessions = [
            (_make_session(id=f"s{i}", session_goal="ship"), None)
            for i in range(5)
        ]
        report = format_profile_report(sessions, project_name="test")
        assert "Goal" in report

    def test_by_goal_section(self):
        """Report should have BY GOAL section instead of shipped vs zero-commit."""
        sessions = [
            (_make_session(id=f"s{i}", session_goal="ship", commit_count=3), None)
            for i in range(5)
        ] + [
            (_make_session(id=f"r{i}", session_goal="investigate"), None)
            for i in range(5)
        ]
        report = format_profile_report(sessions, project_name="test")
        assert "BY GOAL" in report
        assert "shipped vs zero-commit" not in report.lower()

    def test_shapes_show_goal_distribution(self):
        """Session shapes should show goal distribution within each shape."""
        sessions = [
            (_make_session(id="s1", session_goal="ship", session_shape="explore_build", commit_count=5), None),
            (_make_session(id="s2", session_goal="explore", session_shape="explore_build", commit_count=2), None),
            (_make_session(id="s3", session_goal="review", session_shape="review_only"), None),
        ]
        report = format_profile_report(sessions, project_name="test")
        assert "goal:" in report.lower()


class TestSessionReport:
    def test_goal_in_header(self):
        """Session report should show goal in header."""
        sa = _make_session(session_goal="investigate")
        report = format_session_report(sa)
        assert "Goal: investigate" in report

    def test_narrative_in_report(self):
        """Session report should include SUMMARY with narrative."""
        sa = _make_session(session_goal="review")
        report = format_session_report(sa)
        assert "SUMMARY:" in report
