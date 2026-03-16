"""Tests for session parser — Claude Code and Pi formats."""

import json
import tempfile
from pathlib import Path

import pytest

from fluency.parser import parse_session, classify_session_goal


# --- Fixtures: minimal JSONL sessions ---


def _write_jsonl(lines: list[dict]) -> Path:
    """Write a list of dicts as JSONL to a temp file, return path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
    for line in lines:
        f.write(json.dumps(line) + "\n")
    f.close()
    return Path(f.name)


def _cc_user(text: str, ts: str) -> dict:
    """Claude Code user message."""
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _cc_user_with_tool_result(text: str, tool_result_text: str, ts: str) -> dict:
    """Claude Code user message that contains both human text and a tool result."""
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "tool_result",
                    "content": [{"type": "text", "text": tool_result_text}],
                },
            ]
        },
    }


def _cc_user_tool_result_only(tool_result_text: str, ts: str) -> dict:
    """Claude Code user message that is ONLY a tool result (no human text)."""
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "content": [{"type": "text", "text": tool_result_text}],
                }
            ]
        },
    }


def _cc_assistant(tool_uses: list[dict], ts: str, text: str = "") -> dict:
    """Claude Code assistant message with tool uses."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tu in tool_uses:
        content.append({"type": "tool_use", **tu})
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"content": content},
    }


# --- Tests ---


class TestClaudeCodeBasicParsing:
    def test_counts_human_prompts_excluding_tool_results(self):
        """Human prompt count should exclude messages that are only tool results."""
        session = _write_jsonl([
            _cc_user("fix the bug in auth.py", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Read", "input": {"path": "auth.py"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_user_tool_result_only(
                "def login():\n    pass", "2026-01-01T10:00:06Z"
            ),
            _cc_assistant(
                [{"name": "Edit", "input": {"path": "auth.py", "oldText": "pass", "newText": "return True"}}],
                "2026-01-01T10:00:10Z",
            ),
            _cc_user("looks good, commit", "2026-01-01T10:01:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "git commit -am 'fix: auth bug'"}}],
                "2026-01-01T10:01:05Z",
            ),
        ])
        result = parse_session(session)
        assert result.human_prompt_count == 2  # "fix the bug" + "looks good, commit"
        assert result.source == "claude_code"

    def test_counts_tool_calls(self):
        session = _write_jsonl([
            _cc_user("fix auth", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [
                    {"name": "Read", "input": {"path": "auth.py"}},
                    {"name": "Edit", "input": {"path": "auth.py", "oldText": "a", "newText": "b"}},
                ],
                "2026-01-01T10:00:05Z",
            ),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:10Z",
            ),
        ])
        result = parse_session(session)
        assert result.tool_call_count == 3
        assert result.unique_tools == {"Read", "Edit", "Bash"}

    def test_human_chars_excludes_tool_results(self):
        session = _write_jsonl([
            _cc_user_with_tool_result(
                "fix it", "lots of code here that should not count", "2026-01-01T10:00:00Z"
            ),
            _cc_assistant([], "2026-01-01T10:00:05Z"),
        ])
        result = parse_session(session)
        assert result.human_chars == len("fix it")

    def test_excludes_local_command_from_human_prompts(self):
        session = _write_jsonl([
            _cc_user("real prompt", "2026-01-01T10:00:00Z"),
            _cc_assistant([], "2026-01-01T10:00:05Z"),
            _cc_user("<local-command>something</local-command>", "2026-01-01T10:00:10Z"),
            _cc_assistant([], "2026-01-01T10:00:15Z"),
        ])
        result = parse_session(session)
        assert result.human_prompt_count == 1

    def test_excludes_command_name_from_human_prompts(self):
        session = _write_jsonl([
            _cc_user("real prompt", "2026-01-01T10:00:00Z"),
            _cc_assistant([], "2026-01-01T10:00:05Z"),
            _cc_user("<command-name>/clear</command-name>", "2026-01-01T10:00:10Z"),
            _cc_assistant([], "2026-01-01T10:00:15Z"),
        ])
        result = parse_session(session)
        assert result.human_prompt_count == 1

    def test_counts_interruptions(self):
        session = _write_jsonl([
            _cc_user("do something big", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Read", "input": {"path": "a.py"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_user("[Request interrupted by user]", "2026-01-01T10:00:10Z"),
            _cc_assistant([], "2026-01-01T10:00:15Z"),
        ])
        result = parse_session(session)
        assert result.interruption_count == 1
        assert result.human_prompt_count == 1  # interruption excluded


class TestClaudeCodeEditsAndCommits:
    def test_counts_edits_and_lines_changed(self):
        session = _write_jsonl([
            _cc_user("fix it", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [
                    {
                        "name": "Edit",
                        "input": {
                            "path": "a.py",
                            "oldText": "line1\nline2\nline3",
                            "newText": "new1\nnew2",
                        },
                    }
                ],
                "2026-01-01T10:00:05Z",
            ),
        ])
        result = parse_session(session)
        assert result.edit_count == 1
        # lines_changed = lines in oldText + lines in newText = 3 + 2 = 5
        assert result.lines_changed == 5

    def test_counts_commits(self):
        session = _write_jsonl([
            _cc_user("commit", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "git commit -am 'fix: thing'"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "git commit -m 'feat: other'"}}],
                "2026-01-01T10:00:10Z",
            ),
        ])
        result = parse_session(session)
        assert result.commit_count == 2


