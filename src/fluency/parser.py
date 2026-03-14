"""Parse session transcripts into SessionAnalysis."""

import json
import re
from datetime import datetime
from pathlib import Path

from fluency.models import SessionAnalysis, Segment, SteeringEvent

# Patterns for filtering non-human messages
_LOCAL_CMD = "<local-command"
_CMD_NAME = "<command-name>"
_INTERRUPTED = "[Request interrupted"

# Test result detection
_TEST_RAN_RE = re.compile(r"Ran (\d+) tests? in [\d.]+s")
_TEST_OK_RE = re.compile(r"^OK$", re.MULTILINE)
_TEST_FAIL_RE = re.compile(r"^FAILED \(", re.MULTILINE)
# CI markers — reject lines with these
_CI_MARKERS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z|\t")

# Slash command extraction
_SLASH_CMD_RE = re.compile(r"<command-name>/([^<]+)</command-name>")

# Gap threshold for segment detection (5 minutes)
_SEGMENT_GAP_SEC = 300

# Typing speed for human_acting_sec estimation
_CHARS_PER_SEC = 40


def parse_session(path: Path) -> SessionAnalysis:
    """Parse a session JSONL file into a SessionAnalysis."""
    path = Path(path)
    lines = _read_jsonl(path)

    if not lines:
        return SessionAnalysis(id=_extract_id(path), source="unknown")

    # Detect format
    source = _detect_source(lines)

    if source == "claude_code":
        return _parse_claude_code(lines, path)
    elif source == "pi":
        return _parse_pi(lines, path)
    else:
        return SessionAnalysis(id=_extract_id(path), source="unknown")


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL file, skip malformed lines."""
    lines = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return lines


def _detect_source(lines: list[dict]) -> str:
    """Detect whether this is Claude Code or Pi format."""
    for line in lines[:10]:
        if line.get("type") == "session":
            return "pi"
        if line.get("type") in ("user", "assistant"):
            return "claude_code"
        # Pi messages have role inside message
        msg = line.get("message", {})
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant", "toolResult"):
            return "pi"
    return "unknown"


def _extract_id(path: Path) -> str:
    """Extract session ID from filename."""
    name = path.stem
    # Claude Code: UUID format, take first 8 chars
    if len(name) >= 36 and name[8] == "-":
        return name[:8]
    return name[:16]


def _parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ===========================================================================
# Claude Code parser
# ===========================================================================


def _parse_claude_code(lines: list[dict], path: Path) -> SessionAnalysis:
    sa = SessionAnalysis(id=_extract_id(path), source="claude_code")

    timestamps: list[datetime] = []
    # Track events as (timestamp, type) for timing
    events: list[tuple[datetime, str]] = []  # ('human', 'assistant', 'tool_result')

    # For steering events
    human_indices: list[int] = []  # indices into events where human messages are
    assistant_count_since_human = 0

    # For unresolved errors
    has_error_pending = False

    for line in lines:
        msg_type = line.get("type")
        ts = _parse_timestamp(line.get("timestamp"))

        if msg_type == "user":
            _process_cc_user(line, sa, ts, events, timestamps, human_indices)
            # Check for tool errors
            content = line.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("is_error"):
                        sa.tool_error_count += 1
                        has_error_pending = True

            # Check for test results in tool results (resolves errors)
            _extract_test_results_from_content(content, sa)
            if sa.test_arc and sa.test_arc[-1] == "PASS":
                has_error_pending = False

        elif msg_type == "assistant":
            _process_cc_assistant(line, sa, ts, events, timestamps)
            # If assistant uses same tool successfully after error, resolve it
            # (simplified: any assistant activity after error starts resolution)

    # Count unresolved errors
    if has_error_pending:
        sa.unresolved_error_count = _count_unresolved_errors_cc(lines, sa)

    # Compute steering events
    sa.steering_events = _compute_steering_events_cc(events, human_indices)

    # Timing
    if timestamps:
        _compute_timing(sa, timestamps, events)

    # Test arc
    if sa.test_arc:
        sa.first_test_result = sa.test_arc[0]
        sa.last_test_result = sa.test_arc[-1]

    # Session type
    sa.session_shape = _detect_shape(sa)

    return sa


def _process_cc_user(
    line: dict,
    sa: SessionAnalysis,
    ts: datetime | None,
    events: list,
    timestamps: list,
    human_indices: list,
):
    """Process a Claude Code user message."""
    content = line.get("message", {}).get("content", "")
    text = ""
    is_tool_result_only = True

    if isinstance(content, str):
        text = content
        is_tool_result_only = False
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text += block.get("text", "")
                    is_tool_result_only = False

    clean = text.strip()

    # Check for slash commands before filtering
    for match in _SLASH_CMD_RE.finditer(text):
        cmd = match.group(1).strip()
        if cmd and cmd not in sa.slash_commands:
            sa.slash_commands.append(cmd)

    # Filter non-human messages
    if not clean or _LOCAL_CMD in clean or _CMD_NAME in clean:
        if ts:
            timestamps.append(ts)
            events.append((ts, "tool_result"))
        return

    if _INTERRUPTED in clean:
        sa.interruption_count += 1
        if ts:
            timestamps.append(ts)
            events.append((ts, "tool_result"))
        return

    if is_tool_result_only:
        if ts:
            timestamps.append(ts)
            events.append((ts, "tool_result"))
        return

    # Real human message
    sa.human_prompt_count += 1
    sa.human_chars += len(clean)

    if not sa.first_intent:
        sa.first_intent = clean

    if ts:
        timestamps.append(ts)
        events.append((ts, "human"))
        human_indices.append(len(events) - 1)


def _process_cc_assistant(
    line: dict,
    sa: SessionAnalysis,
    ts: datetime | None,
    events: list,
    timestamps: list,
):
    """Process a Claude Code assistant message."""
    content = line.get("message", {}).get("content", [])
    assistant_text = ""

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue

            if block.get("type") == "text":
                assistant_text += block.get("text", "")

            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                if not isinstance(inp, dict):
                    inp = {}

                sa.tool_calls.append(name)
                sa.tool_call_count += 1
                sa.unique_tools.add(name)

                # Edit tracking
                if name == "Edit":
                    sa.edit_count += 1
                    old = inp.get("oldText", "")
                    new = inp.get("newText", "")
                    sa.lines_changed += len(old.split("\n")) + len(new.split("\n"))

                    # Skill file edits
                    edit_path = inp.get("path", "")
                    if edit_path and ("SKILL.md" in edit_path or "skills/" in edit_path.lower()):
                        if edit_path not in sa.skill_files_edited:
                            sa.skill_files_edited.append(edit_path)
                    if edit_path and ("CLAUDE.md" in edit_path or "claude.md" in edit_path):
                        sa.claude_md_edited = True

                # Commit tracking
                elif name == "Bash":
                    cmd = inp.get("command", "")
                    if "git commit" in cmd:
                        sa.commit_count += 1
                        sa.commit_commands.append(cmd)

                # Subagent tracking
                elif name in ("Task", "TaskCreate"):
                    sa.has_subagents = True

                # Skill tracking
                elif name == "Skill":
                    sa.has_skills = True
                    skill_name = inp.get("skill", "")
                    if skill_name and skill_name not in sa.skills_invoked:
                        sa.skills_invoked.append(skill_name)

    if assistant_text:
        sa.last_assistant_text = assistant_text

    if ts:
        timestamps.append(ts)
        events.append((ts, "assistant"))


def _extract_test_results_from_content(content, sa: SessionAnalysis):
    """Extract test results from user message content (tool results)."""
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict):
            continue

        # Get text from tool result
        text = ""
        if block.get("type") == "tool_result":
            rc = block.get("content", "")
            if isinstance(rc, str):
                text = rc
            elif isinstance(rc, list):
                for rb in rc:
                    if isinstance(rb, dict) and rb.get("type") == "text":
                        text += rb.get("text", "")
        elif block.get("type") == "text":
            # Also check plain text blocks in tool result messages
            continue  # Human text, not test output

        if not text:
            continue

        _detect_test_result(text, sa)


def _detect_test_result(text: str, sa: SessionAnalysis):
    """Detect test results in text, rejecting CI output."""
    # Check for CI markers in the text
    lines = text.split("\n")

    ran_line_idx = None
    for i, line in enumerate(lines):
        # Reject CI output
        if _CI_MARKERS.search(line):
            continue
        if _TEST_RAN_RE.search(line):
            ran_line_idx = i
            break

    if ran_line_idx is None:
        return

    # Look for OK or FAILED after the "Ran N tests" line
    remaining = "\n".join(lines[ran_line_idx:])
    if _TEST_FAIL_RE.search(remaining):
        sa.test_arc.append("FAIL")
    elif _TEST_OK_RE.search(remaining):
        sa.test_arc.append("PASS")


def _count_unresolved_errors_cc(lines: list[dict], sa: SessionAnalysis) -> int:
    """Count tool errors that were never followed by a passing test or successful retry."""
    errors = 0
    error_pending = False

    for line in lines:
        if line.get("type") == "user":
            content = line.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("is_error"):
                            error_pending = True
                        # Check if test passed in same message
                        if block.get("type") == "tool_result":
                            rc = block.get("content", "")
                            text = ""
                            if isinstance(rc, str):
                                text = rc
                            elif isinstance(rc, list):
                                for rb in rc:
                                    if isinstance(rb, dict) and rb.get("type") == "text":
                                        text += rb.get("text", "")
                            if text and _TEST_OK_RE.search(text) and _TEST_RAN_RE.search(text):
                                error_pending = False

        elif line.get("type") == "assistant":
            # Assistant working on fix doesn't resolve, but test pass does
            pass

    if error_pending:
        errors = 1  # Simplified: at least one unresolved

    return errors


def _compute_steering_events_cc(
    events: list[tuple[datetime, str]], human_indices: list[int]
) -> list[SteeringEvent]:
    """Compute steering events: for each human message, count assistant turns that followed."""
    steering = []

    for i, h_idx in enumerate(human_indices):
        # Count assistant events until next human event
        next_h_idx = human_indices[i + 1] if i + 1 < len(human_indices) else len(events)
        run_after = sum(
            1 for j in range(h_idx + 1, next_h_idx) if events[j][1] == "assistant"
        )
        # Estimate chars from the human event (we stored the index)
        # We don't have chars here, so use 0 as placeholder
        steering.append(SteeringEvent(chars=0, run_after=run_after))

    return steering


# ===========================================================================
# Pi parser
# ===========================================================================


def _extract_pi_text(content) -> str:
    """Extract text from Pi content (string or list of {type: text, text: ...} blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts).strip()
    return str(content).strip() if content else ""


