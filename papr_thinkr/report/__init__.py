"""Output documents: the review report (review_report.py) and the Markdown ->
.docx conversion it and the response letter use (word.py)."""
from .review_report import build_report_markdown, write_report
from .word import markdown_to_docx

__all__ = ["build_report_markdown", "write_report", "markdown_to_docx"]
