"""Connect sessions to git repos — match commits by message, find SHAs."""

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fluency.models import SessionAnalysis


@dataclass
class SessionCommitMatch:
    """A commit matched from a session to a git repo."""
    session_id: str
    commit_sha: str
    commit_msg: str
    commit_date: datetime | None = None
    is_merge: bool = False
    pr_number: str = ""


@dataclass
class SessionGitLink:
    """Full link between a session and its git outcomes."""
    session: SessionAnalysis
    repo_path: Path
    commits: list[SessionCommitMatch] = field(default_factory=list)
    branch_name: str = ""
    branch_merged: bool = False
    file_paths: list[str] = field(default_factory=list)


@dataclass
class GitIndex:
    """Pre-built index of all commits in a repo for fast message matching."""
    repo_path: Path
    commits: dict[str, str] = field(default_factory=dict)  # sha → message
    message_index: dict[str, list[str]] = field(default_factory=dict)  # prefix → [sha, ...]
    merge_to_sources: dict[str, set[str]] = field(default_factory=dict)  # merge_sha → {source_shas}
    source_to_merges: dict[str, set[str]] = field(default_factory=dict)  # source_sha → {merge_shas}


def build_git_index(repo_path: Path) -> GitIndex:
    """Build an in-memory index of all commits in a repo.
    
    Two git calls:
    1. `git log --all --format="%H %s"` — commit messages for prefix matching
    2. `git log --merges --first-parent main --format="%H %P"` — merge→source mapping for blame
    """
    idx = GitIndex(repo_path=repo_path)
    
    # 1. All commits with messages
    result = subprocess.run(
        'git log --all --format="%H %s"',
        shell=True, cwd=str(repo_path),
        capture_output=True, text=True, timeout=30,
    )
    
    for line in result.stdout.strip().split("\n"):
        line = line.strip().strip('"')
        if not line:
            continue
        if " " in line:
            sha, msg = line.split(" ", 1)
        else:
            sha, msg = line, ""
        sha = sha.strip()
        msg = msg.strip()
        if len(sha) < 7:
            continue
        
        idx.commits[sha] = msg
        
        # Index by normalized prefixes (60 and 40 chars)
        norm = msg.lower().strip()
        for prefix_len in (60, 40):
            prefix = norm[:prefix_len]
            if prefix:
                idx.message_index.setdefault(prefix, []).append(sha)
    
    # 2. Merge commit → source commit mapping
    # For each merge on main, find the feature branch commits it introduced
    result = subprocess.run(
        'git log --merges --first-parent main --format="%H %P"',
        shell=True, cwd=str(repo_path),
        capture_output=True, text=True, timeout=30,
    )
    
    for line in result.stdout.strip().split("\n"):
        line = line.strip().strip('"')
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        merge_sha = parts[0]
        main_parent = parts[1]
        feature_tip = parts[2]
        
        # Get all commits between main_parent and feature_tip
        source_result = subprocess.run(
            f"git rev-list {main_parent}..{feature_tip}",
            shell=True, cwd=str(repo_path),
            capture_output=True, text=True, timeout=10,
        )
        
        source_shas = set()
        for src_line in source_result.stdout.strip().split("\n"):
            src_sha = src_line.strip()
            if src_sha:
                source_shas.add(src_sha)
        
        if source_shas:
            idx.merge_to_sources[merge_sha] = source_shas
            for src_sha in source_shas:
                idx.source_to_merges.setdefault(src_sha, set()).add(merge_sha)
    
    return idx


def match_message(index: GitIndex, message: str) -> list[str]:
    """Match a commit message against the index. Return matching SHAs.
    
    Strategy:
    1. Exact 60-char prefix match (fast, most common)
    2. Fallback: 40-char prefix match
    3. Always: git log --grep to catch squash merges (searches full body)
    Results from all strategies are merged and deduped.
    """
    norm = message.lower().strip()
    all_matches = set()
    
    # Try 60-char prefix
    prefix_60 = norm[:60]
    for sha in index.message_index.get(prefix_60, []):
        all_matches.add(sha)
    
    # Try 40-char prefix
    if not all_matches:
        prefix_40 = norm[:40]
        for sha in index.message_index.get(prefix_40, []):
            all_matches.add(sha)
    
    # Git grep: search full commit body for squash merge detection.
    # Only run when prefix matches are incomplete (no merge SHA found via index maps).
    needs_grep = True
    if all_matches:
        # Check if any prefix match already has a merge in the index
        for sha in list(all_matches):
            if sha in index.source_to_merges or sha in index.merge_to_sources:
                needs_grep = False
                break
    
    if needs_grep and index.repo_path.exists():
        search_term = message[:60].replace('"', '\\"').replace("'", "\\'")
        result = _git(index.repo_path, f'log --all --format="%H" --grep="{search_term}"')
        if result:
            for line in result.strip().split("\n"):
                sha = line.strip().strip('"')
                if sha and len(sha) >= 7:
                    all_matches.add(sha)
    
    return list(all_matches)


