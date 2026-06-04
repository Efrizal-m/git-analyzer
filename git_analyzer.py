#!/usr/bin/env python3
"""Analyze recent git commits and produce an LLM-ready review prompt."""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


COMMIT_SEPARATOR = "\x1e"
FIELD_SEPARATOR = "\x1f"


@dataclass
class FileChange:
    path: str
    insertions: int
    deletions: int


@dataclass
class Commit:
    sha: str
    author: str
    date: dt.datetime
    subject: str
    body: str
    files: list[FileChange]
    stat: str

    @property
    def total_changes(self) -> int:
        return sum(change.insertions + change.deletions for change in self.files)


def run_git(repo: Path, args: list[str]) -> str:
    command = ["git", "-C", str(repo), *args]
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        raise SystemExit("Error: git is not installed or is not available on PATH.")
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "unknown git error"
        raise SystemExit(f"Error running git {' '.join(args)}: {message}")
    return result.stdout


def ensure_repo(repo: Path) -> None:
    if not repo.exists():
        raise SystemExit(f"Error: repo path does not exist: {repo}")
    output = run_git(repo, ["rev-parse", "--is-inside-work-tree"]).strip()
    if output != "true":
        raise SystemExit(f"Error: not a git repository: {repo}")


def parse_git_date(value: str) -> dt.datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return dt.datetime.fromisoformat(normalized)


def parse_numstat(output: str) -> list[FileChange]:
    files: list[FileChange] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        inserted, deleted, path = parts[0], parts[1], parts[2]
        files.append(
            FileChange(
                path=path,
                insertions=0 if inserted == "-" else int(inserted),
                deletions=0 if deleted == "-" else int(deleted),
            )
        )
    return files


def collect_commits(repo: Path, since: str, max_diff_chars: int) -> list[Commit]:
    has_commits = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if has_commits.returncode != 0:
        return []

    log_format = (
        f"{COMMIT_SEPARATOR}%H{FIELD_SEPARATOR}%an <%ae>{FIELD_SEPARATOR}%aI"
        f"{FIELD_SEPARATOR}%s{FIELD_SEPARATOR}%b"
    )
    output = run_git(repo, ["log", f"--since={since}", f"--format={log_format}"])
    commits: list[Commit] = []

    for block in output.split(COMMIT_SEPARATOR):
        block = block.strip("\n")
        if not block:
            continue
        fields = block.split(FIELD_SEPARATOR, 4)
        if len(fields) != 5:
            continue

        sha, author, date_text, subject, body = fields
        numstat = run_git(repo, ["show", "--format=", "--numstat", sha])
        stat = run_git(repo, ["show", "--stat", "--format=", sha]).strip()
        if len(stat) > max_diff_chars:
            stat = stat[:max_diff_chars].rstrip() + "\n... [truncated]"

        commits.append(
            Commit(
                sha=sha,
                author=author,
                date=parse_git_date(date_text),
                subject=subject.strip(),
                body=body.strip(),
                files=parse_numstat(numstat),
                stat=stat,
            )
        )

    commits.sort(key=lambda commit: commit.date)
    return commits


def estimate_hours(
    commits: list[Commit],
    gap_cap_hours: float,
    isolated_commit_minutes: int,
) -> tuple[float, list[str]]:
    if not commits:
        return 0.0, ["No commits found; estimated hours are 0."]

    by_day: dict[dt.date, list[Commit]] = defaultdict(list)
    for commit in commits:
        by_day[commit.date.date()].append(commit)

    total_hours = 0.0
    notes: list[str] = []
    gap_cap = dt.timedelta(hours=gap_cap_hours)
    isolated_hours = isolated_commit_minutes / 60

    for day, day_commits in sorted(by_day.items()):
        day_commits.sort(key=lambda commit: commit.date)
        if len(day_commits) == 1:
            total_hours += isolated_hours
            notes.append(f"{day}: 1 commit, counted as {isolated_hours:.1f}h base effort.")
            continue

        day_hours = isolated_hours
        for previous, current in zip(day_commits, day_commits[1:]):
            gap = current.date - previous.date
            day_hours += min(gap, gap_cap).total_seconds() / 3600
        total_hours += day_hours
        notes.append(f"{day}: {len(day_commits)} commits, estimated {day_hours:.1f}h.")

    return round(total_hours, 1), notes


def analyze_quality(commits: list[Commit]) -> tuple[list[str], list[str]]:
    if not commits:
        return (
            ["No commits found in the selected window."],
            ["Create commits first, then rerun this script for a meaningful analysis."],
        )

    all_files = [change.path for commit in commits for change in commit.files]
    total_insertions = sum(change.insertions for commit in commits for change in commit.files)
    total_deletions = sum(change.deletions for commit in commits for change in commit.files)
    total_changes = total_insertions + total_deletions
    large_commits = [commit for commit in commits if commit.total_changes > 400]
    short_messages = [commit for commit in commits if len(commit.subject.split()) < 3]
    risky_files = [
        path
        for path in all_files
        if path.endswith((".lock", ".env", ".yml", ".yaml", ".json", ".toml", ".ini"))
        or "config" in path.lower()
    ]
    tests = [path for path in all_files if "test" in path.lower() or "spec" in path.lower()]
    docs = [path for path in all_files if path.lower().endswith((".md", ".rst", ".txt"))]

    findings = [
        f"Reviewed {len(commits)} commits touching {len(set(all_files))} files.",
        f"Total churn: +{total_insertions}/-{total_deletions} lines ({total_changes} changed).",
    ]

    if large_commits:
        findings.append(f"{len(large_commits)} large commit(s) may need closer review.")
    else:
        findings.append("Commit size looks manageable by the simple churn heuristic.")

    if risky_files:
        findings.append(f"Configuration or sensitive-impact files changed: {len(set(risky_files))}.")
    else:
        findings.append("No obvious configuration or sensitive-impact file changes detected.")

    if tests:
        findings.append(f"Test-related files changed: {len(set(tests))}.")
    else:
        findings.append("No test-related file changes detected in the commit stats.")

    if docs:
        findings.append(f"Documentation files changed: {len(set(docs))}.")

    recommendations: list[str] = []
    if large_commits:
        recommendations.append("Review large commits manually and consider smaller commits next time.")
    if short_messages:
        recommendations.append("Use more descriptive commit messages for better analysis accuracy.")
    if total_changes > 0 and not tests:
        recommendations.append("Confirm behavior manually or add tests for the changed code paths.")
    if not recommendations:
        recommendations.append("No major heuristic issues found; validate with tests and review context.")

    return findings, recommendations


