"""Connect sessions to git outcomes and measure code durability."""

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class FileChange:
    path: str
    lines_added: int = 0
    lines_surviving: int = 0
    survival_pct: float = 0.0
    file_exists: bool = True
    loss_category: str = "unknown"
    loss_commit: str | None = None
    loss_commit_msg: str | None = None


@dataclass
class DurabilityReport:
    total_lines_added: int = 0
    total_lines_surviving: int = 0
    raw_survival_pct: float = 0.0

    lines_lost_to_bugs: int = 0
    lines_lost_to_architecture: int = 0
    lines_lost_to_evolution: int = 0
    lines_lost_to_refactor: int = 0
    lines_lost_to_maintenance: int = 0

    adjusted_survival_pct: float = 0.0
    bug_count: int = 0
    waste_pct: float = 0.0

    branch_merged: bool = False
    files: list[FileChange] = field(default_factory=list)


def batch_trace(
    links: list,
    repo_path: Path,
) -> dict[str, "DurabilityReport"]:
    """Trace durability for all sessions in one repo with shared caches.
    
    Returns dict of session_id → DurabilityReport.
    """
    if not (repo_path / ".git").exists():
        return {}
    
    # Build caches
    existing_files = _batch_ls_tree(repo_path)
    blame_cache: dict[str, dict[str, int]] = {}  # filepath → {sha_prefix: count}
    show_cache: dict[str, int] = {}  # "sha:filepath" → lines_added
    loss_cache: dict[str, list[tuple[str, str]]] = {}  # filepath → [(sha, msg), ...]
    
    results = {}
    
    for link in links:
        if not link.commits:
            continue
        
        session_id = link.session.id
        
        # Separate source and merge SHAs
        source_shas = [m.commit_sha for m in link.commits if not m.is_merge]
        if not source_shas:
            source_shas = [m.commit_sha for m in link.commits]
        all_shas = [m.commit_sha for m in link.commits]
        
        report = DurabilityReport()
        report.branch_merged = link.branch_merged
        
        # Collect all files
        all_files = set(link.file_paths)
        
        bug_commits = set()
        
        for filepath in sorted(all_files):
            file_exists = filepath in existing_files
            
            # Count lines added (cached)
            lines_added = 0
            for sha in source_shas:
                cache_key = f"{sha}:{filepath}"
                if cache_key not in show_cache:
                    diff = _git(repo_path, f"show {sha} -- {filepath}")
                    count = sum(1 for line in diff.split("\n")
                               if line.startswith("+") and not line.startswith("+++"))
                    show_cache[cache_key] = count
                lines_added += show_cache[cache_key]
            
            if lines_added == 0:
                continue
            
            fc = FileChange(path=filepath, lines_added=lines_added, file_exists=file_exists)
            
            if not file_exists:
                fc.lines_surviving = 0
                fc.loss_category = "architecture"
                # Find deletion commit
                del_log = _git(repo_path, f'log --diff-filter=D --format="%H %s" -- {filepath}')
                if del_log:
                    parts = del_log.split("\n")[0].split(" ", 1)
                    fc.loss_commit = parts[0].strip('"') if parts else None
                    fc.loss_commit_msg = parts[1] if len(parts) > 1 else None
            else:
                # Blame (cached)
                if filepath not in blame_cache:
                    blame_output = _git(repo_path, f"blame --porcelain HEAD -- {filepath}")
                    sha_counts: dict[str, int] = {}
                    for bline in blame_output.split("\n"):
                        if bline and len(bline) >= 40 and bline[0] != "\t":
                            bparts = bline.split()
                            if bparts:
                                bsha = bparts[0][:8]
                                sha_counts[bsha] = sha_counts.get(bsha, 0) + 1
                    blame_cache[filepath] = sha_counts
                
                sha_counts = blame_cache[filepath]
                
                # Count lines surviving from our commits
                surviving = 0
                for own_sha in all_shas:
                    prefix = own_sha[:8]
                    surviving += sha_counts.get(prefix, 0)
                
                fc.lines_surviving = min(surviving, lines_added)
                fc.survival_pct = fc.lines_surviving / lines_added if lines_added > 0 else 1.0
                
                # Loss categorization (cached)
                if fc.lines_surviving < fc.lines_added:
                    if filepath not in loss_cache:
                        # Get earliest source commit date
                        date = _git(repo_path, f"log -1 --format=%aI {source_shas[0]}")
                        if date:
                            log = _git(repo_path,
                                f'log --reverse --format="%H %s" --after="{date}" -- {filepath}')
                            entries = []
                            for ll in (log.strip().split("\n") if log.strip() else []):
                                if not ll.strip():
                                    continue
                                lparts = ll.strip().strip('"').split(" ", 1)
                                entries.append((lparts[0], lparts[1] if len(lparts) > 1 else ""))
                            loss_cache[filepath] = entries
                        else:
                            loss_cache[filepath] = []
                    
                    # Find first non-own commit
                    for lsha, lmsg in loss_cache[filepath]:
                        if any(lsha.startswith(s[:8]) or s.startswith(lsha[:8]) for s in all_shas):
                            continue
                        fc.loss_commit = lsha
                        fc.loss_commit_msg = lmsg
                        fc.loss_category = _categorize_commit_msg(lmsg, file_exists)
                        break
            
            report.files.append(fc)
            report.total_lines_added += fc.lines_added
            report.total_lines_surviving += fc.lines_surviving
            
            lost = fc.lines_added - fc.lines_surviving
            if lost > 0:
                cat = fc.loss_category
                if cat == "bug_fix":
                    report.lines_lost_to_bugs += lost
                    if fc.loss_commit:
                        bug_commits.add(fc.loss_commit)
                elif cat == "architecture":
                    report.lines_lost_to_architecture += lost
                elif cat == "feature_evolution":
                    report.lines_lost_to_evolution += lost
                elif cat == "refactor":
                    report.lines_lost_to_refactor += lost
                elif cat == "maintenance":
                    report.lines_lost_to_maintenance += lost
        
        report.bug_count = len(bug_commits)
        
        if report.total_lines_added > 0:
            report.raw_survival_pct = report.total_lines_surviving / report.total_lines_added
            adj_total = report.total_lines_added - report.lines_lost_to_architecture
            if adj_total > 0:
                report.adjusted_survival_pct = min(1.0, report.total_lines_surviving / adj_total)
            else:
                report.adjusted_survival_pct = 1.0
            report.waste_pct = report.lines_lost_to_architecture / report.total_lines_added
        
        results[session_id] = report
    
    return results