class TestClaudeCodeTooling:
    def test_detects_subagents(self):
        session = _write_jsonl([
            _cc_user("do it", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Task", "input": {"description": "review code"}}],
                "2026-01-01T10:00:05Z",
            ),
        ])
        result = parse_session(session)
        assert result.has_subagents is True

    def test_detects_skills(self):
        session = _write_jsonl([
            _cc_user("do it", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Skill", "input": {"skill": "tdd"}}],
                "2026-01-01T10:00:05Z",
            ),
        ])
        result = parse_session(session)
        assert result.has_skills is True
        assert result.skills_invoked == ["tdd"]

    def test_detects_slash_commands(self):
        session = _write_jsonl([
            _cc_user("<command-name>/review</command-name>\nreview the code", "2026-01-01T10:00:00Z"),
            _cc_assistant([], "2026-01-01T10:00:05Z"),
        ])
        result = parse_session(session)
        assert "review" in result.slash_commands


class TestClaudeCodeTestDetection:
    def test_detects_passing_test(self):
        test_output = "Ran 42 tests in 1.234s\n\nOK"
        session = _write_jsonl([
            _cc_user("run tests", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "python manage.py test"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_user_tool_result_only(test_output, "2026-01-01T10:00:10Z"),
        ])
        result = parse_session(session)
        assert result.test_arc == ["PASS"]
        assert result.last_test_result == "PASS"
        assert result.first_test_result == "PASS"

    def test_detects_failing_test(self):
        test_output = "Ran 42 tests in 1.234s\n\nFAILED (failures=3)"
        session = _write_jsonl([
            _cc_user("run tests", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_user_tool_result_only(test_output, "2026-01-01T10:00:10Z"),
        ])
        result = parse_session(session)
        assert result.test_arc == ["FAIL"]

    def test_rejects_ci_output(self):
        """CI output with timestamps should NOT be counted as local test results."""
        ci_output = "2026-01-01T10:00:00Z\tRan 42 tests in 1.234s\n\nOK"
        session = _write_jsonl([
            _cc_user("check ci", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "gh run view"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_user_tool_result_only(ci_output, "2026-01-01T10:00:10Z"),
        ])
        result = parse_session(session)
        assert result.test_arc == []

    def test_tdd_arc_fail_then_pass(self):
        """TDD RED→GREEN should show FAIL then PASS."""
        session = _write_jsonl([
            _cc_user("use /tdd", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_user_tool_result_only(
                "Ran 1 test in 0.1s\n\nFAILED (failures=1)", "2026-01-01T10:00:10Z"
            ),
            _cc_assistant(
                [{"name": "Edit", "input": {"path": "a.py", "oldText": "a", "newText": "b"}}],
                "2026-01-01T10:00:15Z",
            ),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:20Z",
            ),
            _cc_user_tool_result_only(
                "Ran 1 test in 0.1s\n\nOK", "2026-01-01T10:00:25Z"
            ),
        ])
        result = parse_session(session)
        assert result.test_arc == ["FAIL", "PASS"]
        assert result.first_test_result == "FAIL"
        assert result.last_test_result == "PASS"


class TestClaudeCodeErrors:
    def test_counts_tool_errors(self):
        session = _write_jsonl([
            _cc_user("do it", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:05Z",
            ),
            {
                "type": "user",
                "timestamp": "2026-01-01T10:00:10Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": [{"type": "text", "text": "command failed"}],
                        }
                    ]
                },
            },
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:15Z",
            ),
            _cc_user_tool_result_only(
                "Ran 1 test in 0.1s\n\nOK", "2026-01-01T10:00:20Z"
            ),
        ])
        result = parse_session(session)
        assert result.tool_error_count == 1
        assert result.unresolved_error_count == 0  # resolved by passing test

    def test_unresolved_error(self):
        session = _write_jsonl([
            _cc_user("do it", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:05Z",
            ),
            {
                "type": "user",
                "timestamp": "2026-01-01T10:00:10Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "content": [{"type": "text", "text": "command failed"}],
                        }
                    ]
                },
            },
            _cc_assistant([], "2026-01-01T10:00:15Z", text="I see the error"),
        ])
        result = parse_session(session)
        assert result.tool_error_count == 1
        assert result.unresolved_error_count == 1


