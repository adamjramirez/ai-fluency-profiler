"""Tests for git tracer — connecting sessions to git outcomes."""

import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fluency.git_tracer import trace_git_outcomes, DurabilityReport


def _run(cmd: str, cwd: str):
    """Run a shell command in a directory."""
    subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, check=True,
                   env={**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T10:00:00Z",
                        "GIT_COMMITTER_DATE": "2026-01-01T10:00:00Z"})


def _run_at(cmd: str, cwd: str, date: str):
    """Run a shell command with a specific git date."""
    subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, check=True,
                   env={**os.environ, "GIT_AUTHOR_DATE": date,
                        "GIT_COMMITTER_DATE": date})


@pytest.fixture
def git_repo(tmp_path):
    """Create a test git repo with controlled history.
    
    Timeline:
    1. Initial commit on main (base file)
    2. Feature branch: add 10 lines to feature.py + 5 lines to test_feature.py
    3. Merge feature branch to main
    4. fix: commit that changes 2 lines of feature.py (bug fix)
    5. feat: commit that changes 3 lines of feature.py (evolution)
    6. Architecture: delete old_module.py entirely
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    # Init
    _run("git init -b main", str(repo))
    _run("git config user.email 'test@test.com'", str(repo))
    _run("git config user.name 'Test'", str(repo))

    # Initial commit
    (repo / "feature.py").write_text("# base\npass\n")
    (repo / "old_module.py").write_text("# will be deleted\nold_code\n")
    _run("git add -A && git commit -m 'initial'", str(repo))

    # Feature branch
    _run("git checkout -b feature/test-feature", str(repo))

    # Write feature code (10 lines)
    feature_code = "\n".join([f"line{i} = {i}" for i in range(1, 11)]) + "\n"
    (repo / "feature.py").write_text(feature_code)

    # Write test code (5 lines)
    test_code = "\n".join([f"def test_{i}(): assert True" for i in range(1, 6)]) + "\n"
    (repo / "test_feature.py").write_text(test_code)

    # Also write to old_module.py (will be deleted later — architecture loss)
    old_code = "\n".join([f"old_line{i} = {i}" for i in range(1, 8)]) + "\n"
    (repo / "old_module.py").write_text(old_code)

    _run_at("git add -A && git commit -m 'feat: add test feature'",
            str(repo), "2026-01-15T10:00:00Z")
    feat_sha = subprocess.run("git rev-parse HEAD", shell=True, cwd=str(repo),
                              capture_output=True, text=True).stdout.strip()

    # Merge to main
    _run("git checkout main", str(repo))
    _run_at("git merge feature/test-feature --no-ff -m 'Merge feature/test-feature'",
            str(repo), "2026-01-15T10:05:00Z")

    # Bug fix: change 2 lines of feature.py
    lines = (repo / "feature.py").read_text().split("\n")
    lines[0] = "line1 = 'fixed'"  # changed
    lines[1] = "line2 = 'fixed'"  # changed
    (repo / "feature.py").write_text("\n".join(lines))
    _run_at("git add -A && git commit -m 'fix: correct line1 and line2'",
            str(repo), "2026-01-20T10:00:00Z")

    # Feature evolution: change 3 more lines
    lines = (repo / "feature.py").read_text().split("\n")
    lines[2] = "line3 = 'evolved'"
    lines[3] = "line4 = 'evolved'"
    lines[4] = "line5 = 'evolved'"
    (repo / "feature.py").write_text("\n".join(lines))
    _run_at("git add -A && git commit -m 'feat: improve feature lines'",
            str(repo), "2026-01-25T10:00:00Z")

    # Architecture: delete old_module.py
    (repo / "old_module.py").unlink()
    _run_at("git add -A && git commit -m 'chore: remove old_module'",
            str(repo), "2026-01-28T10:00:00Z")

    return repo, feat_sha


class TestGitTracer:
    def test_finds_commits_by_branch(self, git_repo):
        repo, feat_sha = git_repo
        result = trace_git_outcomes(
            repo_path=repo,
            branch_name="feature/test-feature",
            commit_shas=[],
            file_paths=[],
            session_start=datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc),
            session_end=datetime(2026, 1, 15, 23, 59, tzinfo=timezone.utc),
        )
        # Should find the feature commit
        assert result is not None
        assert result.total_lines_added > 0

    def test_line_survival(self, git_repo):
        repo, feat_sha = git_repo
        result = trace_git_outcomes(
            repo_path=repo,
            branch_name="feature/test-feature",
            commit_shas=[feat_sha],
            file_paths=["feature.py", "test_feature.py", "old_module.py"],
            session_start=datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc),
            session_end=datetime(2026, 1, 15, 23, 59, tzinfo=timezone.utc),
        )
        assert result is not None
        # feature.py: 10 lines added, 2 lost to bug fix, 3 lost to evolution = 5 surviving
        # test_feature.py: 5 lines added, all surviving
        # old_module.py: 7 lines added, all lost to architecture (file deleted)
        # Total: 22 added, 10 surviving raw
        assert result.total_lines_added >= 15  # At least feature.py + test_feature.py
        assert result.raw_survival_pct < 1.0  # Some lines died

    def test_loss_categorization(self, git_repo):
        repo, feat_sha = git_repo
        result = trace_git_outcomes(
            repo_path=repo,
            branch_name="feature/test-feature",
            commit_shas=[feat_sha],
            file_paths=["feature.py", "test_feature.py", "old_module.py"],
            session_start=datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc),
            session_end=datetime(2026, 1, 15, 23, 59, tzinfo=timezone.utc),
        )
        assert result is not None
        # feature.py: first non-source commit is fix: → all 5 lost lines = bug_fix
        # (per-line attribution is a future enhancement; per-file is good enough)
        assert result.lines_lost_to_bugs >= 1
        # old_module.py deleted → architecture losses
        assert result.lines_lost_to_architecture >= 1
        # Total categorized losses should account for all non-surviving lines
        total_lost = result.total_lines_added - result.total_lines_surviving
        categorized = (result.lines_lost_to_bugs + result.lines_lost_to_architecture +
                       result.lines_lost_to_evolution + result.lines_lost_to_refactor +
                       result.lines_lost_to_maintenance)
        assert categorized == total_lost, f"categorized={categorized} vs lost={total_lost}"

    def test_adjusted_survival_excludes_architecture(self, git_repo):
        repo, feat_sha = git_repo
        result = trace_git_outcomes(
            repo_path=repo,
            branch_name="feature/test-feature",
            commit_shas=[feat_sha],
            file_paths=["feature.py", "test_feature.py", "old_module.py"],
            session_start=datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc),
            session_end=datetime(2026, 1, 15, 23, 59, tzinfo=timezone.utc),
        )
        assert result is not None
        # Adjusted should be higher than raw (architecture losses excluded)
        assert result.adjusted_survival_pct >= result.raw_survival_pct

    def test_branch_merged(self, git_repo):
        repo, feat_sha = git_repo
        result = trace_git_outcomes(
            repo_path=repo,
            branch_name="feature/test-feature",
            commit_shas=[feat_sha],
            file_paths=[],
            session_start=datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc),
            session_end=datetime(2026, 1, 15, 23, 59, tzinfo=timezone.utc),
        )
        assert result is not None
        assert result.branch_merged is True

    def test_bug_count(self, git_repo):
        repo, feat_sha = git_repo
        result = trace_git_outcomes(
            repo_path=repo,
            branch_name="feature/test-feature",
            commit_shas=[feat_sha],
            file_paths=["feature.py", "test_feature.py", "old_module.py"],
            session_start=datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc),
            session_end=datetime(2026, 1, 15, 23, 59, tzinfo=timezone.utc),
        )
        assert result is not None
        assert result.bug_count >= 1  # The fix: commit

    def test_waste_pct(self, git_repo):
        repo, feat_sha = git_repo
        result = trace_git_outcomes(
            repo_path=repo,
            branch_name="feature/test-feature",
            commit_shas=[feat_sha],
            file_paths=["feature.py", "test_feature.py", "old_module.py"],
            session_start=datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc),
            session_end=datetime(2026, 1, 15, 23, 59, tzinfo=timezone.utc),
        )
        assert result is not None
        # waste_pct should reflect the architecture loss (old_module.py deleted)
        assert result.waste_pct > 0
