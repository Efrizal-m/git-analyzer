# Weekly Git Commit Analyzer

A small dependency-free CLI that analyzes git commits from the past week, estimates work hours, reports simple code-quality signals, and generates an LLM-ready prompt for deeper review.

## Usage

```bash
python3 git_analyzer.py
```

Useful options:

```bash
python3 git_analyzer.py --repo /path/to/repo
python3 git_analyzer.py --since "7 days ago"
python3 git_analyzer.py --prompt-out weekly_prompt.txt --report-out weekly_report.txt
python3 git_analyzer.py --no-prompt
```

## What It Analyzes

- Commits in the selected git date window.
- Changed files, insertions, deletions, and commit stats.
- Approximate hours based on commit timing.
- Code-quality signals such as churn, large commits, test/docs changes, risky config changes, and commit message clarity.
- A tuned prompt that asks an LLM to analyze only the available evidence and avoid overclaiming.

## Assumptions

- This MVP uses local git history instead of the GitHub API, so it does not need network access or authentication.
- Hour estimation is approximate. By default, a single-commit day counts as 30 minutes, and gaps between same-day commits are capped at 2 hours.
- Code-quality analysis is heuristic and should be treated as a starting point for human or LLM review.

## Checks

```bash
python3 -m py_compile git_analyzer.py
python3 git_analyzer.py
```