class TestClaudeCodeSteeringEvents:
    def test_steering_events_track_runs(self):
        """Each human message should track how many AI turns followed."""
        session = _write_jsonl([
            _cc_user("implement the plan", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Read", "input": {"path": "a.py"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_assistant(
                [{"name": "Edit", "input": {"path": "a.py", "oldText": "a", "newText": "b"}}],
                "2026-01-01T10:00:10Z",
            ),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:15Z",
            ),
            _cc_user("now commit", "2026-01-01T10:01:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "git commit -am 'fix'"}}],
                "2026-01-01T10:01:05Z",
            ),
        ])
        result = parse_session(session)
        assert len(result.steering_events) == 2
        assert result.steering_events[0].run_after == 3  # Read, Edit, Bash
        assert result.steering_events[1].run_after == 1  # commit


class TestClaudeCodeSessionType:
    def test_implementation_session(self):
        session = _write_jsonl([
            _cc_user("fix the bug", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Read", "input": {"path": "a.py"}}],
                "2026-01-01T10:00:02Z",
            ),
            _cc_assistant(
                [{"name": "Edit", "input": {"path": "a.py", "oldText": "a", "newText": "b"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:08Z",
            ),
            _cc_user("looks good", "2026-01-01T10:01:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "git commit -am 'fix'"}}],
                "2026-01-01T10:01:05Z",
            ),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "git push"}}],
                "2026-01-01T10:01:10Z",
            ),
        ])
        result = parse_session(session)
        assert result.session_shape == "explore_build"

    def test_plan_handoff_session(self):
        session = _write_jsonl([
            _cc_user("Implement the following plan:\n# Plan: Fix 4 Issues", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Read", "input": {"path": "a.py"}}],
                "2026-01-01T10:00:02Z",
            ),
            _cc_assistant(
                [{"name": "Edit", "input": {"path": "a.py", "oldText": "a", "newText": "b"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "pytest"}}],
                "2026-01-01T10:00:08Z",
            ),
            _cc_user("looks good", "2026-01-01T10:01:00Z"),
            _cc_assistant(
                [{"name": "Bash", "input": {"command": "git commit -am 'fix: 4 issues'"}}],
                "2026-01-01T10:01:05Z",
            ),
            _cc_user("ship it", "2026-01-01T10:02:00Z"),
        ])
        result = parse_session(session)
        assert result.session_shape == "plan_handoff"

    def test_review_only_session(self):
        session = _write_jsonl([
            _cc_user("review this insight trace output from all angles", "2026-01-01T10:00:00Z"),
            *[
                _cc_assistant(
                    [{"name": "Read", "input": {"path": f"file{i}.py"}}],
                    f"2026-01-01T10:00:{5+i:02d}Z",
                )
                for i in range(15)
            ],
        ])
        result = parse_session(session)
        assert result.session_shape == "review_only"

    def test_review_iterate_session(self):
        """Review intent + edits = review_iterate."""
        session = _write_jsonl([
            _cc_user("review this insight trace output from all angles", "2026-01-01T10:00:00Z"),
            *[
                _cc_assistant(
                    [{"name": "Read", "input": {"path": f"file{i}.py"}}],
                    f"2026-01-01T10:00:{5+i:02d}Z",
                )
                for i in range(10)
            ],
            _cc_user("yes fix them", "2026-01-01T10:01:00Z"),
            _cc_assistant(
                [{"name": "Edit", "input": {"path": "f.py", "oldText": "a", "newText": "b"}}],
                "2026-01-01T10:01:05Z",
            ),
        ])
        result = parse_session(session)
        assert result.session_shape == "review_iterate"

    def test_debug_investigate_session(self):
        """Edits, no commits, not review/plan intent = debug_investigate."""
        session = _write_jsonl([
            _cc_user("i'm not sure the time_count guardrail works. here is the output:", "2026-01-01T10:00:00Z"),
            *[
                _cc_assistant(
                    [{"name": "Read", "input": {"path": f"file{i}.py"}}],
                    f"2026-01-01T10:00:{5+i:02d}Z",
                )
                for i in range(5)
            ],
            _cc_user("try changing that", "2026-01-01T10:01:00Z"),
            _cc_assistant(
                [{"name": "Edit", "input": {"path": "f.py", "oldText": "a", "newText": "b"}}],
                "2026-01-01T10:01:05Z",
            ),
        ])
        result = parse_session(session)
        assert result.session_shape == "debug_investigate"

    def test_error_fix_session(self):
        """Error intent + commits = error_fix."""
        session = _write_jsonl([
            _cc_user('fix this: {"detail": "duplicate key"}', "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Read", "input": {"path": "f.py"}},
                 {"name": "Read", "input": {"path": "g.py"}},
                 {"name": "Read", "input": {"path": "h.py"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_user("yes fix it", "2026-01-01T10:01:00Z"),
            _cc_assistant(
                [{"name": "Edit", "input": {"path": "f.py", "oldText": "a", "newText": "b"}},
                 {"name": "Bash", "input": {"command": "git commit -m 'fix: key error'"}}],
                "2026-01-01T10:01:05Z",
            ),
            _cc_user("done", "2026-01-01T10:02:00Z"),
        ])
        result = parse_session(session)
        assert result.session_shape == "error_fix"

    def test_explore_build_session(self):
        """No clear prefix, has commits = explore_build."""
        session = _write_jsonl([
            _cc_user("add a shortcut for sensitivity settings", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Read", "input": {"path": "f.py"}},
                 {"name": "Read", "input": {"path": "g.py"}},
                 {"name": "Read", "input": {"path": "h.py"}}],
                "2026-01-01T10:00:02Z",
            ),
            _cc_assistant(
                [{"name": "Edit", "input": {"path": "f.py", "oldText": "a", "newText": "b"}},
                 {"name": "Bash", "input": {"command": "git commit -m 'feat: add shortcut'"}}],
                "2026-01-01T10:00:05Z",
            ),
            _cc_user("looks good", "2026-01-01T10:01:00Z"),
            _cc_user("ship it", "2026-01-01T10:02:00Z"),
        ])
        result = parse_session(session)
        assert result.session_shape == "explore_build"

    def test_explore_only_session(self):
        """No clear prefix, no commits, no edits = explore_only."""
        session = _write_jsonl([
            _cc_user("how do i set up pi-coding-agent?", "2026-01-01T10:00:00Z"),
            *[
                _cc_assistant(
                    [{"name": "Read", "input": {"path": f"file{i}.py"}}],
                    f"2026-01-01T10:00:{5+i:02d}Z",
                )
                for i in range(10)
            ],
            _cc_user("ok thanks", "2026-01-01T10:01:00Z"),
        ])
        result = parse_session(session)
        assert result.session_shape == "explore_only"

    def test_abandoned_session(self):
        session = _write_jsonl([
            _cc_user("hmm", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [{"name": "Read", "input": {"path": "a.py"}}],
                "2026-01-01T10:00:05Z",
            ),
        ])
        result = parse_session(session)
        assert result.session_shape == "abandoned"


class TestClaudeCodeSystemLeverage:
    def test_detects_skill_file_edits(self):
        session = _write_jsonl([
            _cc_user("update the skill", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [
                    {
                        "name": "Edit",
                        "input": {
                            "path": ".pi/skills/django/SKILL.md",
                            "oldText": "old",
                            "newText": "new",
                        },
                    }
                ],
                "2026-01-01T10:00:05Z",
            ),
        ])
        result = parse_session(session)
        assert ".pi/skills/django/SKILL.md" in result.skill_files_edited

    def test_detects_claude_md_edits(self):
        session = _write_jsonl([
            _cc_user("update claude.md", "2026-01-01T10:00:00Z"),
            _cc_assistant(
                [
                    {
                        "name": "Edit",
                        "input": {
                            "path": "CLAUDE.md",
                            "oldText": "old",
                            "newText": "new",
                        },
                    }
                ],
                "2026-01-01T10:00:05Z",
            ),
        ])
        result = parse_session(session)
        assert result.claude_md_edited is True


class TestClaudeCodeIntent:
    def test_captures_first_intent(self):
        session = _write_jsonl([
            _cc_user("fix the N+1 query in accounts endpoint", "2026-01-01T10:00:00Z"),
            _cc_assistant([], "2026-01-01T10:00:05Z"),
        ])
        result = parse_session(session)
        assert result.first_intent == "fix the N+1 query in accounts endpoint"

    def test_captures_session_id(self):
        f = _write_jsonl([
            _cc_user("hi", "2026-01-01T10:00:00Z"),
            _cc_assistant([], "2026-01-01T10:00:05Z"),
        ])
        # Rename to match Claude Code pattern
        named = f.parent / "abc12345-1234-5678-9abc-def012345678.jsonl"
        f.rename(named)
        result = parse_session(named)
        assert result.id == "abc12345"


class TestPiFormat:
    def _pi_session_line(self, ts: str) -> dict:
        return {"type": "session", "timestamp": ts, "session_id": "test-pi-session"}

    def _pi_user(self, text: str, ts: str) -> dict:
        return {
            "type": "message",
            "timestamp": ts,
            "message": {"role": "user", "content": text},
        }

    def _pi_assistant(self, text: str, ts: str, tool_calls: list[dict] | None = None) -> dict:
        msg = {
            "type": "message",
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "content": text,
            },
        }
        if tool_calls:
            msg["message"]["tool_calls"] = tool_calls
        return msg

    def _pi_tool_result(self, text: str, ts: str) -> dict:
        return {
            "type": "message",
            "timestamp": ts,
            "message": {"role": "toolResult", "content": text},
        }

    def test_basic_pi_parsing(self):
        session = _write_jsonl([
            self._pi_session_line("2026-01-01T10:00:00Z"),
            self._pi_user("fix the bug", "2026-01-01T10:00:01Z"),
            self._pi_assistant(
                "I'll fix that.",
                "2026-01-01T10:00:05Z",
                tool_calls=[{"name": "read", "input": {"path": "a.py"}}],
            ),
            self._pi_tool_result("def foo(): pass", "2026-01-01T10:00:06Z"),
            self._pi_assistant(
                "Fixed.",
                "2026-01-01T10:00:10Z",
                tool_calls=[{"name": "edit", "input": {"path": "a.py", "oldText": "a", "newText": "b"}}],
            ),
        ])
        result = parse_session(session)
        assert result.source == "pi"
        assert result.human_prompt_count == 1
        assert result.tool_call_count == 2

    def test_pi_tool_results_not_counted_as_human(self):
        session = _write_jsonl([
            self._pi_session_line("2026-01-01T10:00:00Z"),
            self._pi_user("do it", "2026-01-01T10:00:01Z"),
            self._pi_assistant("ok", "2026-01-01T10:00:05Z"),
            self._pi_tool_result("result data", "2026-01-01T10:00:06Z"),
            self._pi_user("thanks", "2026-01-01T10:01:00Z"),
        ])
        result = parse_session(session)
        assert result.human_prompt_count == 2  # "do it" + "thanks", not tool result


