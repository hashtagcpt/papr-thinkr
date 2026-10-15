"""Prompt templates and JSON schemas used throughout papr-thinkr. Kept in one
place so tone and house-style rules are easy to tune without hunting through
the codebase.

Prompt layout convention: long shared material (the manuscript) goes FIRST
and is byte-identical across the calls of one review, with the per-call task
after it. Local Ollama models then reuse the cached prompt prefix instead of
re-reading a 30k-token paper on every pass."""

# ---------------------------------------------------------------------------
# Shared vocabularies
# ---------------------------------------------------------------------------

SEVERITIES = ["critical", "major", "minor", "editorial"]
CATEGORIES = [
    "design", "methods", "statistics", "results", "interpretation", "novelty",
    "literature", "clarity", "structure", "figures_tables", "reproducibility",
    "code", "data", "ethics", "mechanics", "other",
]
EFFORT_LEVELS = ["trivial", "small", "moderate", "large", "very_large"]
EFFORT_SCALE_TEXT = (
    "trivial (< 1 h: wording, typo, a reference), small (1-4 h: rewrite a paragraph, "
    "relabel a figure, add citations), moderate (4-16 h: re-run an existing analysis, add a "
    "supplementary figure, restructure a section), large (16-80 h: new analyses of existing "
    "data, substantial rewriting), very_large (80+ h: new experiments or data collection)"
)

# ---------------------------------------------------------------------------
# JSON Schemas. Passing an explicit schema (rather than bare JSON mode) is what
# keeps Ollama from collapsing a requested array into a single object. Only
# portable keywords (type/properties/items/enum/required) are used so the same
# schema works for Ollama, OpenAI strict mode, Gemini and Claude.
# ---------------------------------------------------------------------------

COMMENT_EXTRACTION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "reviewer": {"type": "string"},
            "location_ref": {"type": ["string", "null"]},
            "comment_text": {"type": "string"},
            "severity": {"type": "string", "enum": ["major", "minor", "editorial"]},
        },
        "required": ["reviewer", "comment_text", "severity"],
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
        "required": ["comment_id", "status", "evidence", "suggested_action"],
    },
}

PATCH_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "find": {"type": ["string", "null"]},
        "replace": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["find", "replace", "rationale"],
}

RESPONSE_LETTER_ITEM_SCHEMA = {
    "type": "object",
    "properties": {"response_text": {"type": "string"}},
    "required": ["response_text"],
}

_EFFORT_PROPS = {
    "effort_level": {"type": "string", "enum": EFFORT_LEVELS},
    "hours_low": {"type": "number"},
    "hours_high": {"type": "number"},
    "needs_new_data": {"type": "boolean"},
    "effort_confidence": {"type": "number"},
}

CRITIQUE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "category": {"type": "string", "enum": CATEGORIES},
            "severity": {"type": "string", "enum": SEVERITIES},
            "location": {"type": ["string", "null"]},
            "quote": {"type": ["string", "null"]},
            "issue": {"type": "string"},
            "rationale": {"type": "string"},
            "suggestion": {"type": "string"},
            **_EFFORT_PROPS,
            "confidence": {"type": "number"},
        },
        "required": ["title", "category", "severity", "location", "quote", "issue", "rationale",
                     "suggestion", "effort_level", "hours_low", "hours_high", "needs_new_data",
                     "effort_confidence", "confidence"],
    },
}

TRIAGE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "comment_id": {"type": "string"},
            "category": {"type": "string", "enum": CATEGORIES},
            "agreement": {"type": "string", "enum": ["agree", "partially_agree", "disagree"]},
            "agreement_confidence": {"type": "number"},
            "stance": {"type": "string", "enum": ["address", "address_partially", "clarify", "rebut"]},
            "already_addressed": {"type": "string", "enum": ["yes", "partly", "no"]},
            "evidence": {"type": "string"},
            "required_changes": {"type": "array", "items": {"type": "string"}},
            **_EFFORT_PROPS,
            "rebuttal": {"type": "string"},
            "rebuttal_confidence": {"type": "number"},
            "rationale": {"type": "string"},
        },
        "required": ["comment_id", "category", "agreement", "agreement_confidence", "stance",
                     "already_addressed", "evidence", "required_changes", "effort_level",
                     "hours_low", "hours_high", "needs_new_data", "effort_confidence",
                     "rebuttal", "rebuttal_confidence", "rationale"],
    },
}