def _parse_pi(lines: list[dict], path: Path) -> SessionAnalysis:
    sa = SessionAnalysis(id=_extract_id(path), source="pi")

    timestamps: list[datetime] = []
    events: list[tuple[datetime, str]] = []
    human_indices: list[int] = []

    for line in lines:
        line_type = line.get("type")
        ts = _parse_timestamp(line.get("timestamp"))
        msg = line.get("message", {})

        if not isinstance(msg, dict):
            continue

        role = msg.get("role")

        if line_type == "message" and role == "user":
            content = msg.get("content", "")
            text = _extract_pi_text(content)

            if not text:
                continue

            sa.human_prompt_count += 1
            sa.human_chars += len(text)
            if not sa.first_intent:
                sa.first_intent = text

            if ts:
                timestamps.append(ts)
                events.append((ts, "human"))
                human_indices.append(len(events) - 1)

        elif line_type == "message" and role == "assistant":
            content = msg.get("content", [])
            assistant_text = ""

            if isinstance(content, str):
                assistant_text = content
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue

                    block_type = block.get("type", "")

                    if block_type == "text":
                        assistant_text += block.get("text", "")

                    # Pi tool calls: {type: "toolCall", name: "...", arguments: {...}}
                    elif block_type == "toolCall":
                        name = block.get("name", "")
                        args = block.get("arguments", {})
                        if not isinstance(args, dict):
                            args = {}

                        sa.tool_calls.append(name)
                        sa.tool_call_count += 1
                        sa.unique_tools.add(name)

                        name_lower = name.lower()

                        # Edit tracking
                        if name_lower == "edit":
                            sa.edit_count += 1
                            old = args.get("oldText", "")
                            new = args.get("newText", "")
                            sa.lines_changed += len(old.split("\n")) + len(new.split("\n"))

                            edit_path = args.get("path", "")
                            if edit_path and ("SKILL.md" in edit_path or "skills/" in edit_path.lower()):
                                if edit_path not in sa.skill_files_edited:
                                    sa.skill_files_edited.append(edit_path)
                            if edit_path and ("CLAUDE.md" in edit_path or "claude.md" in edit_path):
                                sa.claude_md_edited = True

                        # Commit tracking
                        elif name_lower == "bash":
                            cmd = args.get("command", "")
                            if "git commit" in cmd:
                                sa.commit_count += 1
                                sa.commit_commands.append(cmd)

                        # Subagent tracking
                        elif name_lower in ("task", "taskcreate", "dispatch_agent"):
                            sa.has_subagents = True

                        # Skill tracking
                        elif name_lower == "skill":
                            sa.has_skills = True
                            skill_name = args.get("skill", "")
                            if skill_name and skill_name not in sa.skills_invoked:
                                sa.skills_invoked.append(skill_name)

            # Also check old-style tool_calls array (for backward compat)
            tool_calls = msg.get("tool_calls", [])
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        name = tc.get("name", "")
                        sa.tool_calls.append(name)
                        sa.tool_call_count += 1
                        sa.unique_tools.add(name)

                        inp = tc.get("input", {})
                        if isinstance(inp, dict):
                            if name.lower() == "edit":
                                sa.edit_count += 1
                                old = inp.get("oldText", "")
                                new = inp.get("newText", "")
                                sa.lines_changed += len(old.split("\n")) + len(new.split("\n"))

            if assistant_text:
                sa.last_assistant_text = assistant_text

            if ts:
                timestamps.append(ts)
                events.append((ts, "assistant"))

        elif line_type == "message" and role == "toolResult":
            # Tool result — extract text from content blocks
            content = msg.get("content", "")
            text = _extract_pi_text(content)
            if text:
                _detect_test_result(text, sa)
            if ts:
                timestamps.append(ts)
                events.append((ts, "tool_result"))

    # Steering events
    sa.steering_events = _compute_steering_events_cc(events, human_indices)

    # Timing
    if timestamps:
        _compute_timing(sa, timestamps, events)

    # Test arc
    if sa.test_arc:
        sa.first_test_result = sa.test_arc[0]
        sa.last_test_result = sa.test_arc[-1]

    # Session type
    sa.session_shape = _detect_shape(sa)

    return sa