# ===========================================================================
# Goal classification tests
# ===========================================================================


class TestClassifySessionGoal:
    """Tests for classify_session_goal()."""

    def test_ship_implement(self):
        assert classify_session_goal("implement the login page") == "ship"

    def test_ship_build(self):
        assert classify_session_goal("build the data pipeline") == "ship"

    def test_ship_fix(self):
        assert classify_session_goal("fix the broken test in auth module") == "ship"

    def test_ship_create(self):
        assert classify_session_goal("create a new endpoint for users") == "ship"

    def test_ship_plan_execution(self):
        assert classify_session_goal("implement the following plan: step 1...") == "ship"

    def test_investigate_why(self):
        assert classify_session_goal("why is the test failing?") == "investigate"

    def test_investigate_debug(self):
        assert classify_session_goal("debug the memory leak in worker") == "investigate"

    def test_investigate_figure_out(self):
        assert classify_session_goal("figure out why deploys are slow") == "investigate"

    def test_investigate_look_into(self):
        assert classify_session_goal("look into the failing CI pipeline") == "investigate"

    def test_review(self):
        assert classify_session_goal("review this PR for the auth changes") == "review"

    def test_review_audit(self):
        assert classify_session_goal("audit the security of our API endpoints") == "review"

    def test_explore_what_if(self):
        assert classify_session_goal("what if we used DuckDB instead of SQLite?") == "explore"

    def test_explore_could_we(self):
        assert classify_session_goal("could we replace the ORM with raw SQL?") == "explore"

    def test_plan(self):
        assert classify_session_goal("plan the database migration") == "plan"

    def test_plan_design(self):
        assert classify_session_goal("design the notification system") == "plan"

    def test_learn_explain(self):
        assert classify_session_goal("explain how the parser works") == "learn"

    def test_learn_how_does(self):
        assert classify_session_goal("how does the git tracer find commits?") == "learn"

    def test_learn_walk_me_through(self):
        assert classify_session_goal("walk me through the deploy process") == "learn"

    def test_unknown_empty(self):
        assert classify_session_goal("") == "unknown"

    def test_unknown_short(self):
        assert classify_session_goal("hey") == "unknown"

    def test_unknown_none_like(self):
        assert classify_session_goal("   ") == "unknown"

    def test_priority_investigate_over_ship(self):
        """'debug' should match investigate even though 'fix' is in ship."""
        assert classify_session_goal("debug and fix the login issue") == "investigate"

    def test_priority_review_over_ship(self):
        """'review' should match before 'update'."""
        assert classify_session_goal("review the update to the API") == "review"

    def test_priority_plan_over_ship(self):
        """'plan' should match before 'build'."""
        assert classify_session_goal("plan how to build the new feature") == "plan"
