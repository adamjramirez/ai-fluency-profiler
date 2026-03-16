"""CLI for the AI Fluency Profiler."""

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from fluency.parser import parse_session
from fluency.connector import (
    guess_repo_path,
    link_session_to_git,
    build_git_index,
    batch_match_sessions,
    extract_commit_messages,
)
from fluency.git_tracer import trace_from_link, batch_trace, DurabilityReport
from fluency.sequence import detect_chains, SessionChain
from fluency.report import format_session_report, format_profile_report
from fluency.models import SessionAnalysis


@dataclass
class ProjectData:
    name: str
    sessions: list[SessionAnalysis] = field(default_factory=list)
    durability: dict[str, DurabilityReport] = field(default_factory=dict)
    chains: list[SessionChain] = field(default_factory=list)
    source_label: str = ""
    repo_path: Path | None = None


def _find_project_dirs() -> list[tuple[Path, str]]:
    """Find all session project directories across Claude Code and Pi."""
    dirs = []
    cc_base = Path.home() / ".claude" / "projects"
    if cc_base.exists():
        for d in sorted(cc_base.iterdir()):
            if d.is_dir():
                dirs.append((d, "cc"))
    pi_base = Path.home() / ".pi" / "agent" / "sessions"
    if pi_base.exists():
        for d in sorted(pi_base.iterdir()):
            if d.is_dir():
                dirs.append((d, "pi"))
    return dirs


def _normalize_project_name(dir_name: str) -> str:
    """Normalize project dir names across CC and Pi formats."""
    cleaned = dir_name.strip("-")
    cleaned = re.sub(r"Users-[^-]+-Code-", "", cleaned)
    cleaned = re.sub(r"Users-[^-]+-", "~/", cleaned)
    return cleaned or dir_name


def collect_all_data(use_git: bool = True, code_root: Path | None = None) -> list[ProjectData]:
    """Collect and analyze sessions from all discovered project directories."""
    if code_root is None:
        code_root = Path.home() / "Code"
    project_dirs = _find_project_dirs()
    if not project_dirs:
        return []

    project_groups = defaultdict(list)
    for dir_path, source in project_dirs:
        name = _normalize_project_name(dir_path.name)
        project_groups[name].append((dir_path, source))

    results = []

    for project_name, dirs in sorted(project_groups.items()):
        all_sessions = []
        repo_path = None

        for dir_path, source in dirs:
            if repo_path is None:
                repo_path = guess_repo_path(dir_path.name, code_root)
            session_files = sorted(dir_path.glob("*.jsonl"))
            session_files = [f for f in session_files if not f.name.startswith("agent-")]
            for f in session_files:
                sa = parse_session(f)
                if sa.human_prompt_count < 2:
                    continue
                all_sessions.append(sa)

        if not all_sessions:
            continue

        cc_count = sum(1 for s in all_sessions if s.source == "claude_code")
        pi_count = sum(1 for s in all_sessions if s.source == "pi")
        source_parts = []
        if cc_count: source_parts.append(f"{cc_count} CC")
        if pi_count: source_parts.append(f"{pi_count} Pi")

        pd = ProjectData(
            name=project_name,
            sessions=all_sessions,
            chains=detect_chains(all_sessions),
            source_label=" + ".join(source_parts),
            repo_path=repo_path,
        )

        # Git matching + durability
        if use_git and repo_path and (repo_path / ".git").exists():
            print(f"  Git: {repo_path.name}...", end="", flush=True, file=sys.stderr)
            git_idx = build_git_index(repo_path)

            sessions_with_msgs = [(sa, extract_commit_messages(sa))
                                  for sa in all_sessions if extract_commit_messages(sa)]
            if sessions_with_msgs:
                print(f" {len(sessions_with_msgs)} sessions...", end="", flush=True, file=sys.stderr)
                links = batch_match_sessions(sessions_with_msgs, git_idx)
                links_with_commits = [l for l in links if l.commits]
                if links_with_commits:
                    n_files = len(set(fp for l in links_with_commits for fp in l.file_paths))
                    print(f" {n_files} files...", end="", flush=True, file=sys.stderr)
                    pd.durability = batch_trace(links_with_commits, repo_path)
            print(" done", file=sys.stderr)

        results.append(pd)

    return results