CROSS_COMMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "conflicts": {"type": "array", "items": {"type": "object", "properties": {
            "comment_ids": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
            "suggested_resolution": {"type": "string"},
        }, "required": ["comment_ids", "description", "suggested_resolution"]}},
        "duplicates": {"type": "array", "items": {"type": "object", "properties": {
            "comment_ids": {"type": "array", "items": {"type": "string"}},
            "theme": {"type": "string"},
        }, "required": ["comment_ids", "theme"]}},
        "themes": {"type": "array", "items": {"type": "object", "properties": {
            "theme": {"type": "string"},
            "comment_ids": {"type": "array", "items": {"type": "string"}},
        }, "required": ["theme", "comment_ids"]}},
        "reviewers": {"type": "array", "items": {"type": "object", "properties": {
            "reviewer": {"type": "string"},
            "expertise_guess": {"type": "string"},
            "main_concerns": {"type": "array", "items": {"type": "string"}},
            "tone": {"type": "string"},
            "how_to_respond": {"type": "string"},
        }, "required": ["reviewer", "expertise_guess", "main_concerns", "tone", "how_to_respond"]}},
    },
    "required": ["conflicts", "duplicates", "themes", "reviewers"],
}

DECODE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "comment_id": {"type": "string"},
            "plain_language": {"type": "string"},
            "underlying_concern": {"type": "string"},
            "what_would_satisfy": {"type": "array", "items": {"type": "string"}},
            "clarity": {"type": "string", "enum": ["clear", "somewhat_unclear", "unclear"]},
            "interpretations": {"type": "array", "items": {"type": "object", "properties": {
                "reading": {"type": "string"}, "likelihood": {"type": "number"}},
                "required": ["reading", "likelihood"]}},
            "misunderstanding": {"type": "string", "enum": ["none", "missed_existing_text", "misread",
                                                            "terminology", "wrong_premise", "different_field_norms"]},
            "misunderstanding_explanation": {"type": "string"},
            "manuscript_evidence": {"type": "string"},
            "evidence_location": {"type": "string"},
            "clarifying_edit": {"type": "string"},
            "tone": {"type": "string", "enum": ["constructive", "neutral", "curt", "hostile"]},
            "neutral_rewrite": {"type": "string"},
            "clarification_request": {"type": "string"},
        },
        "required": ["comment_id", "plain_language", "underlying_concern", "what_would_satisfy", "clarity",
                     "interpretations", "misunderstanding", "misunderstanding_explanation", "manuscript_evidence",
                     "evidence_location", "clarifying_edit", "tone", "neutral_rewrite", "clarification_request"],
    },
}

VENT_SCHEMA = {
    "type": "object",
    "properties": {
        "response_text": {"type": "string"},
        "manuscript_change": {"type": "string"},
        "notes_for_author": {"type": "string"},
    },
    "required": ["response_text", "manuscript_change", "notes_for_author"],
}

DEDUP_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
    },
    "required": ["groups"],
}

META_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "key_weaknesses": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string", "enum": ["accept", "minor_revision", "major_revision", "reject"]},
        "recommendation_confidence": {"type": "number"},
        "effort_level": {"type": "string", "enum": ["light", "moderate", "substantial", "extensive"]},
        "effort_hours_low": {"type": "number"},
        "effort_hours_high": {"type": "number"},
        "effort_confidence": {"type": "number"},
        "effort_rationale": {"type": "string"},
        "top_priorities": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "why": {"type": "string"},
            "item_ids": {"type": "array", "items": {"type": "string"}},
        }, "required": ["title", "why", "item_ids"]}},
        "rebuttal_strategy": {"type": "string"},
        "likely_outcome": {"type": "string"},
    },
    "required": ["summary", "strengths", "key_weaknesses", "recommendation",
                 "recommendation_confidence", "effort_level", "effort_hours_low",
                 "effort_hours_high", "effort_confidence", "effort_rationale",
                 "top_priorities", "rebuttal_strategy", "likely_outcome"],
}

# ---------------------------------------------------------------------------
# Co-author chat
# ---------------------------------------------------------------------------