def build_prompt(
    repo: Path,
    since: str,
    commits: list[Commit],
    estimated_hours: float,
    hour_notes: list[str],
    findings: list[str],
) -> str:
    lines = [
        "You are a senior engineer reviewing one developer's git commits.",
        "Analyze only the evidence below. Do not invent unstated implementation details.",
        "When estimating effort, explain uncertainty and treat commit timestamps as imperfect signals.",
        "",
        "Goals:",
        "1. Assess code quality from commit metadata, file changes, churn, and available stats.",
        "2. Estimate hours spent during the selected period.",
        "3. Identify risks, missing tests, and actionable improvements.",
        "",
        f"Repository: {repo}",
        f"Window: {since}",
        f"Commit count: {len(commits)}",
        f"Heuristic estimated hours: {estimated_hours:.1f}",
        "",
        "Hour-estimation method:",
        *[f"- {note}" for note in hour_notes],
        "",
        "Local heuristic findings:",
        *[f"- {finding}" for finding in findings],
        "",
        "Commits:",
    ]

    if not commits:
        lines.append("- No commits found in this window.")
        return "\n".join(lines)

    for commit in commits:
        lines.extend(
            [
                "",
                f"Commit {commit.sha[:12]}",
                f"Author: {commit.author}",
                f"Date: {commit.date.isoformat()}",
                f"Subject: {commit.subject}",
                f"Body: {commit.body or '[empty]'}",
                f"Files changed: {len(commit.files)}",
                f"Churn: {commit.total_changes} lines",
                "Changed files:",
            ]
        )
        for change in commit.files:
            lines.append(f"- {change.path}: +{change.insertions}/-{change.deletions}")
        lines.extend(["Stat summary:", commit.stat or "[no stat output]"])

    return "\n".join(lines)


def format_report(
    repo: Path,
    since: str,
    commits: list[Commit],
    estimated_hours: float,
    hour_notes: list[str],
    findings: list[str],
    recommendations: list[str],
    prompt: str,
    include_prompt: bool,
) -> str:
    lines = [
        "Weekly Git Commit Analyzer",
        "==========================",
        f"Repository: {repo}",
        f"Window: {since}",
        f"Commits found: {len(commits)}",
        f"Estimated hours: {estimated_hours:.1f} (heuristic)",
        "",
        "Code Quality Signals",
        "--------------------",
        *[f"- {finding}" for finding in findings],
        "",
        "Effort Estimate Notes",
        "---------------------",
        *[f"- {note}" for note in hour_notes],
        "",
        "Recommendations",
        "---------------",
        *[f"- {recommendation}" for recommendation in recommendations],
    ]

    if commits:
        authors = Counter(commit.author for commit in commits)
        lines.extend(["", "Commit Summary", "--------------"])
        for author, count in authors.most_common():
            lines.append(f"- {author}: {count} commit(s)")
        for commit in commits:
            lines.append(f"- {commit.date.date()} {commit.sha[:8]} {commit.subject}")
    else:
        lines.extend(["", "Commit Summary", "--------------", "- No commits found."])

    if include_prompt:
        lines.extend(["", "LLM Analysis Prompt", "-------------------", prompt])

    return "\n".join(lines) + "\n"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze git commits from the past week and generate an LLM-ready prompt."
    )
    parser.add_argument("--repo", default=".", help="Path to the git repository. Defaults to current directory.")
    parser.add_argument("--since", default="1 week ago", help='Git date window, e.g. "7 days ago".')
    parser.add_argument("--gap-cap-hours", type=float, default=2.0, help="Max counted gap between commits per day.")
    parser.add_argument(
        "--isolated-commit-minutes",
        type=int,
        default=30,
        help="Base effort for a day with a single commit.",
    )
    parser.add_argument(
        "--max-stat-chars",
        type=int,
        default=4000,
        help="Maximum stat text per commit included in the prompt.",
    )
    parser.add_argument("--prompt-out", help="Optional path to write the generated prompt.")
    parser.add_argument("--report-out", help="Optional path to write the full report.")
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Do not print the full LLM prompt in stdout report.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    ensure_repo(repo)

    commits = collect_commits(repo, args.since, args.max_stat_chars)
    estimated_hours, hour_notes = estimate_hours(
        commits,
        gap_cap_hours=args.gap_cap_hours,
        isolated_commit_minutes=args.isolated_commit_minutes,
    )
    findings, recommendations = analyze_quality(commits)
    prompt = build_prompt(repo, args.since, commits, estimated_hours, hour_notes, findings)
    report = format_report(
        repo,
        args.since,
        commits,
        estimated_hours,
        hour_notes,
        findings,
        recommendations,
        prompt,
        include_prompt=not args.no_prompt,
    )

    if args.prompt_out:
        write_text(Path(args.prompt_out), prompt + "\n")
    if args.report_out:
        write_text(Path(args.report_out), report)

    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
