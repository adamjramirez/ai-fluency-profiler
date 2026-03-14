"""Integration test: parse a real session and validate structure.

Auto-discovers any Claude Code session file. If the known reference session
(711ec74a) exists, validates against its expected values.
"""

from pathlib import Path

import pytest

from fluency.parser import parse_session


def _find_any_cc_session() -> Path | None:
    """Find any Claude Code .jsonl session file."""
    cc_base = Path.home() / ".claude" / "projects"
    if not cc_base.exists():
        return None
    for proj_dir in sorted(cc_base.iterdir()):
        if not proj_dir.is_dir():
            continue
        for f in sorted(proj_dir.glob("*.jsonl")):
            if not f.name.startswith("agent-") and f.stat().st_size > 1000:
                return f
    return None


# Known reference session (may or may not exist on this machine)
_REFERENCE_ID = "711ec74a"
_REFERENCE_PATH = None
_cc_base = Path.home() / ".claude" / "projects"
if _cc_base.exists():
    for _proj in _cc_base.iterdir():
        _candidate = _proj / f"{_REFERENCE_ID}-ce9c-4c21-99c3-f42c54e77f62.jsonl"
        if _candidate.exists():
            _REFERENCE_PATH = _candidate
            break

REAL_SESSION = _REFERENCE_PATH or _find_any_cc_session()
IS_REFERENCE = REAL_SESSION is not None and _REFERENCE_ID in str(REAL_SESSION)


@pytest.mark.skipif(REAL_SESSION is None, reason="No session files available")
class TestRealSession:
    """Validate parser against a real session file.

    If the reference session (711ec74a) is available, validates against known values.
    Otherwise validates structural correctness on any available session.
    """

    @pytest.fixture(scope="class")
    def result(self):
        return parse_session(REAL_SESSION)

    def test_source(self, result):
        assert result.source in ("claude_code", "pi")

    def test_session_id(self, result):
        assert len(result.id) >= 6

    def test_has_prompts(self, result):
        assert result.human_prompt_count >= 1

    def test_has_tool_calls(self, result):
        assert result.tool_call_count >= 1

    def test_has_timing(self, result):
        assert result.active_min > 0

    def test_has_segments(self, result):
        assert len(result.segments) >= 1

    def test_session_shape_valid(self, result):
        valid_shapes = {
            "plan_handoff", "review_iterate", "review_only",
            "debug_investigate", "error_fix", "explore_build",
            "explore_only", "abandoned",
        }
        assert result.session_shape in valid_shapes

    def test_steering_events_match_prompts(self, result):
        assert len(result.steering_events) == result.human_prompt_count

    # --- Reference session assertions (only when 711ec74a is available) ---

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_prompt_count(self, result):
        assert result.human_prompt_count == 5

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_tool_calls(self, result):
        assert 100 <= result.tool_call_count <= 150

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_active_time(self, result):
        assert 15 <= result.active_min <= 60

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_wall_clock(self, result):
        assert 150 <= result.wall_clock_min <= 300

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_last_test_pass(self, result):
        assert result.last_test_result == "PASS"

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_test_arc(self, result):
        assert len(result.test_arc) >= 3

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_shape(self, result):
        assert result.session_shape == "plan_handoff"

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_has_commits(self, result):
        assert result.commit_count >= 1

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_has_edits(self, result):
        assert result.edit_count >= 5

    @pytest.mark.skipif(not IS_REFERENCE, reason="Reference session not available")
    def test_ref_first_intent(self, result):
        assert "plan" in result.first_intent.lower() or "implement" in result.first_intent.lower()
