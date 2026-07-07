# papr-thinkr

A local, offline co-author and thinking partner for revising a manuscript in
response to peer review. It runs entirely on your machine against a local
[Ollama](https://ollama.com) model (default: `gemma3:27b-it-qat`) and edits
your real `.tex`/`.docx`/`.md` files in place, with confirmation and
automatic backups at every step.

It is **general-purpose**: it doesn't know anything about any specific paper.
Point it at any manuscript file and any combination of reviewer/editor
comment documents, and it builds a project workspace around them.

## What it does

- **Ingests** your manuscript (`.tex`, `.docx`, `.md`, `.txt`) and one or more
  reviewer/editor comment documents (`.docx`, `.pdf`, `.txt`, `.md`), using
  `pandoc` / `pdftotext` under the hood.
- **Extracts discrete comments** from a messy decision letter (editor +
  multiple reviewers mixed together) into a structured, trackable list, using
  the local LLM to segment prose into individual points, correctly attributed
  per reviewer.
- **Tracks status** per comment (unaddressed / partially addressed /
  addressed / unclear), and can run a **gap analysis** at any time: re-reads
  the current manuscript and judges, per comment, whether it's now addressed
  and what's still missing. This is a living, regenerable status check, not
  a static document that goes stale.
- **Proposes manuscript edits** as precise find/replace patches (the "find"
  text must match your file verbatim and uniquely, exactly like a careful
  text-editing tool), shows you a diff, and only writes to disk when you hit
  **Apply** — a timestamped backup is made first.
- **Co-author chat**: a free-form thinking-partner conversation, either about
  the whole project or focused on one specific comment, grounded in the
  actual manuscript text and actual comments (it's instructed not to invent
  data, citations, or reviewer quotes).
- **Drafts a point-by-point response-to-reviewers letter** from the current
  comment statuses and applied edits, exportable as Markdown.
- Lets you **delete spurious extracted items** (e.g. journal-portal
  boilerplate that got mistaken for a reviewer comment).
- Optionally takes an **existing gap/revision-plan document** you've already
  written, and uses it as grounding context (not ground truth) for the gap
  analysis and chat.

## Requirements

| Requirement | Why | Install |
|---|---|---|
| Python 3.9+ | Runs the server. Stdlib only — no `pip install` needed. | [python.org](https://www.python.org/downloads/) |
| [Ollama](https://ollama.com) running locally | Runs the local LLM. | `ollama serve` (or the Ollama desktop app) |
| A pulled model | Default is `gemma3:27b-it-qat`. Any Ollama chat model works via `--model`. | `ollama pull gemma3:27b-it-qat` |
| `pandoc` on PATH | Converts `.docx`/`.doc`/`.odt`/`.rtf` to text. | [pandoc.org/installing](https://pandoc.org/installing.html) |
| `pdftotext` on PATH | Converts `.pdf` to text (falls back to pandoc if missing). | Windows: comes with a TeX Live install, or install [poppler](https://github.com/oschwartz10612/poppler-windows). macOS: `brew install poppler`. Linux: `apt install poppler-utils`. |

No third-party Python packages are required. `requirements.txt` is present
and intentionally empty (see the file for details) — everything outside the
standard library is an external CLI/service invoked over subprocess or a
plain HTTP call.

## Setup

```bash
git clone https://github.com/<your-username>/papr-thinkr.git
cd papr-thinkr
```

That's it — there's no build step and no virtualenv/package install needed.
Just make sure Ollama, pandoc, and pdftotext are installed per the table
above, and that Ollama has the model pulled:

```bash
ollama pull gemma3:27b-it-qat
```

## Running it

```bash
python server.py
```

Then open **http://localhost:8765** in a browser. Options:

```bash
python server.py --port 8765 --model gemma3:27b-it-qat --ollama-host http://localhost:11434
```

| Flag | Default | Purpose |
|---|---|---|
| `--port` | `8765` | Local port to serve on |
| `--host` | `127.0.0.1` | Bind address (keep this local-only) |
| `--model` | `gemma3:27b-it-qat` | Which Ollama model to use |
| `--ollama-host` | `http://localhost:11434` | Where Ollama's API is listening |

## Using it

1. Click **+ New project**. Give it a name, optionally a title/journal (this
   shapes the co-author's tone), and:
   - the path to your manuscript file
   - one or more reviewer comment files, one per line, optionally labeled
     (`Reviewer letter: D:\path\to\comments.docx`) — use **Browse…** to pick
     files without typing full paths
   - optionally an existing gap/revision-plan document
2. On creation, comments are automatically extracted and listed on the left,
   grouped by reviewer. Delete any spurious items (e.g. boilerplate) with the
   ✕ on its card.
3. Click **Run gap analysis** to have the model assess each comment against
   the *current* manuscript text. This can take several minutes for a large
   decision letter, since each batch call includes the full manuscript text.
4. Click a comment to see detail: adjust its status, add private notes,
   **Propose manuscript edit** (review the diff, then Apply / Reject / Ask
   for revision), **Draft reviewer response**, or **Discuss in chat** to open
   a focused conversation about just that point.
5. When you're ready to submit, **Export response letter** assembles a
   point-by-point Markdown letter from every comment's current status and
   response text.

## Project data

Each project lives under `projects/<slug>/` (git-ignored — this is your
working data, not source code):

- `project.json` — which real files this project points at (never modified)
- `state.json` — comments, chat threads, patch history (human-readable JSON)
- `backups/` — a timestamped copy of your manuscript file made immediately
  before every applied edit

Your actual manuscript file is only ever touched when you click **Apply** on
a proposed patch. Nothing is written automatically.

## Architecture

```
server.py            stdlib HTTP server: REST API + serves web/
copilot/
  llm.py              Ollama REST client (chat, JSON-schema structured output with retry)
  ingest.py           file -> text (pandoc/pdftotext), manuscript chunking
  comments.py         raw letter text -> structured comment items (LLM + regex fallback)
  project.py          project workspace: config + state, JSON on disk
  patches.py          propose/validate/diff/apply find-replace edits, with backups
  analysis.py         gap analysis, co-author chat, response-letter drafting
  prompts.py          all prompt templates and JSON schemas in one place
web/
  index.html, app.js, style.css   vanilla-JS single page app, no build step
projects/             created at runtime, one folder per project (git-ignored)
```

## Known limitations

- Gap analysis across 100+ comments is slow on local hardware — each batch
  call includes up to ~20k characters of manuscript text, so expect it to
  take several minutes for a full decision letter. It's correct, just slow.
- Comment extraction can occasionally mis-file a stray boilerplate line
  (e.g. submission instructions) as a "comment" — use the ✕ delete button to
  prune these.
- This is a single-user, localhost-only tool. `--host` should not be pointed
  at a non-local interface without adding authentication.
