"""The reviewer: critique passes and triage of reviewer comments
(reviewer.py), deterministic checks (checks.py), merging findings
(findings.py) and effort estimates (effort.py)."""
from .effort import describe_hours
from .reviewer import DEPTHS, Reviewer, run_review

__all__ = ["DEPTHS", "Reviewer", "run_review", "describe_hours"]
