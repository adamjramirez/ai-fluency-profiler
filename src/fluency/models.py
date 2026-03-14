"""Data models for the AI Fluency Profiler."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Segment:
    start: datetime
    end: datetime
    duration_sec: float


@dataclass
class SteeringEvent:
    """A human message and how many AI turns followed it."""
    chars: int
    run_after: int


@dataclass
class SessionAnalysis:
    # Identity
    id: str
    source: str  # 'claude_code' | 'pi'
    project_path: str = ""

    # Timing
    wall_clock_min: float = 0.0
    active_min: float = 0.0
    segments: list[Segment] = field(default_factory=list)
    ai_working_sec: float = 0.0
    human_waiting_sec: float = 0.0
    human_acting_sec: float = 0.0

    # Human behavior
    human_prompt_count: int = 0
    human_chars: int = 0
    interruption_count: int = 0
    steering_events: list[SteeringEvent] = field(default_factory=list)

    # AI behavior
    tool_calls: list[str] = field(default_factory=list)
    tool_call_count: int = 0
    unique_tools: set[str] = field(default_factory=set)
    edit_count: int = 0
    lines_changed: int = 0
    tool_error_count: int = 0
    unresolved_error_count: int = 0
    has_subagents: bool = False
    has_skills: bool = False
    has_plan_mode: bool = False

    # Observable outcomes
    commit_commands: list[str] = field(default_factory=list)
    commit_count: int = 0
    test_arc: list[str] = field(default_factory=list)  # ['FAIL', 'PASS', ...]
    first_test_result: str = "none"
    last_test_result: str = "none"

    # Intent
    first_intent: str = ""
    last_assistant_text: str = ""

    # System leverage
    skills_invoked: list[str] = field(default_factory=list)
    slash_commands: list[str] = field(default_factory=list)
    skill_files_edited: list[str] = field(default_factory=list)
    claude_md_edited: bool = False

    # Session shape (derived)
    session_shape: str = "unknown"
    # Valid shapes: plan_handoff, review_iterate, review_only, debug_investigate,
    #   error_fix, explore_build, explore_only, abandoned
