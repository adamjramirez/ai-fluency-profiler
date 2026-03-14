"""Tests for batch git matching (connector)."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from fluency.connector import (
    build_git_index,
    match_message,
    batch_match_sessions,
    extract_commit_messages,
    guess_repo_path,
    GitIndex,
)


# --- Unit tests (no real git) ---


def _make_index(commits: dict[str, str]) -> GitIndex:
    """Helper: build a GitIndex from sha→message dict."""
    message_index: dict[str, list[str]] = {}
    for sha, msg in commits.items():
        # 60-char prefix
        prefix_60 = msg[:60].lower().strip()
        message_index.setdefault(prefix_60, []).append(sha)
        # 40-char prefix
        prefix_40 = msg[:40].lower().strip()
        message_index.setdefault(prefix_40, []).append(sha)
    return GitIndex(
        repo_path=Path("/fake"),
        commits=commits,
        message_index=message_index,
    )


def test_match_message_exact_prefix():
    idx = _make_index({
        "abc123": "feat: add user authentication to the login page",
        "def456": "fix: correct N+1 query in account list view",
    })
    result = match_message(idx, "feat: add user authentication to the login page")
    assert "abc123" in result


def test_match_message_60char_prefix():
    long_msg = "feat: implement the new dashboard widget for customer health scores and metrics"
    idx = _make_index({"abc123": long_msg})
    result = match_message(idx, long_msg[:60])
    assert "abc123" in result


def test_match_message_40char_fallback():
    idx = _make_index({
        "abc123": "fix: correct the account sync service for HubSpot integration issues",
    })
    result = match_message(idx, "fix: correct the account sync service for different suffix here")
    assert "abc123" in result


def test_match_message_no_match():
    idx = _make_index({"abc123": "feat: something completely different"})
    result = match_message(idx, "fix: unrelated commit message")
    assert result == []


def test_match_message_multiple_matches():
    idx = _make_index({
        "abc123": "feat: add user auth — phase 1",
        "def456": "feat: add user auth — phase 2",
    })
    result = match_message(idx, "feat: add user auth — phase 1")
    assert "abc123" in result


def test_match_message_case_insensitive():
    idx = _make_index({"abc123": "Fix: Correct The Account Sync"})
    result = match_message(idx, "fix: correct the account sync")
    assert "abc123" in result


# --- Integration tests (real git repo required) ---


def _find_any_git_repo(min_commits: int = 100) -> Path | None:
    """Find any git repo in ~/Code/ with enough commits for testing."""
    code_root = Path.home() / "Code"
    if not code_root.exists():
        return None
    for d in sorted(code_root.iterdir()):
        git_dir = d / ".git"
        if not git_dir.exists():
            continue
        try:
            result = subprocess.run(
                "git rev-list --all --count",
                shell=True, cwd=str(d),
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and int(result.stdout.strip()) >= min_commits:
                return d
        except (ValueError, subprocess.TimeoutExpired):
            continue
    return None


TEST_REPO = _find_any_git_repo()


@pytest.mark.skipif(
    TEST_REPO is None,
    reason="No suitable git repo found in ~/Code/",
)
class TestBuildGitIndex:
    def test_build_index_has_commits(self):
        idx = build_git_index(TEST_REPO)
        assert len(idx.commits) > 100
        assert len(idx.message_index) > 100

    def test_build_index_matches_rev_list_count(self):
        idx = build_git_index(TEST_REPO)
        result = subprocess.run(
            "git rev-list --all --count",
            shell=True, cwd=str(TEST_REPO),
            capture_output=True, text=True,
        )
        expected = int(result.stdout.strip())
        assert abs(len(idx.commits) - expected) < 10

    def test_match_known_commit(self):
        """Match a real commit from the repo."""
        idx = build_git_index(TEST_REPO)
        result = subprocess.run(
            'git log --all --format="%H %s" -1',
            shell=True, cwd=str(TEST_REPO),
            capture_output=True, text=True,
        )
        line = result.stdout.strip().strip('"')
        sha, msg = line.split(" ", 1)

        matches = match_message(idx, msg)
        assert sha in matches, f"Expected {sha} in matches for '{msg[:60]}'"


def _find_any_pi_project_dir() -> Path | None:
    """Find any Pi session project directory with sessions."""
    pi_base = Path.home() / ".pi" / "agent" / "sessions"
    if not pi_base.exists():
        return None
    for d in sorted(pi_base.iterdir()):
        if d.is_dir() and len(list(d.glob("*.jsonl"))) >= 5:
            return d
    return None


PI_PROJECT_DIR = _find_any_pi_project_dir()


@pytest.mark.skipif(
    not Path("/tmp/git_match_baseline.json").exists(),
    reason="No baseline file — run baseline collection first",
)
class TestBatchMatchBaseline:
    def test_batch_match_finds_baseline_shas(self):
        """Batch matching finds ≥90% of SHAs that per-session grep found.

        This test requires a baseline file at /tmp/git_match_baseline.json
        generated by a prior profiler run. It auto-discovers the matching
        repo and session directory from the baseline content.
        """
        with open("/tmp/git_match_baseline.json") as f:
            baseline = json.load(f)

        if not baseline:
            pytest.skip("Baseline is empty")

        # Find the session directory that contains these files
        session_dir = None
        sample_file = next(iter(baseline))
        for base in [
            Path.home() / ".pi" / "agent" / "sessions",
            Path.home() / ".claude" / "projects",
        ]:
            if not base.exists():
                continue
            for d in base.iterdir():
                if (d / sample_file).exists():
                    session_dir = d
                    break
            if session_dir:
                break

        if not session_dir:
            pytest.skip("Cannot find session directory for baseline files")

        # Guess the repo from the session directory name
        repo_path = guess_repo_path(session_dir.name)
        if not repo_path or not (repo_path / ".git").exists():
            pytest.skip(f"Cannot find git repo for {session_dir.name}")

        idx = build_git_index(repo_path)

        from fluency.parser import parse_session

        sessions_with_msgs = []
        session_filenames = []
        for filename in baseline:
            session_path = session_dir / filename
            if not session_path.exists():
                continue
            sa = parse_session(session_path)
            msgs = extract_commit_messages(sa)
            sessions_with_msgs.append((sa, msgs))
            session_filenames.append(filename)

        links = batch_match_sessions(sessions_with_msgs, idx)

        total_baseline_shas = 0
        found_shas = 0

        for link, filename in zip(links, session_filenames):
            expected_shas = baseline[filename]
            matched_shas = {m.commit_sha for m in link.commits}

            for sha in expected_shas:
                total_baseline_shas += 1
                if any(sha.startswith(m[:8]) or m.startswith(sha[:8]) for m in matched_shas):
                    found_shas += 1

        rate = found_shas / total_baseline_shas if total_baseline_shas else 0
        assert rate >= 0.90, (
            f"Batch match found {found_shas}/{total_baseline_shas} ({rate:.0%}) "
            f"of baseline SHAs — need ≥90%"
        )