def cmd_retro(args):
    """Run a single session retrospective."""
    session_path = Path(args.session)
    if not session_path.exists():
        print(f"Session file not found: {session_path}", file=sys.stderr)
        sys.exit(1)

    sa = parse_session(session_path)
    repo = Path(args.repo) if args.repo else None

    dr = None
    link = None
    if repo and sa.commit_commands:
        link = link_session_to_git(sa, repo)
        if link.commits:
            dr = trace_from_link(link)
            if dr:
                dr.branch_merged = link.branch_merged

    report = format_session_report(sa, dr)
    print(report)

    if link and link.commits:
        print(f"GIT MATCH: {len(link.commits)} commits in {link.repo_path}")
        for m in link.commits:
            tag = " [MERGE]" if m.is_merge else ""
            pr = f" PR#{m.pr_number}" if m.pr_number else ""
            print(f"  {m.commit_sha[:10]} {m.commit_msg[:80]}{tag}{pr}")


def cmd_scan(args):
    """Scan all sessions with git + chain analysis."""
    code_root = Path(args.code_root) if args.code_root else None
    projects = collect_all_data(use_git=not args.no_git, code_root=code_root)
    if not projects:
        print("No sessions found.", file=sys.stderr)
        sys.exit(1)

    all_data = []
    for pd in projects:
        report_data = [(sa, pd.durability.get(sa.id)) for sa in pd.sessions]
        report = format_profile_report(
            report_data,
            project_name=pd.name,
            chains=pd.chains,
            source_label=pd.source_label,
        )
        print(report)
        all_data.extend(report_data)

    if len(all_data) > 10:
        report = format_profile_report(
            all_data,
            project_name="ALL SESSIONS",
            source_label=f"{len(all_data)} total",
        )
        print(report)