COAUTHOR_SYSTEM = """You are an experienced co-author and manuscript-revision thinking \
partner for a scientific paper {venue_clause}.

Manuscript title: {title}

You have access to the current manuscript text and the reviewer/editor comments \
listed below. Your job is to help the author (not replace them): reason carefully, \
be direct about weaknesses, and ground every suggestion in the actual manuscript \
text and actual reviewer comments provided -- never invent data, citations, or \
reviewer quotes that were not given to you. When a reviewer is wrong, say so and \
explain how to push back respectfully; when they are right, say that too.

When asked to draft manuscript text, write in the concise, results-forward style \
typical of the target journal, and do not editorialize beyond what the data support.

When you are unsure or the manuscript doesn't contain enough information to answer, \
say so plainly rather than guessing.
"""

# ---------------------------------------------------------------------------
# Comment extraction / gap analysis / patches / response letter
# ---------------------------------------------------------------------------

COMMENT_EXTRACTION = """Below is the raw text of a journal decision letter or review, \
which contains an editor's comments and/or one or more reviewers' comments on a \
submitted manuscript. The comments are often unstructured prose mixing several \
distinct points together.

Your task: split this into a JSON array of individual, DISCRETE comment items -- \
one object per distinct point/question/request a reviewer or editor raised about \
the science, writing, or presentation of the manuscript. Do not merge unrelated \
points into one item, and do not split a single point across multiple items.

Ignore purely administrative/procedural boilerplate that carries no actionable \
feedback about the manuscript itself -- e.g. submission deadlines, portal links, \
instructions for uploading files, salutations/sign-offs, and the reviewer's \
neutral summary of what the paper is about. If a stretch of text contains no \
substantive comment, contribute nothing for it. If the ENTIRE source text is \
boilerplate, respond with an empty JSON array.

Each object must have exactly these keys:
- "reviewer": string label for who raised it, e.g. "Editor", "Reviewer 1"
- "location_ref": string or null -- any line/figure/section reference the comment mentions \
verbatim (e.g. "line 110", "Figure 5", "Abstract"), else null
- "comment_text": string -- the verbatim or lightly cleaned-up text of the comment itself, \
INCLUDING any line/figure/section reference it starts with (e.g. keep "Line 24: retinoscopy \
..." intact). Do not paraphrase away specifics.
- "severity": one of "major", "minor", "editorial" -- your best judgement of how \
substantive the point is

Respond with ONLY a JSON array, no markdown fences, no commentary.

SOURCE TEXT:
---
{source_text}
---
"""

GAP_ANALYSIS = """CURRENT MANUSCRIPT TEXT:
---
{manuscript_text}
---
{existing_plan_block}
You are assessing how well the manuscript above currently addresses a list of \
reviewer/editor comments. This is a status check, not a rewrite. Be strict: a \
comment is "addressed" only if the manuscript text visibly resolves it.

For EACH comment below, decide:
- "status": one of "addressed", "partially_addressed", "not_addressed", "unclear"
- "evidence": a short quote from the manuscript text that supports your judgement \
(empty string if not_addressed)
- "suggested_action": one concise, concrete sentence describing what the author \
should still do (empty string if fully addressed)

Respond with ONLY a JSON array of objects, one per input comment, in the same order, \
with keys: "comment_id" (copy the id exactly as given in brackets), "status", \
"evidence", "suggested_action".

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
- "find" must be copied character-for-character from the excerpt (including whitespace/punctuation \
and any markup such as LaTeX commands), and must be the smallest span that unambiguously \
identifies the edit location -- typically one or two full sentences. Do not quote the entire excerpt.
- "replace" must be the complete replacement for "find" (keep any unchanged parts of the span).
- Do not fabricate citations, numbers, or statistics that are not already present in \
the manuscript or comment. If new supporting text requires a citation you don't have, \
leave a placeholder like [CITATION NEEDED]; if it requires a number you don't have, use [XX].
- Keep the author's voice and the journal's concise, data-forward style.
- If the comment cannot be addressed with a text edit (e.g. it requires new data/analysis), \
set "find" and "replace" to null and explain why in "rationale".
{extra_context}
Respond with ONLY a JSON object, no markdown fences, with keys:
"find" (string or null), "replace" (string or null), "rationale" (string, 1-3 sentences).

COMMENT TO ADDRESS:
---
{comment_text}
---

MANUSCRIPT EXCERPT (section: {section_heading}):
---
{excerpt}
---
"""