# ===========================================================================
# Timing
# ===========================================================================


def _compute_timing(
    sa: SessionAnalysis,
    timestamps: list[datetime],
    events: list[tuple[datetime, str]],
):
    """Compute timing from timestamps and event types."""
    if len(timestamps) < 2:
        return

    sorted_ts = sorted(timestamps)
    sa.wall_clock_min = (sorted_ts[-1] - sorted_ts[0]).total_seconds() / 60

    # Segments: split on gaps > 5 min
    segments = []
    seg_start = sorted_ts[0]
    prev_ts = sorted_ts[0]

    for ts in sorted_ts[1:]:
        gap = (ts - prev_ts).total_seconds()
        if gap > _SEGMENT_GAP_SEC:
            segments.append(
                Segment(
                    start=seg_start,
                    end=prev_ts,
                    duration_sec=(prev_ts - seg_start).total_seconds(),
                )
            )
            seg_start = ts
        prev_ts = ts

    # Final segment
    segments.append(
        Segment(
            start=seg_start,
            end=prev_ts,
            duration_sec=(prev_ts - seg_start).total_seconds(),
        )
    )

    sa.segments = segments
    sa.active_min = sum(s.duration_sec for s in segments) / 60

    # Time breakdown: AI working vs human waiting
    ai_working = 0.0
    human_waiting = 0.0

    for i in range(1, len(events)):
        prev_ts_e, prev_type = events[i - 1]
        curr_ts_e, curr_type = events[i]
        gap = (curr_ts_e - prev_ts_e).total_seconds()

        if gap > _SEGMENT_GAP_SEC:
            continue  # Break — don't count

        if prev_type == "human" and curr_type == "assistant":
            ai_working += gap  # AI processing user request
        elif prev_type == "assistant" and curr_type == "assistant":
            ai_working += gap  # AI multi-step
        elif prev_type == "tool_result" and curr_type == "assistant":
            ai_working += gap  # AI processing tool output
        elif prev_type == "assistant" and curr_type == "human":
            human_waiting += gap  # Human reading/deciding
        elif prev_type == "assistant" and curr_type == "tool_result":
            ai_working += gap  # Tool executing

    sa.ai_working_sec = ai_working
    sa.human_waiting_sec = human_waiting
    sa.human_acting_sec = sa.human_chars / _CHARS_PER_SEC


