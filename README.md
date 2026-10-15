# papr-thinkr

A critical manuscript reviewer and revision co-author for scientists, built
especially for the moment the reviews come back. Point it at a paper's folder
(manuscript, reviewer letters, R/MATLAB/Python code, `.csv`/`.mat` data,
figures) and it will:

- **decode every reviewer comment**:
  - what they actually want, in plain words, and the worry behind it
  - what would satisfy them, and the possible readings when a comment is unclear
  - whether they **misread your paper**, quoting the passage that already
    answers them, with a one-sentence clarification so nobody misreads it again
  - a **calm version** of harsh comments
  - the actual text of every "line 123" they cite, taken from the PDF they read
- **triage each comment**: is the reviewer right, how much work is it (hours
  with a confidence), address / clarify / rebut, and a drafted rebuttal when
  pushback is justified
- **review the paper itself** like a panel of demanding referees, whether or not
  there are reviews yet:
  - section-by-section reading and five specialist perspectives
  - an audit of the code and data
  - **figures checked against the code that draws them**: captions vs R/MATLAB
    code, plus a look at the images for models that can see
- **turn your honest reaction into a professional reply** ("vent → reply")
- write it all up as a **Word report**, then help you edit the manuscript
  (diff, backup, undo) and assemble the response letter
- track **evidence for each revision action**, audit response claims against
  current sources, and export a **submission bundle** with remaining issues
- save manuscript versions and reuse completed model calls when reviewing again

It runs on **local models** (Ollama) or **cloud models**: the Claude Code, Codex and Gemini
command-line tools (which reuse your existing logins), or the Anthropic, OpenAI
and Gemini APIs.

## Quick start

```bash
python papr.py backends                              # what can run on this machine
python papr.py serve                                 # web UI at http://127.0.0.1:8765
python papr.py review --dir D:\papers\my-paper       # or straight from the command line
```

In the web UI, **+ New project → 📁 Fill everything from a paper folder…**.
Check what it found, choose the model, and **Create project**. The review runs
in the background, and the Word report appears under **Report (.docx)**.

Try it on the included demo paper. It is deliberately flawed, comes with a
decision letter full of the usual frustrations, and has R and MATLAB code that
disagrees with its figure captions:

```bash
python papr.py review --dir examples/demo-paper --backend ollama --depth standard
```

## What it does with reviewer comments

| You get | Why it helps |
|---|---|
| **In plain words** + *what's behind it* + *what would satisfy them* | Jargon and vague complaints turned into a concrete to-do list |
| **Possible readings** (with likelihoods) + a sentence for the letter stating how you read it | Answer an incomprehensible comment without guessing wrong |
| **Possible misreading**, quoting your manuscript, + a **clarifying edit** | "The reviewer didn't read it" becomes "we have clarified" |
| **Calm view** (toggle) | Read harsh comments as neutral requests; the original is one click away |
| **Line references resolved** from the submitted, line-numbered PDF | See what "line 38" says without opening anything |
| **Figure context**: a comment about "Fig. 2" shows the figure, its caption and the code that draws it | Check the reviewer's claim in seconds |
| **Triage**: reviewer right? address / partially / clarify / rebut; hours ± confidence | Know what to concede, what to push back on, and what it costs |
| **Reviewer profiles**: likely expertise, tone, how to answer them | Write the letter for the person reading it |
| **Conflicts and duplicates** between reviewers | Spot "shorten it" vs "expand it" before you start |
| **Vent → reply**: type what you really think, get the reply you should send | Keeps every real argument and drops the heat |
| **Quick wins** list | Clear the easy items first |
| **Revision checklist** | Break a request into actions, link source quotes and applied edits, and record the response passage |
| **Response claim audit** | Check claimed changes and unanswered subrequests against current evidence; unsupported claims stay flagged |
| **File versions** | Mark assessments, evidence and response drafts stale after source files change |

## From reviews to a submission

1. Select a comment. Its **Revision checklist** starts from the triage's
   required changes (or the decoded subrequests). Edit these actions or add
   missing subrequests yourself.
2. For each action, link an applied manuscript edit, or select a project file
   and paste an exact supporting passage. Mark the action complete and save.
   Quotes are checked against the actual file. A rebuttal can instead have
   a written rationale with the stance set to **rebut** or status **won't fix**.
3. Draft or write the response. Optionally link each action to its exact
   response paragraph. **Check response claims** asks the selected model to
   audit factual and completion claims and identify unanswered subrequests.
   Supporting quotes returned by the model are checked against the supplied
   sources; invented quotes cannot earn a supported result.
4. Open **Submission check**, then **Check all response claims**. The checklist
   flags open requests, missing evidence/responses, placeholders, stale audits,
   reverted or missing edits, and unanswered or unsupported claims. Downloads
   remain available while there are blockers, clearly labelled **DRAFT**.