PATCH_REVISION = """You previously proposed this edit to address a reviewer comment:

find: {find}
replace: {replace}
rationale: {rationale}

The author gave this feedback: "{feedback}"

Revise the proposal accordingly. The same rules apply: "find" must be copied \
VERBATIM from the manuscript excerpt below and occur in it exactly once; do not \
invent citations or numbers (use [CITATION NEEDED] / [XX] placeholders).

Respond with ONLY a JSON object with keys "find" (string or null), "replace" \
(string or null), "rationale" (string).

COMMENT BEING ADDRESSED:
---
{comment_text}
---

MANUSCRIPT EXCERPT:
---
{excerpt}
---
"""

RESPONSE_LETTER_ITEM = """Draft a point-by-point response-to-reviewer entry for the \
comment below, for inclusion in a formal response letter to the journal editor.

Be specific about what was changed, quoting the actual replacement text given below \
if there is one -- never write a placeholder like "[insert revised text here]"; if you \
don't have the exact wording, describe the change in your own words instead of \
inventing a quote. Thank the reviewer briefly, but do not grovel.

If the planned stance is "rebut" or "clarify", respectfully but firmly explain why the \
requested change is not appropriate or rests on a misunderstanding, using ONLY the \
rationale, evidence and notes provided -- do not invent justifications, data or \
citations. If the stance is "address_partially", say what was done and why the rest \
was not. If the comment was not addressed and no rationale is given, write a short \
honest placeholder response the author can complete, marked [AUTHOR TO COMPLETE].

Respond with ONLY a JSON object with key "response_text" (string, 2-7 sentences, \
formal peer-review register).

COMMENT (from {reviewer}):
---
{comment_text}
---

STATUS: {status}
PLANNED STANCE: {stance}
LINKED MANUSCRIPT CHANGES: {change_summary}
TRIAGE NOTES (from the revision assessment): {triage_notes}
AUTHOR NOTES: {author_notes}
"""

# ---------------------------------------------------------------------------
# Reviewer (critique) prompts
# ---------------------------------------------------------------------------

REVIEWER_SYSTEM = """You are a demanding, meticulous senior peer reviewer \
{venue_clause}. You have reviewed hundreds of papers in {field} and you are known \
for catching what other reviewers miss.

Your stance: be LIBERAL with criticism. Raise every genuine weakness you can find -- \
fatal flaws, methodological gaps, statistical problems, overclaiming, missing \
controls, unclear writing, sloppy figures, inconsistent numbers, missing details \
needed to replicate, typos. Do not soften criticism, do not pad with praise, do not \
assume the authors "probably did it right" when the text doesn't show it.

But be RIGOROUS, not careless:
- Every criticism must be specific, located (section / figure / table / line), \
grounded in the actual text (quote it), and actionable.
- Keep distinct problems and corrective actions separate even when they affect \
the same sentence. Assess the science independently of journal reviewers' comments.
- Never invent content, numbers, citations or quotes. If something is missing, \
say it is missing -- don't pretend to quote it.
- Never invent references. If prior work should be cited, describe the kind of work \
(e.g. "prior studies of X in species Y") instead of fabricating an author/year.
- You see only the TEXT of the manuscript: figure and table images, supplementary files \
and journal formatting are not available to you. Never report figures as "missing" or \
"not provided"; judge figures only through their captions, legends and how the text \
describes and cites them.
- "confidence" (0-1) is how sure you are this is a real problem that a competent \
expert reviewer would also raise. Use the full range: 0.9+ for clear-cut errors, \
0.5-0.7 for judgement calls, below 0.4 for hunches you still think worth checking.
- "effort_confidence" (0-1) is how sure you are of the hours estimate.
"""