def cmd_report(args):
    """Generate a full markdown durability report."""
    code_root = Path(args.code_root) if args.code_root else None
    projects = collect_all_data(use_git=not args.no_git, code_root=code_root)
    if not projects:
        print("No sessions found.", file=sys.stderr)
        sys.exit(1)

    # Collect aggregate stats
    all_sessions = []
    all_dur = {}
    project_rows = []
    all_chains = []

    for pd in projects:
        all_sessions.extend(pd.sessions)
        all_dur.update(pd.durability)
        all_chains.extend(pd.chains)

        # Project durability summary
        dur_sessions = [dr for sid, dr in pd.durability.items() if dr.total_lines_added > 0]
        if dur_sessions:
            ta = sum(dr.total_lines_added for dr in dur_sessions)
            ts = sum(dr.total_lines_surviving for dr in dur_sessions)
            tb = sum(dr.lines_lost_to_bugs for dr in dur_sessions)
            bc = sum(dr.bug_count for dr in dur_sessions)
            tarch = sum(dr.lines_lost_to_architecture for dr in dur_sessions)
            adj_d = ta - tarch
            raw = ts / ta * 100 if ta else 0
            adj = min(100, ts / adj_d * 100) if adj_d > 0 else 100
            bug_rate = tb / ta * 100 if ta else 0
            project_rows.append((pd.name, len(dur_sessions), ta, ts, raw, adj, bug_rate, bc))

    # Anonymize if requested
    name_map = {}
    if args.anonymize:
        labels = ["Project-SaaS", "Project-ML", "Project-Launch", "Project-Analytics",
                  "Project-Config", "Project-Intel", "Project-Infra", "Project-Alpha",
                  "Project-Beta", "Project-Gamma", "Project-Delta", "Project-Epsilon"]
        # Sort by lines added descending for consistent labeling
        sorted_names = [r[0] for r in sorted(project_rows, key=lambda x: -x[2])]
        for i, name in enumerate(sorted_names):
            name_map[name] = labels[i] if i < len(labels) else f"Project-{i+1}"

    def pname(n):
        return name_map.get(n, n)

    # Build report
    lines = []
    lines.append("# AI Coding Session Analysis")
    lines.append("")
    lines.append("Generated by the AI Fluency Profiler. Sessions evaluated against their")
    lines.append("inferred goal — see Methodology Notes at the bottom for known limitations.")
    lines.append("")

    # Dataset
    total_sessions = len(all_sessions)
    goal_counts = Counter(s.session_goal for s in all_sessions)
    dur_count = sum(1 for dr in all_dur.values() if dr.total_lines_added > 0)
    total_added = sum(dr.total_lines_added for dr in all_dur.values() if dr.total_lines_added > 0)
    total_surv = sum(dr.total_lines_surviving for dr in all_dur.values() if dr.total_lines_added > 0)
    total_bugs = sum(dr.lines_lost_to_bugs for dr in all_dur.values() if dr.total_lines_added > 0)
    total_bug_commits = sum(dr.bug_count for dr in all_dur.values() if dr.total_lines_added > 0)

    lines.append("## Dataset")
    lines.append("")
    lines.append(f"| | |")
    lines.append(f"|---|---|")
    lines.append(f"| Sessions | {total_sessions} |")
    goal_str = ", ".join(f"{c} {g}" for g, c in goal_counts.most_common())
    lines.append(f"| By goal | {goal_str} |")
    if dur_count:
        lines.append(f"| Sessions with durability data | {dur_count} |")
        lines.append(f"| Lines added | {total_added:,} |")
        lines.append(f"| Lines surviving | {total_surv:,} ({total_surv*100//total_added}%) |")
        lines.append(f"| Bug rate | {total_bugs/total_added*100:.1f}% ({total_bug_commits} fix commits) |")
    lines.append("")

    # Durability by project
    if project_rows:
        lines.append("## Durability by Project")
        lines.append("")
        lines.append("| Project | Sessions | Added | Surviving | Raw % | Adj % | Bug Rate | Bug Commits |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for name, sess, ta, ts, raw, adj, br, bc in sorted(project_rows, key=lambda x: -x[2]):
            lines.append(f"| {pname(name)} | {sess} | {ta:,} | {ts:,} | {raw:.0f}% | {adj:.0f}% | {br:.1f}% | {bc} |")
        lines.append("")

    # Behavioral signals — by goal
    def _med(vals):
        s = sorted(vals)
        return s[len(s)//2] if s else 0

    goal_groups = {}
    for s in all_sessions:
        goal_groups.setdefault(s.session_goal, []).append(s)
    displayable = [(g, slist) for g, slist in goal_groups.items() if len(slist) >= 3]

    if len(displayable) >= 2:
        lines.append("## Session Profiles by Goal")
        lines.append("")

        # Sort by count descending
        displayable.sort(key=lambda x: -len(x[1]))
        header = "| Signal |" + " | ".join(f" {g} (n={len(s)}) " for g, s in displayable) + " |"
        sep = "|---|" + "|".join("---:" for _ in displayable) + "|"
        lines.append(header)
        lines.append(sep)

        lines.append("| Active minutes |" + " | ".join(
            f" {_med([s.active_min for s in slist]):.0f} " for _, slist in displayable) + " |")
        lines.append("| Prompts |" + " | ".join(
            f" {_med([s.human_prompt_count for s in slist])} " for _, slist in displayable) + " |")
        lines.append("| Tool calls |" + " | ".join(
            f" {_med([s.tool_call_count for s in slist])} " for _, slist in displayable) + " |")
        lines.append("| Commits |" + " | ".join(
            f" {_med([s.commit_count for s in slist])} " for _, slist in displayable) + " |")
        lines.append("| Has tests |" + " | ".join(
            f" {sum(1 for s in slist if s.test_arc)*100//len(slist)}% " for _, slist in displayable) + " |")
        lines.append("| Has subagents |" + " | ".join(
            f" {sum(1 for s in slist if s.has_subagents)*100//len(slist)}% " for _, slist in displayable) + " |")
        lines.append("")

    # Session shapes with goal distribution
    from collections import Counter
    shape_counts = Counter(s.session_shape for s in all_sessions)
    lines.append("## Session Shapes")
    lines.append("")
    lines.append("| Shape | Sessions | Goal Distribution | With Commits |")
    lines.append("|---|---:|---|---:|")
    for shape, count in shape_counts.most_common():
        shape_sessions = [s for s in all_sessions if s.session_shape == shape]
        shape_goals = Counter(s.session_goal for s in shape_sessions)
        goal_dist = ", ".join(f"{c} {g}" for g, c in shape_goals.most_common())
        with_commits = sum(1 for s in shape_sessions if s.commit_count > 0)
        lines.append(f"| {shape} | {count} ({count*100//total_sessions}%) | {goal_dist} | {with_commits} |")
    lines.append("")

    # Chains
    multi_chains = [c for c in all_chains if len(c.sessions) > 1]
    if multi_chains:
        pattern_counts = Counter(c.pattern for c in multi_chains)
        lines.append("## Chains")
        lines.append("")
        lines.append("| Pattern | Count | Sessions | Has Commits | Note |")
        lines.append("|---|---:|---:|---:|---|")
        for pattern, count in pattern_counts.most_common():
            pchains = [c for c in multi_chains if c.pattern == pattern]
            with_commits_c = sum(1 for c in pchains if c.has_commits)
            total_s = sum(len(c.sessions) for c in pchains)
            note = ""
            if pattern == "thrashing":
                note = "⚠️ Ship goal, 0 output"
            elif pattern in ("investigation", "review_cycle", "exploration", "research", "planning"):
                note = "0 commits expected"
            lines.append(f"| {pattern} | {count} | {total_s} | {with_commits_c} | {note} |")
        lines.append("")

    # Methodology Notes
    lines.append("## Methodology Notes")
    lines.append("")
    lines.append("**Known limitations of this analysis:**")
    lines.append("")
    lines.append("- **Intent is inferred from the opening prompt.** This is imperfect — about 40% of sessions")
    lines.append("  classify as 'unknown' because the opening message is ambiguous or a continuation.")
    lines.append("- **Durability metrics favor well-scoped tasks.** Bugfix sessions naturally score higher on line")
    lines.append("  survival because the problem is well-defined. Feature design sessions may produce code that gets")
    lines.append("  refactored later — this is evolution, not failure.")
    lines.append("- **Quantitative metrics miss qualitative value.** A 10-line architectural insight that reshapes")
    lines.append("  the project scores worse than 500 lines of boilerplate. Line counts are not value counts.")
    lines.append("- **Pipeline sessions look worse individually.** Sessions in an explore → plan → execute pipeline")
    lines.append("  may show 0 commits individually but contribute to later shipped work.")
    lines.append("- **Zero commits ≠ failure.** Investigation, review, exploration, and planning sessions are")
    lines.append("  expected to produce understanding, not code.")
    lines.append("")

    output = "\n".join(lines)

    if args.output:
        Path(args.output).write_text(output)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(output)


def main():
    parser = argparse.ArgumentParser(description="AI Fluency Profiler")
    subparsers = parser.add_subparsers(dest="command")

    retro = subparsers.add_parser("retro", help="Single session retrospective")
    retro.add_argument("session", help="Path to session .jsonl file")
    retro.add_argument("--repo", help="Path to git repo")

    scan = subparsers.add_parser("scan", help="Scan all sessions with git + chains")
    scan.add_argument("--no-git", action="store_true", help="Skip git matching (fast mode)")
    scan.add_argument("--code-root", help="Root directory for git repos (default: ~/Code)")

    report = subparsers.add_parser("report", help="Generate markdown durability report")
    report.add_argument("--no-git", action="store_true", help="Skip git matching")
    report.add_argument("--anonymize", action="store_true", help="Replace project names")
    report.add_argument("--output", "-o", help="Write to file instead of stdout")
    report.add_argument("--code-root", help="Root directory for git repos (default: ~/Code)")

    args = parser.parse_args()

    if args.command == "retro":
        cmd_retro(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "report":
        cmd_report(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