5. **Download submission bundle (.zip)** exports the response letter in Word
   and Markdown, a change summary, submission review in Markdown/JSON, the
   evidence checklist, and a source-version manifest. Private author notes,
   chat, original source files and API keys are excluded. Review the bundle
   before submitting it.

Model audits assess the material they are given; they do not prove scientific
correctness or that an analysis was executed. When a manuscript exceeds the
model's context, the audit records its excerpt-coverage note and can return
**unclear**. Authors decide whether and when to submit.

### Versions and interrupted reviews

The **File versions** panel compares the current files with the latest saved
snapshot. Use **Refresh file versions** after editing outside the app, or
**Save snapshot** to record a version. Reviews, gap analyses, response drafts,
audits, and manuscript edits also record their input versions automatically.

Snapshots live in `projects/<slug>/snapshots/`. They contain immutable copies
of manuscript sources (including LaTeX components and bibliographies) and
SHA-256 manifests for configured reviewer letters, code, data, and figures.
Large datasets and images are hashed, not copied. Evidence quotes must be
rechecked and saved after their source changes, even if the quote still exists.
Projects created before version tracking show **unknown** until reassessed.

Completed model calls are saved atomically under
`projects/<slug>/review-cache/`. After cancellation or a server restart, rerun
the same review: matching calls are reused and missing calls continue. Gap
analysis saves each completed batch immediately; **Reassess this comment**
runs it for just the selected item. Cache keys include the model/backend,
endpoint, context options, prompts, schemas, image content and relevant source
versions. Changes to data invalidate the audit passes that use it while
unaffected manuscript critique calls can be reused. Failed and incomplete
batches are retried. **Ignore saved review calls** or CLI `--fresh` forces new
calls; also use this after changing a provider's default model externally.

## What it checks in the paper

- **Five referee perspectives**: methods and design, statistics, claims and
  significance, clarity and presentation, reproducibility. Each finding comes
  with severity, location, a verbatim quote (checked against your text), a
  suggested fix, effort in hours, and a confidence that it's a real problem.
- **Figures vs code.** For `.tex` (`\includegraphics`), `.docx` (embedded images
  or legends with separate files) and Markdown, it finds each figure and maps
  it to the script that saves it. It recognises R `ggsave()`/`pdf()`/`png()`/…,
  MATLAB `exportgraphics()`/`saveas()`/`print()` and Python `savefig()`. It
  then checks:
  - error bars: SD vs SEM vs CI, caption vs code
  - n in the caption vs rows the code drops
  - error bars not defined, bar charts that hide the data, red/green palettes
  - low-resolution rasters, missing image files, figures no script produces
  - a figure-referee model pass over caption + citing text + code + data
    summary + the image itself (for models with vision)
- **Code (R especially)**:
  - `setwd()` and hard-coded paths, `rm(list = ls())`, `install.packages` inside
    analysis scripts, `attach()`
  - package versions not recorded, random numbers without `set.seed()`, NAs
    dropped silently
  - several tests with no multiple-comparison correction, data files the code
    reads but you didn't provide, notebooks not run top-to-bottom
- **Data**: `.csv`/`.tsv`/`.xlsx`/`.json` column statistics, missingness,
  duplicate and malformed rows, possible personal identifiers. **MATLAB `.mat`**
  files (v5/v7 read with no dependencies; v7.3 needs `h5py`) are summarised per
  variable: class, size, statistics, NaNs, struct fields and cell contents.
- **Mechanics**:
  - leftover TODO/`??`, broken `\ref`/`\cite`, figure numbering, `p = 0.000`
  - p-values without effect sizes, overclaiming, very long sentences
  - missing ethics, limitations, data-availability or conflict-of-interest
    statements

The reviewer is deliberately **liberal with criticism**. To stay honest,
quotes that can't be found in your text are flagged *unverified*, and every
finding carries a confidence.

## Models and logins

| Backend | Kind | Notes |
|---|---|---|
| `ollama` | local | Auto-picks the best installed model (e.g. `qwen3.6:27b`, `gemma4:31b`). Vision-capable models also look at figures. |
| `claude-code` | cloud CLI | Uses your Claude Code login; run with tools off (read-only access to figure images only). |
| `codex` | cloud CLI | Uses your Codex login, which is shared with the ChatGPT/Codex VS Code extension; read-only sandbox. |
| `gemini` | cloud CLI | Needs a `GEMINI_API_KEY`: Google no longer accepts the free Code Assist sign-in for the CLI. |
| `anthropic-api`, `openai-api`, `gemini-api` | cloud API | API keys; `openai-api` can also point at a local LM Studio / llama.cpp server. |

Add keys in the app (backend pill → *API keys*) or with
`python papr.py keys set GEMINI_API_KEY`. They are stored outside the
repository. See **[docs/api-keys-and-logins.md](docs/api-keys-and-logins.md)**
for where to get keys, where they are stored, and what is sent where. Local
models send nothing anywhere.