CRITIQUE_ITEM_SPEC = """Return a JSON array of critique items. Each item has keys:
- "title": short label, at most 12 words
- "category": one of """ + ", ".join(CATEGORIES) + """
- "severity": "critical" (invalidates a main conclusion or blocks publication), "major" \
(must be fixed for the paper to be convincing), "minor" (should be fixed; doesn't change \
conclusions), or "editorial" (wording, typos, formatting)
- "location": section / figure / table / equation / line reference, or null
- "quote": a SHORT verbatim excerpt (at most 40 words) from the manuscript the issue is \
about, copied exactly, or null if the issue is something missing
- "issue": what is wrong, concretely (1-4 sentences)
- "rationale": why it matters to an editor or reader (1-2 sentences)
- "suggestion": the concrete fix (1-3 sentences)
- "effort_level": one of """ + EFFORT_SCALE_TEXT + """
- "hours_low", "hours_high": your estimate of author working hours to fix it
- "needs_new_data": true only if fixing requires new experiments or data collection
- "effort_confidence": 0-1
- "confidence": 0-1 that this is a genuine problem

Be exhaustive: list every distinct problem (a whole paper typically yields 10-30 items \
per perspective; a short section fewer), each problem once. Never pad with non-issues \
or vague generic advice. Return [] only if you truly find nothing."""

MATERIAL_BLOCK = """MANUSCRIPT{title_clause}:
=== BEGIN MANUSCRIPT ===
{manuscript}
=== END MANUSCRIPT ===
{note}"""

PERSONAS = {
    "methods": {
        "name": "Methods & design",
        "focus": "You are reviewing as the METHODS AND STUDY-DESIGN expert. Scrutinise: whether the "
                 "design can answer the research question; controls and comparison groups; sample size "
                 "and power justification; randomisation, blinding and allocation; confounds and "
                 "alternative explanations built into the design; measurement validity and "
                 "reliability; inclusion/exclusion criteria and attrition; whether the methods are "
                 "described in enough detail to replicate (reagents, equipment, settings, versions, "
                 "parameters, timings); pre-registration and deviations from it.",
    },
    "statistics": {
        "name": "Statistics",
        "focus": "You are reviewing as the STATISTICAL referee. Scrutinise: choice of tests and models "
                 "and whether their assumptions hold; independence of observations (pseudo-replication, "
                 "repeated measures, nesting/clustering); multiple comparisons; effect sizes and "
                 "confidence intervals vs bare p-values; model specification, covariates, overfitting; "
                 "handling of outliers and missing data; signs of p-hacking or garden-of-forking-paths; "
                 "whether every reported statistic is complete (test, statistic, df, exact p, n); and "
                 "whether numbers are internally consistent across abstract, text, tables and figures "
                 "(check arithmetic: percentages, totals, degrees of freedom vs n).",
    },
    "claims": {
        "name": "Claims & significance",
        "focus": "You are reviewing as a SKEPTICAL DOMAIN EXPERT. Scrutinise: whether each conclusion "
                 "actually follows from the evidence shown; overclaiming and causal language from "
                 "correlational data; alternative explanations the authors did not rule out; "
                 "generalisability beyond the sample; novelty relative to prior work and whether key "
                 "prior work is missing (describe it, never invent citations); whether the abstract "
                 "and title promise more than the results deliver; and whether limitations are "
                 "honestly discussed.",
    },
    "presentation": {
        "name": "Clarity & presentation",
        "focus": "You are reviewing as the CLARITY AND PRESENTATION referee. Scrutinise: whether the "
                 "title and abstract accurately summarise the work; logical flow and structure; "
                 "undefined abbreviations and jargon; inconsistent terminology; figures and tables "
                 "(axes, units, legends, error bars defined, n stated, readability, redundancy with "
                 "text); paragraphs that are confusing, redundant or too long; grammar, typos and "
                 "formatting problems. Quote the offending text for every editorial item.",
    },
    "reproducibility": {
        "name": "Reproducibility & transparency",
        "focus": "You are reviewing as the REPRODUCIBILITY AND OPEN-SCIENCE referee. Scrutinise: data "
                 "and code availability statements; whether an independent lab could reproduce every "
                 "figure and number from what is described; software names and versions; parameter "
                 "values; preprocessing and exclusion steps; adherence to the relevant reporting "
                 "guideline (e.g. CONSORT, ARRIVE, STROBE, PRISMA, MIAME, TRIPOD) where applicable; "
                 "ethics approvals, consent and conflicts of interest statements.",
    },
}
DEFAULT_PERSONAS = ["methods", "statistics", "claims", "presentation", "reproducibility"]