def _batch_ls_tree(repo: Path) -> set[str]:
    """Get all file paths at HEAD in one call."""
    output = _git(repo, "ls-tree -r --name-only HEAD")
    return set(f.strip() for f in output.split("\n") if f.strip())


def trace_from_link(link) -> "DurabilityReport | None":
    """Trace git outcomes from a SessionGitLink (connector output).
    
    This is the preferred entry point — connector already found the SHAs.
    """
    if not link.commits or not link.repo_path or not (link.repo_path / ".git").exists():
        return None

    # Use non-merge commit SHAs for line counting
    commit_shas = [m.commit_sha for m in link.commits if not m.is_merge]
    if not commit_shas:
        commit_shas = [m.commit_sha for m in link.commits]

    # ALL SHAs (including merges) go to exclude list for loss categorization
    all_shas = [m.commit_sha for m in link.commits]

    return trace_git_outcomes(
        repo_path=link.repo_path,
        commit_shas=commit_shas,
        file_paths=link.file_paths,
        branch_merged=link.branch_merged,
        exclude_shas=all_shas,
    )


def trace_git_outcomes(
    repo_path: Path,
    branch_name: str = "",
    commit_shas: list[str] | None = None,
    file_paths: list[str] | None = None,
    session_start: datetime | None = None,
    session_end: datetime | None = None,
    branch_merged: bool | None = None,
    exclude_shas: list[str] | None = None,
) -> DurabilityReport | None:
    """Trace git outcomes for a session's commits.
    
    Strategy:
    1. Find commits from explicit SHAs
    2. Find commits from branch name
    3. For each commit's files: measure line survival via git blame
    4. Categorize losses via subsequent commit messages
    """
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        return None

    report = DurabilityReport()

    # Find commits
    commits = _find_commits(repo, branch_name, commit_shas or [], session_start, session_end)
    if not commits:
        return None

    # Check if branch was merged
    if branch_merged is not None:
        report.branch_merged = branch_merged
    else:
        report.branch_merged = _check_merged(repo, branch_name, commits)

    # Determine files to analyze
    all_files = set(file_paths or [])
    for sha in commits:
        all_files.update(_get_changed_files(repo, sha))

    # Filter to files that were actually in the commits
    commit_files = set()
    for sha in commits:
        commit_files.update(_get_changed_files(repo, sha))
    if file_paths:
        all_files = commit_files | set(file_paths)
    else:
        all_files = commit_files

    # Analyze each file
    bug_commits = set()
    for filepath in sorted(all_files):
        # Merge source commits + exclude_shas for skipping in loss categorization
        all_exclude = list(dict.fromkeys((exclude_shas or []) + commits))
        fc = _analyze_file(repo, filepath, commits, all_exclude)
        if fc and fc.lines_added > 0:
            report.files.append(fc)
            report.total_lines_added += fc.lines_added
            report.total_lines_surviving += fc.lines_surviving

            lost = fc.lines_added - fc.lines_surviving
            if lost > 0:
                cat = fc.loss_category
                if cat == "bug_fix":
                    report.lines_lost_to_bugs += lost
                    if fc.loss_commit:
                        bug_commits.add(fc.loss_commit)
                elif cat == "architecture":
                    report.lines_lost_to_architecture += lost
                elif cat == "feature_evolution":
                    report.lines_lost_to_evolution += lost
                elif cat == "refactor":
                    report.lines_lost_to_refactor += lost
                elif cat == "maintenance":
                    report.lines_lost_to_maintenance += lost

    report.bug_count = len(bug_commits)

    # Compute percentages
    if report.total_lines_added > 0:
        report.raw_survival_pct = report.total_lines_surviving / report.total_lines_added

        adjusted_total = report.total_lines_added - report.lines_lost_to_architecture
        if adjusted_total > 0:
            report.adjusted_survival_pct = min(1.0, report.total_lines_surviving / adjusted_total)
        else:
            report.adjusted_survival_pct = 1.0

        report.waste_pct = report.lines_lost_to_architecture / report.total_lines_added

    return report