## Command line

```bash
python papr.py serve [--port 8765] [--backend codex] [--model ...]
python papr.py discover D:\papers\my-paper                 # show what it finds
python papr.py review --dir D:\papers\my-paper --backend claude-code --out review.docx --json review.json
python papr.py review paper.docx --reviews decision.pdf --submitted-pdf submitted.pdf \
       --code analysis/ --data data/ --figures figs/ --journal IOVS --depth thorough
python papr.py review --project <slug> --backend codex     # re-review after revising (keeps your notes)
python papr.py review --project <slug> --fresh             # bypass completed-call checkpoints
python papr.py submission <slug>                          # evidence/readiness checks, no model call
python papr.py submission <slug> --check --backend ollama  # audit response claims, then check readiness
python papr.py submission <slug> --out submission.zip      # export, including unresolved issues
python papr.py batch papers.json                           # several papers (see examples/batch.json)
python papr.py report <slug> | letter <slug> | list | keys
```

`python -m papr_thinkr ...` works too. `--json` (or `--json -` for stdout)
writes every item, its triage and its decoding as JSON, for other programs to
use.

The `submission` command exits with **0** when no blockers remain, **2** when
the submission is still a draft, and **1** on an operational error. An export
can be produced for a draft without calling a model.

## Review depth and time

| Depth | What runs | Local 27B model | Cloud |
|---|---|---|---|
| quick | one whole-paper pass, code audit, figure passes, decoding and triage | ~5–15 min | ~2–4 min |
| standard | + section-by-section reading, de-duplication | ~15–45 min | ~3–8 min |
| thorough | + five specialist referees | ~30–90 min | ~5–15 min |

## Repository layout

```
papr.py                 the one entry point (python papr.py ...)
papr_thinkr/
  cli.py                command line
  settings.py           API keys (stored outside the repo)
  project.py            a project's settings and saved state (projects/<name>/)
  prompts.py            every prompt, referee persona and JSON schema
  llm/                  talking to models
    ollama.py             local models
    cli_agents.py         Claude Code / Codex / Gemini command-line tools
    cloud_apis.py         Anthropic / OpenAI / Gemini APIs
    registry.py           pick a backend by name; detect what's installed
  inputs/               reading what you provide
    discover.py           find manuscript, reviews, code, data, figures in a folder
    documents.py          .docx/.pdf/.tex/.md -> text; encoding-safe file I/O
    chunking.py           sections, retrieval, fitting a paper into context
    review_letters.py     decision letter -> individual comments
    line_refs.py          "line 123" -> the text the reviewer saw
    figures.py            figures, their captions, and the code that draws them
    code_and_data.py      code audit, data summaries
    matfile.py            MATLAB .mat reader
  review/               the reviewer
    reviewer.py           critique, figure, decode and triage passes; meta-review
    checks.py             deterministic manuscript and figure checks
    findings.py           merging duplicates, verifying quotes
    effort.py             effort estimates
  revision/             after the review
    patches.py            propose / apply / revert manuscript edits
    gap_analysis.py       what's addressed now?
    chat.py               co-author chat
    response_letter.py    response letter, vent -> reply
    checklist.py          author actions, source evidence and patch links
    provenance.py         content versions and manuscript snapshots
    cache.py              durable model-call checkpoints
    consistency.py        response claim and subrequest audits
    submission.py         readiness checks, change summary and ZIP export
  report/               the Word report (pandoc, or a built-in writer)
  web/                  the browser UI (server.py, jobs.py, static/)
docs/                   api-keys-and-logins.md
examples/               demo-paper/ (a deliberately flawed paper) and batch.json
tests/                  python -m unittest discover tests   (no model needed)
projects/               your projects (created at runtime, git-ignored)
```

## Requirements

- Python 3.9+, standard library only. Pillow, SciPy and h5py are used if
  present but are never required.
- At least one backend from the table above.
- `pandoc` to read `.docx` and write the report (a built-in `.docx` writer is
  the fallback). `pdftotext`/`pdftoppm` (poppler) for PDFs, line numbers and
  rendering PDF figures. Both come with TeX Live or Git for Windows.

## Your files are safe

The manuscript is written only when you click **Apply** or **Revert** on an
edit. A timestamped backup is taken first, and the original encoding and line
endings are kept. `.docx` and `.pdf` manuscripts are never modified: suggested
edits are shown for you to apply by hand. The server accepts only local
requests. Code is analysed, never executed.

## Limitations

- An AI reviewer can be confidently wrong. Use the confidence values and the
  *unverified quote* flags, and delete items you disagree with (✕).
- Figure images are seen only by vision-capable models (local `qwen3.6`/`gemma4`,
  Claude, Codex, and the cloud APIs). Others judge figures from captions and
  code.
- Effort estimates assume someone familiar with the work. They don't include
  journal turnaround.