# ===========================================================================
# Session classification
# ===========================================================================


def _detect_shape(sa: SessionAnalysis) -> str:
    """Detect session shape from intent text + outcome signals.
    
    Order matters — most specific first.
    """
    if sa.human_prompt_count < 3 and sa.tool_call_count < 5:
        return "abandoned"

    intent_l = sa.first_intent.lower()[:200]

    # 1. Plan handoff: explicit plan execution
    if intent_l.startswith("implement the following plan"):
        return "plan_handoff"

    # 2-3. Review sessions: review/trace intent
    is_review = (
        "review" in intent_l[:50]
        or "trace output" in intent_l[:80]
    )
    if is_review:
        if sa.edit_count > 0 or sa.commit_count > 0:
            return "review_iterate"
        return "review_only"

    # 4. Error fix: error/bug intent + shipped
    is_error = any(w in intent_l[:100] for w in [
        "fix this", "fix:", "i got this", '"detail"', '"detail\\"',
    ])
    if is_error and sa.commit_count > 0:
        return "error_fix"

    # 5. Debug/investigate: edits but no commits, not caught above
    if sa.edit_count > 0 and sa.commit_count == 0:
        return "debug_investigate"

    # 6. Explore + build: no clear prefix but produced commits
    if sa.commit_count > 0:
        return "explore_build"

    # 7. Explore only: fallthrough
    return "explore_only"
