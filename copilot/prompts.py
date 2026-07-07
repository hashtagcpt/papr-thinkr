"""Prompt templates used throughout papr-thinkr. Kept in one place so tone
and house-style rules are easy to tune without hunting through the codebase."""

# JSON Schemas for structured-output calls. Passing an explicit schema (rather
# than the bare format="json" mode) is what keeps Ollama from collapsing a
# requested array down to a single object when there's only one -- or even
# when there's several, sometimes -- which format="json" alone was observed
# to do with gemma3.
COMMENT_EXTRACTION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "reviewer": {"type": "string"},
            "order": {"type": "integer"},
            "location_ref": {"type": ["string", "null"]},
            "comment_text": {"type": "string"},
            "severity": {"type": "string", "enum": ["major", "minor", "editorial"]},
        },
        "required": ["reviewer", "comment_text"],
    },
}

GAP_ANALYSIS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "comment_id": {"type": "string"},
            "status": {"type": "string", "enum": ["addressed", "partially_addressed", "not_addressed", "unclear"]},
            "evidence": {"type": "string"},
            "suggested_action": {"type": "string"},
        },
        "required": ["comment_id", "status"],
    },
}

PATCH_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "find": {"type": ["string", "null"]},
        "replace": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["rationale"],
}

RESPONSE_LETTER_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "response_text": {"type": "string"},
    },
    "required": ["response_text"],
}


COAUTHOR_SYSTEM = """You are an experienced co-author and manuscript-revision thinking \
partner for a scientific paper undergoing peer review at journal: {journal}.

Manuscript title: {title}

You have access to the current manuscript text and the reviewer/editor comments \
listed below. Your job is to help the author (not replace them): reason carefully, \
be direct about weaknesses, and ground every suggestion in the actual manuscript \
text and actual reviewer comments provided -- never invent data, citations, or \
reviewer quotes that were not given to you.

When asked to draft manuscript text, write in the concise, results-forward style \
typical of the target journal, and do not editorialize beyond what the data support.

When you are unsure or the manuscript doesn't contain enough information to answer, \
say so plainly rather than guessing.
"""

COMMENT_EXTRACTION = """Below is the raw text of a journal decision letter, which \
contains an editor's comments and one or more reviewers' comments on a submitted \
manuscript. The comments are often unstructured prose mixing several distinct \
points together.

Your task: split this into a JSON array of individual, DISCRETE comment items -- \
one object per distinct point/question/request a reviewer or editor raised about \
the science, writing, or presentation of the manuscript. Do not merge unrelated \
points into one item, and do not split a single point across multiple items.

Ignore purely administrative/procedural boilerplate that carries no actionable \
feedback about the manuscript itself -- e.g. submission deadlines, portal links, \
instructions for uploading files, salutations/sign-offs. If a given stretch of text \
contains no substantive comment at all, contribute nothing for it. If the ENTIRE \
source text is boilerplate with no substantive comments, respond with an empty \
JSON array.

Each object must have exactly these keys:
- "reviewer": string label for who raised it, e.g. "Editor", "AE", "Reviewer 1", "Reviewer 2"
- "order": integer, the order the comment appears in the source text (starting at 1 per reviewer)
- "location_ref": string or null -- any line/figure/section reference the comment mentions \
verbatim (e.g. "line 110", "Figure 5", "Abstract"), else null
- "comment_text": string -- the verbatim or lightly cleaned-up text of the comment itself. \
Do not paraphrase away specifics.
- "severity": one of "major", "minor", "editorial" -- your best judgement of how \
substantive the point is

Respond with ONLY a JSON array, no markdown fences, no commentary.

SOURCE TEXT:
---
{source_text}
---
"""

GAP_ANALYSIS = """You are assessing how well a manuscript currently addresses a list \
of reviewer/editor comments. This is a status check, not a rewrite.

For EACH comment given below, decide:
- "status": one of "addressed", "partially_addressed", "not_addressed", "unclear"
- "evidence": a short quote or paraphrase of the manuscript text that supports your \
status judgement (or empty string if status is "not_addressed")
- "suggested_action": one concise, concrete sentence describing what the author \
should still do, if anything (empty string if fully addressed)

Respond with ONLY a JSON array of objects, one per input comment, in the same order, \
with keys: "comment_id", "status", "evidence", "suggested_action". No markdown fences.

CURRENT MANUSCRIPT TEXT:
---
{manuscript_text}
---
{existing_plan_block}
COMMENTS TO ASSESS:
---
{comments_block}
---
"""

PATCH_PROPOSAL = """You are proposing a concrete, minimal edit to the manuscript to \
address ONE reviewer comment. You must work like a precise text-editing tool: choose \
a "find" string that appears VERBATIM and EXACTLY ONCE in the manuscript excerpt \
below, and a "replace" string that is your proposed replacement text.

Rules:
- "find" must be copied character-for-character from the excerpt (including whitespace/punctuation), \
and must be the smallest span that unambiguously identifies the edit location -- do not \
quote the entire excerpt.
- Do not fabricate citations, numbers, or statistics that are not already present in \
the manuscript or comment. If new supporting text requires a citation you don't have, \
leave a placeholder like [CITATION NEEDED].
- Keep the author's voice and the journal's concise, data-forward style.
- If the comment cannot be addressed with a text edit (e.g. it requires new data/analysis), \
set "find" and "replace" to null and explain why in "rationale".

Respond with ONLY a JSON object, no markdown fences, with keys:
"find" (string or null), "replace" (string or null), "rationale" (string, 1-3 sentences).

REVIEWER COMMENT:
---
{comment_text}
---

MANUSCRIPT EXCERPT (section: {section_heading}):
---
{excerpt}
---
"""

RESPONSE_LETTER_ITEM = """Draft a point-by-point response-to-reviewer entry for the \
comment below, for inclusion in a formal response letter to the journal editor.

Be specific about what was changed, quoting the actual replacement text given below \
if there is one -- never write a placeholder like "[insert revised text here]"; if you \
don't have the exact wording, describe the change in your own words instead of \
inventing a quote. If the comment was not addressed (status: not_addressed) or the \
author chose not to make a change, write a respectful, substantive explanation of why, \
using only the rationale/notes provided -- do not invent justifications.

Respond with ONLY a JSON object with keys "response_text" (string, 2-6 sentences, \
formal peer-review register).

COMMENT (from {reviewer}):
---
{comment_text}
---

STATUS: {status}
LINKED MANUSCRIPT CHANGES: {change_summary}
AUTHOR NOTES: {author_notes}
"""