CRITIQUE_WHOLE_TASK = """TASK: {focus}

Review the whole manuscript above from this perspective only (other referees cover the \
other perspectives).

""" + CRITIQUE_ITEM_SPEC

CRITIQUE_GENERAL_TASK = """TASK: Review the whole manuscript above as a complete, \
critical referee report would: design, methods, statistics, results, interpretation, \
novelty, clarity, figures/tables, reproducibility and ethics.

""" + CRITIQUE_ITEM_SPEC

CRITIQUE_SECTION_TASK = """TASK: Now do a close, line-by-line review of ONE part of the \
manuscript: "{heading}". Its text is repeated here for reference:
=== BEGIN SECTION ===
{section_text}
=== END SECTION ===

Criticise everything wrong in THIS part (all categories: content, logic, methods, \
statistics, claims, clarity, figures/tables referenced here, typos and wording), using the \
rest of the manuscript only as context (e.g. to catch inconsistencies with other sections). \
Do not repeat whole-paper generalities; be specific to this part.

""" + CRITIQUE_ITEM_SPEC

SECTION_CONTEXT_BLOCK = """The manuscript is too long to show in full here. Its outline and \
opening are below for context.

OUTLINE:
{outline}

OPENING (title/abstract):
{opening}
"""

CODE_AUDIT_TASK = """TASK: You are the COMPUTATIONAL REPRODUCIBILITY referee. Below is the \
analysis code and a summary of the data files the authors provided with the manuscript. \
Audit whether the code and data actually support the manuscript:
- mismatches between the methods described in the paper and what the code does (different \
models, tests, parameters, thresholds, covariates, exclusion rules, preprocessing)
- numbers in the paper that the code/data contradict (sample sizes vs data rows, group counts, \
reported statistics), and analyses or figures in the paper with no corresponding code
- bugs: wrong variable or column, off-by-one, data leakage between train/test, filtering \
mistakes, unit errors, hard-coded results, silent failure handling
- reproducibility gaps: missing files, absolute paths, unpinned dependencies, missing random \
seeds, manual steps, no instructions for running
- data issues: missing values not handled, implausible values, identifiable personal data
Quote code as file:line in "location" and copy the offending code in "quote". Use categories \
"code", "data", "reproducibility" or "statistics".

{dossier}

""" + CRITIQUE_ITEM_SPEC

TRIAGE_CONTEXT_BLOCK = """ALL REVIEWER/EDITOR COMMENTS ON THIS MANUSCRIPT (for cross-reference):
{all_comments}
"""

TRIAGE_TASK = """TASK: You are the authors' most experienced, candid co-author planning the \
revision. For EACH reviewer comment listed below, assess it on its merits against the \
manuscript above and plan the response. Be honest in both directions: most reviewer \
comments deserve to be addressed, but reviewers are sometimes wrong, and you must say so \
when they are.

Recommend "rebut" (respectful pushback without the requested change) ONLY when justified, \
e.g. the reviewer misread the paper and it already does what they ask (quote where), the \
premise is factually wrong, the request is outside the paper's scope, the requested \
analysis is impossible or invalid with these data, it contradicts another reviewer's \
request, or the cost is wildly out of proportion to the benefit. Use "clarify" when the \
reviewer misunderstood something that better wording would fix, and "address_partially" \
when part of the request is reasonable. Never recommend rebutting a valid point just \
because addressing it is a lot of work -- say it is a lot of work instead.

Some comments come with extra context: the text of the lines they cite in the submitted PDF, \
the figure they are about (caption and plotting code), and a first-pass decoding (plain-\
language reading, possible misunderstanding with manuscript evidence). Use it, but check it \
against the manuscript yourself. When a reviewer misread something, the best response is \
usually "clarify": a small edit that removes the ambiguity plus a gracious reply.

For each comment return:
- "comment_id": copy exactly as given in brackets
- "category": one of """ + ", ".join(CATEGORIES) + """
- "agreement": "agree", "partially_agree" or "disagree" -- is the reviewer right?
- "agreement_confidence": 0-1
- "stance": "address", "address_partially", "clarify" or "rebut"
- "already_addressed": "yes", "partly" or "no" -- does the CURRENT manuscript already satisfy it?
- "evidence": short verbatim quote(s) from the manuscript that bear on the comment (or "")
- "required_changes": list of concrete changes to make (empty for a pure rebuttal)
- "effort_level": one of """ + EFFORT_SCALE_TEXT + """ -- for the recommended stance
- "hours_low", "hours_high": author working hours for the recommended stance
- "needs_new_data": true only if new experiments/data collection are needed
- "effort_confidence": 0-1
- "rebuttal": for stance rebut/clarify/address_partially, the paragraph the authors would \
send back, written in the AUTHORS' voice and addressed to the reviewer ("We thank the \
reviewer ... however ..."): respectful, specific, evidence-based, quoting the manuscript \
where it already answers the point, with no invented data or citations. For stance \
"address" use ""
- "rebuttal_confidence": 0-1, how likely an editor accepts this pushback (0 if no rebuttal)
- "rationale": 1-3 sentences explaining your assessment

COMMENTS TO TRIAGE:
{batch}
"""

