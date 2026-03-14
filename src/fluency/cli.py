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
    lines.append("# AI Engineering Durability Report")
    lines.append("")
    lines.append("Generated by the AI Fluency Profiler.")
    lines.append("")

    # Dataset
    total_sessions = len(all_sessions)
    shipped = sum(1 for s in all_sessions if s.commit_count > 0)
    dur_count = sum(1 for dr in all_dur.values() if dr.total_lines_added > 0)
    total_added = sum(dr.total_lines_added for dr in all_dur.values() if dr.total_lines_added > 0)
    total_surv = sum(dr.total_lines_surviving for dr in all_dur.values() if dr.total_lines_added > 0)
    total_bugs = sum(dr.lines_lost_to_bugs for dr in all_dur.values() if dr.total_lines_added > 0)
    total_bug_commits = sum(dr.bug_count for dr in all_dur.values() if dr.total_lines_added > 0)
    total_arch = sum(dr.lines_lost_to_architecture for dr in all_dur.values() if dr.total_lines_added > 0)
    total_evolve = sum(dr.lines_lost_to_evolution for dr in all_dur.values() if dr.total_lines_added > 0)

    lines.append("## Dataset")
    lines.append("")
    lines.append(f"| | |")
    lines.append(f"|---|---|")
    lines.append(f"| Sessions | {total_sessions} |")
    lines.append(f"| Sessions with commits | {shipped} ({shipped*100//total_sessions}%) |")
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

    # Behavioral signals
    shipped_sessions = [s for s in all_sessions if s.commit_count > 0]
    zero_sessions = [s for s in all_sessions if s.commit_count == 0]

    def _med(vals):
        s = sorted(vals)
        return s[len(s)//2] if s else 0

    if shipped_sessions and zero_sessions:
        lines.append("## Behavioral Signals")
        lines.append("")
        lines.append(f"| Signal | Shipped (n={len(shipped_sessions)}) | Zero-commit (n={len(zero_sessions)}) |")
        lines.append("|---|---:|---:|")
        lines.append(f"| Active minutes | {_med([s.active_min for s in shipped_sessions]):.0f} | {_med([s.active_min for s in zero_sessions]):.0f} |")
        lines.append(f"| Prompts | {_med([s.human_prompt_count for s in shipped_sessions])} | {_med([s.human_prompt_count for s in zero_sessions])} |")
        lines.append(f"| Tool calls | {_med([s.tool_call_count for s in shipped_sessions])} | {_med([s.tool_call_count for s in zero_sessions])} |")
        s_test = sum(1 for s in shipped_sessions if s.test_arc)*100//len(shipped_sessions)
        z_test = sum(1 for s in zero_sessions if s.test_arc)*100//len(zero_sessions)
        lines.append(f"| Has tests | {s_test}% | {z_test}% |")
        s_sub = sum(1 for s in shipped_sessions if s.has_subagents)*100//len(shipped_sessions)
        z_sub = sum(1 for s in zero_sessions if s.has_subagents)*100//len(zero_sessions)
        lines.append(f"| Has subagents | {s_sub}% | {z_sub}% |")
        lines.append("")

    # Session shapes
    from collections import Counter
    shape_counts = Counter(s.session_shape for s in all_sessions)
    lines.append("## Session Shapes")
    lines.append("")
    lines.append("| Shape | Sessions | Ship rate | Avg commits |")
    lines.append("|---|---:|---:|---:|")
    for shape, count in shape_counts.most_common():
        shape_sessions = [s for s in all_sessions if s.session_shape == shape]
        with_commits = [s for s in shape_sessions if s.commit_count > 0]
        ship_rate = len(with_commits) * 100 // count if count else 0
        avg_c = sum(s.commit_count for s in with_commits) / len(with_commits) if with_commits else 0
        avg_str = f"{avg_c:.1f}" if with_commits else "—"
        lines.append(f"| {shape} | {count} ({count*100//total_sessions}%) | {ship_rate}% | {avg_str} |")
    lines.append("")

    # Chains
    multi_chains = [c for c in all_chains if len(c.sessions) > 1]
    if multi_chains:
        pattern_counts = Counter(c.pattern for c in multi_chains)
        lines.append("## Chains")
        lines.append("")
        lines.append("| Pattern | Count | Ship rate |")
        lines.append("|---|---:|---:|")
        for pattern, count in pattern_counts.most_common():
            pchains = [c for c in multi_chains if c.pattern == pattern]
            shipped_c = sum(1 for c in pchains if c.shipped)
            total_s = sum(len(c.sessions) for c in pchains)
            lines.append(f"| {pattern} | {count} ({total_s} sessions) | {shipped_c*100//count}% |")
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
