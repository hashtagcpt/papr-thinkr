"""papr-thinkr: a critical manuscript reviewer and revision co-author.

  cli.py       the command line (`python papr.py ...` / `python -m papr_thinkr ...`)
  llm/         talking to models: Ollama, Claude Code / Codex / Gemini CLIs, cloud APIs
  inputs/      reading what you provide: manuscript, reviewer letters, code and data
  review/      the reviewer: critique, triage of reviewer comments, checks, effort estimates
  revision/    after the review: manuscript edits, gap analysis, chat, response letter
  report/      the Word report
  web/         the browser UI and its REST API
  project.py   a project's settings and saved state (projects/<name>/)
  prompts.py   every prompt, referee persona and JSON schema
"""
__version__ = "2.0.0"