DECODE_TASK = """TASK: Reviewer comments are often terse, ambiguous, jargon-laden, irritated -- or \
based on a misreading of the paper. The authors want to know EXACTLY what each reviewer \
wants, explained calmly and charitably but without spin. For each comment below return:

- "comment_id": copy exactly as given in brackets
- "plain_language": what the reviewer is asking for, in 1-2 plain sentences a tired author \
can act on ("They want you to ..."). Translate jargon.
- "underlying_concern": the worry behind the comment -- what they suspect is wrong with the \
science or the presentation
- "what_would_satisfy": 1-4 concrete, checkable things that would make this reviewer tick \
the box (the minimum that would genuinely answer the comment)
- "clarity": "clear", "somewhat_unclear" or "unclear"
- "interpretations": when not perfectly clear, 2-3 plausible readings, each {{"reading", \
"likelihood" 0-1}}, most likely first; [] when clear
- "misunderstanding": does the comment rest on a misreading of the manuscript? "none", \
"missed_existing_text" (what they ask for is already there), "misread" (they misread what the \
text says), "terminology" (a term means something different to them), "wrong_premise" (a \
factual premise is incorrect), or "different_field_norms" (conventions from another field)
- "misunderstanding_explanation": 1-2 sentences, or ""
- "manuscript_evidence": text copied VERBATIM from the manuscript (at most 60 words) that shows \
the misunderstanding -- or, if there is none, the text the comment is about; "" if nothing
- "evidence_location": section / figure / line of that evidence, or ""
- "clarifying_edit": if other readers could plausibly misread the paper the same way, a short \
concrete edit (a sentence to add or a phrase to change) that prevents it; else "". A \
reviewer's misreading usually means the text could be clearer, and this edit lets the authors \
reply "we have clarified ..." instead of "you are wrong".
- "tone": "constructive", "neutral", "curt" or "hostile"
- "neutral_rewrite": the same comment as a calm, specific, professional request -- keep every \
substantive point, drop sarcasm and rhetoric
- "clarification_request": if the comment is unclear, one polite sentence for the response \
letter that states how the authors interpreted it ("We understood the reviewer to be asking \
whether ...; we have therefore ..."); else ""

Be accurate, not reassuring: if the reviewer is right, plain_language must make that obvious. \
Only claim a misunderstanding when the manuscript text you quote actually shows it. Where a \
comment cites line numbers, the text of those lines is given -- use it.

COMMENTS TO DECODE:
{batch}
"""

FIGURE_AUDIT_TASK = """TASK: You are the FIGURE referee. {image_note} Compare what the caption \
and the text claim with what the plotting code actually does{and_image}:
- does the code plot what the caption says (which subjects/eyes/groups/time points, which \
variable, transformations, filtering, n per group)?
- error bars, boxes and whiskers: what does the code compute (SD, SEM, CI, range) versus what \
the caption says?
- statistics shown or cited for this figure: are they computed correctly in the code (paired \
vs unpaired, pseudo-replication, multiple comparisons)?
- does the figure support the sentences that cite it?
- design: axis labels and units, legibility, colour-blind safety, chart type (bars hide the \
distribution when n is small), missing panel labels, resolution
Put the figure and code location in "location" (e.g. "Figure 2; make_figures.R:24") and the \
exact caption text or code line in "quote". Use category "figures_tables" unless the problem \
is really statistics, code or data.

{figure}

{data}

""" + CRITIQUE_ITEM_SPEC