def batch_match_sessions(
    sessions_with_msgs: list[tuple[SessionAnalysis, list[str]]],
    index: GitIndex,
) -> list[SessionGitLink]:
    """Match all sessions' commit messages against the git index.
    
    For each matched SHA: resolves merge commits, file paths, branch status.
    """
    repo = index.repo_path
    links = []
    
    for sa, msgs in sessions_with_msgs:
        link = SessionGitLink(session=sa, repo_path=repo)
        seen_shas = set()
        
        for msg in msgs:
            for sha in match_message(index, msg):
                if sha in seen_shas:
                    continue
                seen_shas.add(sha)
                
                # Get commit date
                date_str = _git(repo, f"log -1 --format=%aI {sha}")
                commit_date = None
                try:
                    commit_date = datetime.fromisoformat(date_str.strip())
                except (ValueError, TypeError):
                    pass
                
                commit_msg = index.commits.get(sha, msg)
                is_merge = bool(re.search(r"\(#\d+\)", commit_msg))
                pr_match = re.search(r"#(\d+)", commit_msg)
                pr_number = pr_match.group(1) if pr_match else ""
                
                link.commits.append(SessionCommitMatch(
                    session_id=sa.id,
                    commit_sha=sha,
                    commit_msg=commit_msg,
                    commit_date=commit_date,
                    is_merge=is_merge,
                    pr_number=pr_number,
                ))
        
        # Find merge commits that contain our source commits (via pre-built map)
        source_shas = [m.commit_sha for m in link.commits if not m.is_merge]
        for src_sha in source_shas:
            merge_shas = index.source_to_merges.get(src_sha, set())
            for mshe in merge_shas:
                if mshe in seen_shas:
                    continue
                seen_shas.add(mshe)
                mmsg = index.commits.get(mshe, "")
                link.commits.append(SessionCommitMatch(
                    session_id=sa.id,
                    commit_sha=mshe,
                    commit_msg=mmsg,
                    commit_date=None,
                    is_merge=True,
                    pr_number=re.search(r"#(\d+)", mmsg).group(1) if re.search(r"#(\d+)", mmsg) else "",
                ))
        
        # File paths from non-merge commits
        non_merge = [m.commit_sha for m in link.commits if not m.is_merge]
        for sha in non_merge:
            files = _git(repo, f"diff-tree --no-commit-id --name-only -r {sha}")
            for fp in files.split("\n"):
                fp = fp.strip()
                if fp and fp not in link.file_paths:
                    link.file_paths.append(fp)
        
        # Check branch merged
        link.branch_merged = any(m.is_merge or m.pr_number for m in link.commits)
        if not link.branch_merged:
            for m in non_merge[:3]:
                branches = _git(repo, f"branch --contains {m}")
                if "main" in branches or "master" in branches:
                    link.branch_merged = True
                    break
        
        links.append(link)
    
    return links


def guess_repo_path(project_dir_name: str, code_root: Path | None = None) -> Path | None:
    """Guess the local repo path from a Claude Code or Pi project directory name.
    
    Works for any user. Session dir names encode the project path:
      CC:  -Users-USERNAME-Code-REPO
      Pi:  --Users-USERNAME-Code-REPO--
    """
    if code_root is None:
        code_root = Path.home() / "Code"

    # Extract repo name from the dir name pattern
    # Handles: -Users-XXX-Code-REPO, --Users-XXX-Code-REPO--, or just REPO
    cleaned = project_dir_name.strip("-")
    m = re.search(r"Code-(.+)$", cleaned)
    if m:
        repo_name = m.group(1).strip("-")
        p = code_root / repo_name
        if (p / ".git").exists():
            return p

    # Fallback: try the dir name itself as a repo name
    p = code_root / cleaned
    if (p / ".git").exists():
        return p

    return None