def _git(repo: Path, cmd: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        f"git {cmd}",
        shell=True,
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _find_commits(
    repo: Path,
    branch_name: str,
    explicit_shas: list[str],
    session_start: datetime | None,
    session_end: datetime | None,
) -> list[str]:
    """Find commits attributable to this session."""
    commits = []

    # 1. Explicit SHAs
    for sha in explicit_shas:
        result = _git(repo, f"cat-file -t {sha}")
        if result == "commit":
            commits.append(sha)

    if commits:
        return list(dict.fromkeys(commits))

    # 2. Branch commits (not yet on main)
    if branch_name:
        log = _git(repo, f"log --format=%H {branch_name} --not main 2>/dev/null")
        if log:
            commits.extend(log.split("\n"))

    if commits:
        return list(dict.fromkeys(commits))

    # 3. Branch already merged — find via merge commit
    if branch_name:
        merge_log = _git(repo, f'log --merges --format=%H --all --grep="{branch_name}"')
        if merge_log:
            merge_sha = merge_log.split("\n")[0]
            # Get the non-main parent of the merge
            parents = _git(repo, f"rev-list --parents -1 {merge_sha}").split()
            if len(parents) >= 3:
                # parents[0] = merge commit, parents[1] = main parent, parents[2] = feature parent
                feature_tip = parents[2]
                # Get commits from feature tip that were introduced
                ancestor = _git(repo, f"merge-base {parents[1]} {feature_tip}")
                if ancestor:
                    log = _git(repo, f"log --format=%H {ancestor}..{feature_tip}")
                    if log:
                        commits.extend(log.split("\n"))

    if commits:
        return list(dict.fromkeys(commits))

    # 4. Timestamp-based fallback
    if session_start and session_end:
        after = session_start.strftime("%Y-%m-%dT%H:%M:%S")
        before = session_end.strftime("%Y-%m-%dT%H:%M:%S")
        log = _git(repo, f'log --format=%H --after="{after}" --before="{before}" --all')
        if log:
            commits.extend(log.split("\n"))

    return list(dict.fromkeys(commits))


def _check_merged(repo: Path, branch_name: str, commits: list[str]) -> bool:
    """Check if the branch/commits were merged into main."""
    if branch_name:
        # Check merge commits
        merges = _git(repo, f'log --merges --all --oneline --grep="{branch_name}"')
        if merges:
            return True
        # Check if branch is in merged list
        merged = _git(repo, "branch --merged main")
        if branch_name in merged:
            return True

    # Check if any commit is reachable from main
    for sha in commits[:3]:  # Check first few
        result = _git(repo, f"branch --contains {sha}")
        if "main" in result or "master" in result:
            return True

    return False


def _get_changed_files(repo: Path, sha: str) -> list[str]:
    """Get files changed in a commit."""
    output = _git(repo, f"diff-tree --no-commit-id --name-only -r {sha}")
    return [f for f in output.split("\n") if f.strip()]


def _analyze_file(repo: Path, filepath: str, source_commits: list[str], exclude_commits: list[str] | None = None) -> FileChange | None:
    """Analyze a file's line survival from source commits."""
    fc = FileChange(path=filepath)

    # Count lines added in source commits
    for sha in source_commits:
        diff = _git(repo, f"show {sha} -- {filepath}")
        if not diff:
            continue
        for line in diff.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                fc.lines_added += 1

    if fc.lines_added == 0:
        return None

    # Check if file still exists at HEAD
    ls_result = _git(repo, f"ls-tree HEAD -- {filepath}")
    fc.file_exists = bool(ls_result.strip())

    if not fc.file_exists:
        # File was deleted — all lines lost to architecture
        fc.lines_surviving = 0
        fc.loss_category = "architecture"
        # Find the deletion commit
        del_log = _git(repo, f'log --diff-filter=D --format="%H %s" -- {filepath}')
        if del_log:
            parts = del_log.split("\n")[0].split(" ", 1)
            fc.loss_commit = parts[0] if parts else None
            fc.loss_commit_msg = parts[1] if len(parts) > 1 else None
        return fc

    # File exists — count surviving lines via git blame
    # Include ALL related SHAs (source + merge commits from exclude list) for blame matching
    all_own_shas = list(dict.fromkeys(source_commits + (exclude_commits or [])))

    blame = _git(repo, f"blame --porcelain HEAD -- {filepath}")
    surviving = 0
    for line in blame.split("\n"):
        # Blame lines start with the commit SHA
        if line and len(line) >= 40 and line[0] != "\t":
            parts = line.split()
            if parts:
                blamed_sha = parts[0]
                # Check if this line was authored by one of our commits (including merges)
                for own_sha in all_own_shas:
                    if blamed_sha.startswith(own_sha[:8]) or own_sha.startswith(blamed_sha[:8]):
                        surviving += 1
                        break

    fc.lines_surviving = min(surviving, fc.lines_added)
    fc.survival_pct = fc.lines_surviving / fc.lines_added if fc.lines_added > 0 else 1.0

    # Find what changed our lines (loss categorization)
    if fc.lines_surviving < fc.lines_added:
        _categorize_loss(repo, filepath, exclude_commits or source_commits, fc)

    return fc


def _categorize_loss(repo: Path, filepath: str, source_commits: list[str], fc: FileChange):
    """Find commits after ours that modified this file and pick the earliest non-source one."""
    if not source_commits:
        return

    # Get the date of our commit
    date = _git(repo, f"log -1 --format=%aI {source_commits[0]}")
    if not date:
        return

    # Find subsequent commits that modified this file (chronological order)
    log = _git(
        repo,
        f'log --reverse --format="%H %s" --after="{date}" -- {filepath}'
    )
    if not log:
        return

    for line in log.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        sha = parts[0].strip('"')
        msg = parts[1] if len(parts) > 1 else ""

        # Skip our own commits
        if any(sha.startswith(s[:8]) or s.startswith(sha[:8]) for s in source_commits):
            continue

        fc.loss_commit = sha
        fc.loss_commit_msg = msg
        fc.loss_category = _categorize_commit_msg(msg, fc.file_exists)
        return


def _categorize_commit_msg(msg: str, file_exists: bool) -> str:
    """Categorize a commit message into a loss type."""
    if not file_exists:
        return "architecture"

    m = msg.lower().strip().strip('"')

    if m.startswith("fix:") or m.startswith("fix(") or "hotfix" in m:
        return "bug_fix"
    if m.startswith("feat:") or m.startswith("feat("):
        return "feature_evolution"
    if m.startswith("chore:") or m.startswith("chore(") or "refactor" in m or "rewrite" in m:
        return "refactor"
    if m.startswith("docs:") or m.startswith("test:"):
        return "maintenance"
    if m.startswith("perf:") or m.startswith("perf("):
        return "refactor"

    # Non-conventional commits: use heuristics
    if "bug" in m or "fix" in m and not "feature" in m:
        return "bug_fix"
    if "improve" in m or "enhance" in m or "add" in m or "implement" in m:
        return "feature_evolution"
    if "clean" in m or "remove" in m or "simplify" in m or "rename" in m:
        return "refactor"

    # Default: in a fast-moving codebase, most non-categorized changes are evolution
    return "feature_evolution"