VENT_TASK = """The author has just read this reviewer comment and written their honest, \
unfiltered reaction (only you will see it). Turn it into the reply they should actually send: \
professional, calm, specific, firm where the author is right, gracious where the reviewer has a \
point. Keep every legitimate argument from the reaction and support it with the manuscript \
(quote it); drop insults, sarcasm and anything that isn't an argument. Never invent data, \
analyses or citations: if the author's position needs something they haven't done, say so in \
"notes_for_author" instead of claiming it was done.

Respond with ONLY a JSON object:
- "response_text": the reply (3-8 sentences, formal response-letter register, authors' voice)
- "manuscript_change": a concrete edit that would let the reply say "we have clarified/revised \
..." (exact sentence to add or change), or "" if none is needed
- "notes_for_author": 1-3 sentences: what you kept, what you dropped and why, and any weakness \
in the author's position they should know about before sending

REVIEWER COMMENT ({reviewer}):
---
{comment}
---
{context}
AUTHOR'S RAW REACTION:
---
{vent}
---

RELEVANT MANUSCRIPT TEXT:
---
{excerpt}
---
"""

CROSS_COMMENT_TASK = """Below are all reviewer and editor comments on one manuscript, with ids.

Identify:
- "conflicts": pairs/groups of comments that ask for contradictory things (e.g. one reviewer \
wants a section shortened, another wants it expanded), with a suggested resolution
- "duplicates": groups of comments from different reviewers that raise essentially the same \
point (so one response can cover them)
- "themes": the main recurring themes across the reviews, each with its comment ids, most \
important first
- "reviewers": one profile per reviewer/editor: "reviewer" (as labelled), "expertise_guess" \
(their likely field/methods background, judged from what they focus on), "main_concerns" \
(their 1-4 core worries), "tone" (e.g. "constructive", "blunt but fair", "hostile"), and \
"how_to_respond" (2-3 sentences of practical advice: what this person needs to see in the \
response letter to be satisfied, and what to avoid)

Respond with ONLY a JSON object with keys "conflicts", "duplicates", "themes", "reviewers". \
Use [] when there are none. Copy ids exactly.

COMMENTS:
{comments}
"""

DEDUP_TASK = """Below is a numbered list of critique items produced by several independent \
reviewers of the same manuscript. Some describe the SAME underlying problem in different \
words. Group together the item numbers that are duplicates of each other (same problem, \
same place or same root cause). Do not group items that are merely related or in the same \
category. Only output groups with 2 or more items.

Respond with ONLY a JSON object: {{"groups": [[1, 7], [3, 12, 15]]}}

ITEMS:
{items}
"""

META_REVIEW_TASK = """You are the handling editor writing the summary assessment of a \
manuscript{venue_clause}. You have the manuscript's opening below and the consolidated \
findings of a panel of critical referees{revision_clause}.

OPENING OF THE MANUSCRIPT:
{opening}

CONSOLIDATED FINDINGS (id | severity | category | title | est. hours | confidence):
{items}

{triage_block}
BOTTOM-UP EFFORT ESTIMATE (sum over items): {bottom_up}

Write the overall assessment. Be candid and specific; do not soften real problems.
- "summary": 1-2 paragraphs on what the paper does, its main problems, and what it needs
- "strengths": genuine strengths (may be short)
- "key_weaknesses": the most important problems, most serious first
- "recommendation": what a referee would recommend for the CURRENT version: accept, \
minor_revision, major_revision or reject
- "recommendation_confidence": 0-1
- "effort_level": light (about a day), moderate (about a week), substantial (about a month), \
extensive (months) -- for the work needed to get this paper accepted
- "effort_hours_low", "effort_hours_high": your holistic estimate of total author hours. \
Account for overlap between items (one new analysis may fix several) -- this is usually less \
than the bottom-up sum -- and for writing/rechecking time.
- "effort_confidence": 0-1, how confident you are in the hours range
- "effort_rationale": 2-4 sentences explaining what drives the effort and the uncertainty
- "top_priorities": the 3-7 things to fix first, each with "title", "why" and the item ids
- "rebuttal_strategy": {rebuttal_strategy_hint}
- "likely_outcome": one sentence on the likely editorial outcome if the priorities are fixed
"""