def extract_commit_messages(sa: SessionAnalysis) -> list[str]:
    """Extract first-line commit messages from session commit commands."""
    msgs = []
    for cmd in sa.commit_commands:
        # Heredoc format: git commit -m "$(cat <<'EOF'\nmessage\nEOF)"
        match = re.search(r"EOF['\"]?\)?\n(.+?)\nEOF", cmd, re.DOTALL)
        if match:
            first_line = match.group(1).strip().split("\n")[0]
            if first_line:
                msgs.append(first_line)
            continue

        # Simple -m format: git commit -m "message" (may contain newlines)
        match = re.search(r'-m "(.+?)"', cmd, re.DOTALL)
        if not match:
            match = re.search(r"-m '(.+?)'", cmd, re.DOTALL)
        if match:
            first_line = match.group(1).split("\n")[0]
            if first_line:
                msgs.append(first_line)

    return list(dict.fromkeys(msgs))  # dedupe preserving order


def match_commits_to_git(
    commit_msgs: list[str],
    repo_path: Path,
    session_start: datetime | None = None,
    session_end: datetime | None = None,
) -> list[SessionCommitMatch]:
    """Match extracted commit messages to actual git SHAs."""
    matches = []
    seen_shas = set()

    for msg in commit_msgs:
        # Search by first 60 chars of message (avoid special chars breaking grep)
        search_term = msg[:60].replace('"', '\\"').replace("'", "\\'")
        # Try exact grep first
        result = _git(repo_path, f'log --all --format="%H|%aI|%s" --grep="{search_term}"')
        if not result:
            continue

        for line in result.strip().split("\n"):
            line = line.strip().strip('"')
            if not line or "|" not in line:
                continue
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            sha, date_str, commit_msg = parts
            sha = sha.strip()

            if sha in seen_shas:
                continue
            seen_shas.add(sha)

            # Parse date
            try:
                commit_date = datetime.fromisoformat(date_str.strip())
            except ValueError:
                commit_date = None

            # Check if it's a merge commit (has PR number)
            is_merge = bool(re.search(r"\(#\d+\)", commit_msg))
            pr_match = re.search(r"#(\d+)", commit_msg)
            pr_number = pr_match.group(1) if pr_match else ""

            matches.append(SessionCommitMatch(
                session_id="",
                commit_sha=sha,
                commit_msg=commit_msg.strip(),
                commit_date=commit_date,
                is_merge=is_merge,
                pr_number=pr_number,
            ))

    # Prefer non-merge commits (the original work), keep merges for branch_merged check
    return matches


def link_session_to_git(sa: SessionAnalysis, repo_path: Path) -> SessionGitLink:
    """Create full link between session and git outcomes."""
    link = SessionGitLink(session=sa, repo_path=repo_path)

    # Extract and match commit messages
    msgs = extract_commit_messages(sa)
    if not msgs:
        return link

    # Get session time range for fallback
    session_start = sa.segments[0].start if sa.segments else None
    session_end = sa.segments[-1].end if sa.segments else None

    matches = match_commits_to_git(msgs, repo_path, session_start, session_end)

    for m in matches:
        m.session_id = sa.id

    link.commits = matches

    # Determine if branch was merged (any match is a merge commit or has PR)
    link.branch_merged = any(m.is_merge or m.pr_number for m in matches)

    # If no merge commit found, check if the non-merge commits are reachable from main
    if not link.branch_merged:
        non_merge = [m for m in matches if not m.is_merge]
        for m in non_merge[:3]:
            branches = _git(repo_path, f"branch --contains {m.commit_sha}")
            if "main" in branches or "master" in branches:
                link.branch_merged = True
                break

    # Collect file paths from non-merge commits
    non_merge_shas = [m.commit_sha for m in matches if not m.is_merge]
    for sha in non_merge_shas:
        files = _git(repo_path, f"diff-tree --no-commit-id --name-only -r {sha}")
        for f in files.split("\n"):
            f = f.strip()
            if f and f not in link.file_paths:
                link.file_paths.append(f)

    return link


def link_all_sessions(
    sessions: list[SessionAnalysis],
    project_dir_name: str,
    code_root: Path | None = None,
) -> list[SessionGitLink]:
    """Link all sessions to their git repos."""
    repo_path = guess_repo_path(project_dir_name, code_root)
    if not repo_path:
        return [SessionGitLink(session=sa, repo_path=Path()) for sa in sessions]

    return [link_session_to_git(sa, repo_path) for sa in sessions]


def _git(repo: Path, cmd: str) -> str:
    result = subprocess.run(
        f"git {cmd}",
        shell=True,
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()
