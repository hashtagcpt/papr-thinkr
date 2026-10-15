"""papr-thinkr command line (also reachable as `python -m papr_thinkr`).

  python papr.py serve                                    # web UI at http://127.0.0.1:8765
  python papr.py backends
  python papr.py review --dir D:\\papers\\my-paper           # find everything in the paper's folder
  python papr.py review paper.tex                         # first-draft critique
  python papr.py review paper.docx --reviews decision.pdf --code analysis/ --data data/ \\
                        --backend claude-code --out review.docx --json review.json
  python papr.py batch papers.json                        # several papers, one manifest
  python papr.py report <project-slug> [--out file.docx]  # re-export a report
  python papr.py letter <project-slug> [--out file.docx]  # response letter
  python papr.py list

Every review is also saved as a project under projects/, so you can open it in
the web UI (python papr.py serve) afterwards to propose edits, chat, and draft the
response letter. Exit status is 0 on success, 1 on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__, settings
from .inputs.discover import discover
from .inputs import documents, review_letters
from .llm import CallContext, LLMError, canonical_backend, detect_backends, make_llm, use_context
from .project import Project, ProjectNotFound
from .report import review_report
from .review import DEPTHS, describe_hours, run_review
from .revision import consistency, response_letter, submission


def _log(msg: str, frac: float | None = None) -> None:
    pct = f"{frac * 100:3.0f}% " if frac is not None else "     "
    print(f"[{time.strftime('%H:%M:%S')}] {pct}{msg}", file=sys.stderr, flush=True)


def _abs(p: str, base: Path | None = None) -> str:
    path = Path(os.path.expanduser(p))
    if not path.is_absolute() and base is not None:
        path = base / path
    return str(path.resolve())


def _run_cancellable(fn):
    """Run fn() in a worker thread so Ctrl+C cancels cleanly (the current
    model call is abandoned instead of waiting for every queued pass)."""
    ctx = CallContext(threading.Event())
    box: dict = {}

    def target():
        use_context(ctx)
        try:
            box["result"] = fn()
        except BaseException as e:  # re-raised in the main thread
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        _log("Cancelling (Ctrl+C again to force quit)...")
        ctx.cancel_event.set()
        t.join(60)
        raise
    if "error" in box:
        raise box["error"]
    return box.get("result"), ctx


def run_one(spec: dict, base_dir: Path | None = None) -> dict:
    """Review one paper. spec keys: manuscript (or dir), reviews, code, data,
    submitted_pdf, figures_dir, name, title, journal, field, backend, model,
    think, depth, independent, project, out, json, num_ctx, ollama_host, timeout."""
    if spec.get("project") and not spec.get("manuscript") and not spec.get("dir"):
        existing = Project.load(spec["project"])
        spec = dict(spec, manuscript=str(existing.manuscript_path()))
    existing = Project.load(spec["project"]) if spec.get("project") else None
    if spec.get("dir"):
        found = discover(_abs(spec["dir"], base_dir))
        _log(f"Found in {found['folder']}:")
        for k in ("manuscript", "reviews", "submitted_pdf", "code_paths", "data_paths", "figures_dir"):
            _log(f"  {k:<14} {found[k]}")
        for n in found["notes"]:
            _log(f"  note: {n}")
        spec = {**spec}
        for mine, theirs in (("manuscript", "manuscript"), ("reviews", "reviews"), ("code", "code_paths"),
                             ("data", "data_paths"), ("submitted_pdf", "submitted_pdf"), ("figures_dir", "figures_dir")):
            if not spec.get(mine) and found.get(theirs):
                spec[mine] = found[theirs]
    if not spec.get("manuscript"):
        raise SystemExit("No manuscript given (and none found with --dir).")
    manuscript = _abs(spec["manuscript"], base_dir)
    if not Path(manuscript).is_file():
        raise SystemExit(f"Manuscript not found: {manuscript}")
    reviews = [_abs(p, base_dir) for p in spec.get("reviews") or []]
    code = [_abs(p, base_dir) for p in spec.get("code") or []]
    data = [_abs(p, base_dir) for p in spec.get("data") or []]
    standards = [_abs(p, base_dir) for p in spec.get("standards") or []]
    if any(not Path(p).is_file() for p in standards):
        raise SystemExit("Field standards must be existing document files")
    for p in reviews:
        if not Path(p).is_file():
            raise SystemExit(f"Review file not found: {p}")
    for p in code + data:
        if not Path(p).exists():
            raise SystemExit(f"Code/data path not found: {p}")
    submitted_pdf = _abs(spec["submitted_pdf"], base_dir) if spec.get("submitted_pdf") else None
    figures_dir = _abs(spec["figures_dir"], base_dir) if spec.get("figures_dir") else None
    if submitted_pdf and not Path(submitted_pdf).is_file():
        raise SystemExit(f"Submitted PDF not found: {submitted_pdf}")

    saved_llm = (existing.config.get("llm") or {}) if existing else {}
    backend = canonical_backend(spec.get("backend") or saved_llm.get("backend") or os.environ.get("PAPR_BACKEND") or "ollama")
    think = {"on": True, "off": False, True: True, False: False}.get(spec.get("think"))
    if spec.get("think") is None:
        think = saved_llm.get("think")
    same_backend = not spec.get("backend") or canonical_backend(spec["backend"]) == saved_llm.get("backend")
    llm_cfg = {"backend": backend, "model": spec.get("model") or (saved_llm.get("model") if same_backend else None), "think": think}
    review_cfg = {"depth": spec.get("depth") or "thorough",
                  "independent_review": spec.get("independent", True) is not False, "personas": None}
    sources = [{"path": p, "label": Path(p).name} for p in reviews]
    meta = {k: spec.get(k) or "" for k in ("title", "journal", "field")}

    if spec.get("project"):
        proj = Project.load(spec["project"])
        with proj.mutate():
            proj.config["manuscript_files"] = [{"path": manuscript, "label": Path(manuscript).name}]
            known = {s["path"] for s in proj.config["comment_sources"]}
            proj.config["comment_sources"] += [s for s in sources if s["path"] not in known]
            if code:
                proj.config["code_paths"] = code
            if data:
                proj.config["data_paths"] = data
            if standards:
                proj.config["standards_paths"] = standards
            proj.config["llm"] = llm_cfg
            proj.config["review"].update(review_cfg)
            if submitted_pdf:
                proj.config["submitted_pdf"] = submitted_pdf
            if figures_dir:
                proj.config["figures_dir"] = figures_dir
            for k, v in meta.items():
                if v:
                    proj.config[k] = v
        _log(f"Updating existing project '{proj.slug}'")
    else:
        name = spec.get("name") or f"{Path(manuscript).stem} {time.strftime('%Y-%m-%d')}"
        proj = Project.create(name=name, journal=meta["journal"],
                              manuscript_files=[{"path": manuscript, "label": Path(manuscript).name}],
                              comment_sources=sources, title=meta["title"], field=meta["field"],
                              code_paths=code, data_paths=data, llm=llm_cfg, review=review_cfg,
                              submitted_pdf=submitted_pdf, figures_dir=figures_dir, standards_paths=standards)
        _log(f"Created project '{proj.slug}' ({proj.dir})")

    llm = make_llm(backend, llm_cfg["model"], ollama_host=spec.get("ollama_host"),
                   num_ctx=spec.get("num_ctx"), timeout=spec.get("timeout"))
    if not llm.is_available():
        raise LLMError(f"Backend '{backend}' is not available on this machine. Run `python papr.py backends`.")
    _log(f"Model: {llm.display_name}")

    def work():
        p = Project.load(proj.slug)
        for src in p.config["comment_sources"]:
            if any(c.get("source") == src["label"] for c in p.state["comments"]):
                continue
            _log(f"Extracting reviewer comments from {src['label']}...")
            raw = documents.extract_text(src["path"])
            items = review_letters.extract_comments(llm, src["label"], raw, progress=_log)
            review_letters.replace_source_comments(p, src["label"], items)
            _log(f"  {len(items)} comment(s)")
        p.reload()
        return run_review(llm, p, progress=_log, depth=review_cfg["depth"],
                                 independent=review_cfg["independent_review"], force=spec.get("fresh", False))

    result, _ctx = _run_cancellable(work)
    proj.reload()
    out = spec.get("out")
    out_path = review_report.write_report(proj, _abs(out, None) if out else None)
    _log(f"Report: {out_path}")
    if out_path.suffix.lower() == ".docx":
        _log(f"Markdown copy: {out_path.with_suffix('.md')}")

    overall = result.get("overall") or {}
    summary = {
        "project": proj.slug,
        "project_dir": str(proj.dir),
        "manuscript": manuscript,
        "report": str(out_path),
        "mode": result.get("mode"),
        "backend": result.get("backend"),
        "recommendation": overall.get("recommendation"),
        "recommendation_confidence": overall.get("recommendation_confidence"),
        "effort_hours_low": overall.get("effort_hours_low"),
        "effort_hours_high": overall.get("effort_hours_high"),
        "effort_level": overall.get("effort_level"),
        "effort_confidence": overall.get("effort_confidence"),
        "n_reviewer_comments": sum(1 for c in proj.state["comments"] if c.get("origin", "reviewer") == "reviewer"),
        "n_rebuttals": sum(1 for c in proj.state["comments"]
                           if (c.get("triage") or {}).get("stance") in ("rebut", "clarify")),
        "n_findings": sum(1 for c in proj.state["comments"] if c.get("origin") == "ai-review" and not c.get("stale")),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "warnings": result.get("warnings"),
    }
    if spec.get("json"):
        payload = {"summary": summary, "review": proj.state.get("review"), "items": proj.state["comments"]}
        if spec["json"] == "-":
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        else:
            jp = Path(_abs(spec["json"], None))
            jp.parent.mkdir(parents=True, exist_ok=True)
            jp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            _log(f"JSON: {jp}")
    return summary


def cmd_keys(args) -> int:
    if args.action == "list":
        for k in settings.key_status():
            state = f"set ({k['masked']}, from {k['source']})" if k["set"] else "not set"
            print(f"{k['name']:<18} {state:<45} {k['description']}")
        print(f"\nStored in {settings.keys_path()} (outside the repository).")
        return 0
    if not args.name or args.name not in settings.KEYS:
        print(f"Name one of: {', '.join(settings.KEYS)}", file=sys.stderr)
        return 2
    if args.action == "remove":
        settings.save_key(args.name, None)
        print(f"Removed {args.name}.")
        return 0
    value = args.value
    if value is None:
        import getpass
        value = getpass.getpass(f"{args.name} (input hidden): ")
    if not value.strip():
        print("Nothing entered; unchanged.", file=sys.stderr)
        return 1
    settings.save_key(args.name, value)
    print(f"Saved {args.name} to {settings.keys_path()}.")
    return 0


def cmd_discover(args) -> int:
    print(json.dumps(discover(args.folder), indent=2))
    return 0


def cmd_serve(args) -> int:
    from .web.server import serve
    serve(host=args.host, port=args.port, backend=args.backend or os.environ.get("PAPR_BACKEND") or "ollama",
          model=args.model or os.environ.get("PAPR_MODEL"), ollama_host=args.ollama_host,
          num_ctx=args.num_ctx, timeout=args.timeout)
    return 0


def cmd_backends(args) -> int:
    for b in detect_backends(args.ollama_host):
        mark = "OK " if b["available"] else "-- "
        print(f"{mark}{b['id']:<14} {b['kind']:<10} {b['detail']}")
        if b["available"] and b["models"]:
            dm = f" (auto: {b['default_model']})" if b.get("default_model") else ""
            print(f"    models{dm}: {', '.join(b['models'][:14])}")
    return 0


def _spec_from_args(args) -> dict:
    return {
        "manuscript": args.manuscript, "dir": args.dir, "reviews": args.reviews, "code": args.code,
        "data": args.data, "submitted_pdf": args.submitted_pdf, "figures_dir": args.figures,
        "name": args.name, "title": args.title, "journal": args.journal, "field": args.field,
        "backend": args.backend, "model": args.model, "think": args.think, "depth": args.depth,
        "independent": not args.no_independent, "project": args.project, "out": args.out,
        "json": args.json, "num_ctx": args.num_ctx, "ollama_host": args.ollama_host, "timeout": args.timeout,
        "fresh": args.fresh,
        "standards": args.standards,
    }


def cmd_review(args) -> int:
    s = run_one(_spec_from_args(args))
    rec = (s.get("recommendation") or "n/a").replace("_", " ")
    if s.get("effort_hours_high"):
        eff = describe_hours(s["effort_hours_low"], s["effort_hours_high"])
        print(f"\n{Path(s['manuscript']).name}: {rec}; revision effort {eff} "
              f"(confidence {s['effort_confidence']:.0%}); {s['n_findings']} findings"
              + (f", {s['n_reviewer_comments']} reviewer comments ({s['n_rebuttals']} to rebut/clarify)"
                 if s['n_reviewer_comments'] else ""), file=sys.stderr)
    print(s["report"])
    return 0


def _load_manifest(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".toml":
        import tomllib   # Python 3.11+
        return tomllib.loads(text)
    return json.loads(text)


def cmd_batch(args) -> int:
    mpath = Path(args.manifest).resolve()
    manifest = _load_manifest(mpath)
    defaults = manifest.get("defaults") or {}
    papers = manifest.get("papers") or []
    if not papers:
        print("Manifest has no 'papers'.", file=sys.stderr)
        return 1
    out_dir = Path(_abs(args.out_dir or defaults.get("out_dir") or "papr-reviews", mpath.parent))
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, paper in enumerate(papers, 1):
        spec = {**defaults, **paper}
        for k in ("backend", "model", "depth"):
            if getattr(args, k, None):
                spec[k] = getattr(args, k)
        stem = Path(spec["manuscript"]).stem
        spec.setdefault("out", str(out_dir / f"{i:02d}-{stem}-review.docx"))
        spec.setdefault("json", str(out_dir / f"{i:02d}-{stem}-review.json"))
        _log(f"=== Paper {i}/{len(papers)}: {spec['manuscript']} ===")
        try:
            results.append({"ok": True, **run_one(spec, mpath.parent)})
        except KeyboardInterrupt:
            _log("Batch interrupted.")
            break
        except (Exception, SystemExit) as e:
            _log(f"FAILED: {e}")
            results.append({"ok": False, "manuscript": spec["manuscript"], "error": str(e)})
    lines = ["# papr-thinkr batch review", "", "| # | Manuscript | Result | Recommendation | Effort | Confidence | Report |",
             "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        if r["ok"]:
            eff = describe_hours(r["effort_hours_low"], r["effort_hours_high"]) if r.get("effort_hours_high") else "n/a"
            lines.append(f"| {i} | {Path(r['manuscript']).name} | ok | {(r.get('recommendation') or 'n/a').replace('_', ' ')} "
                         f"| {eff} | {(r.get('effort_confidence') or 0):.0%} | {Path(r['report']).name} |")
        else:
            lines.append(f"| {i} | {Path(r['manuscript']).name} | **failed**: {r['error'][:80]} | | | | |")
    (out_dir / "batch-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "batch-summary.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nSummary written to {out_dir / 'batch-summary.md'}", file=sys.stderr)
    return 0 if all(r["ok"] for r in results) else 1


def cmd_report(args) -> int:
    proj = Project.load(args.slug)
    print(review_report.write_report(proj, args.out))
    return 0


def cmd_letter(args) -> int:
    proj = Project.load(args.slug)
    out = args.out or str(proj.reports_dir / f"{proj.slug}-response-letter-{time.strftime('%Y%m%d-%H%M%S')}.docx")
    print(response_letter.write_response_letter(proj, out))
    return 0


def cmd_submission(args) -> int:
    proj = Project.load(args.slug)
    if args.check:
        cfg = proj.config.get("llm") or {}
        llm = make_llm(args.backend or cfg.get("backend") or "ollama", args.model or cfg.get("model"),
                       ollama_host=args.ollama_host)
        result = consistency.check_all_responses(llm, proj, progress=_log, force=args.fresh)
        for error in result["errors"]:
            _log(f"{error['comment_id']}: {error['error']}")
        proj.reload()
    review = submission.build_submission_review(proj)
    print(submission.review_markdown(review))
    if args.out:
        _log(f"Bundle: {submission.write_bundle(proj, args.out)}")
    return 0 if review["ready"] else 2


def cmd_list(args) -> int:
    for cfg in Project.list_all():
        try:
            p = Project.load(cfg["slug"])
            s = p.summary()
            r = (p.state.get("review") or {}).get("overall") or {}
            print(f"{cfg['slug']:<40} {s['mode']:<11} items={s['n_comments']:<4} "
                  f"rec={(r.get('recommendation') or '-'):<15} {cfg.get('name', '')}")
        except Exception as e:
            print(f"{cfg.get('slug')}: error {e}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="papr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"papr-thinkr {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def llm_opts(p):
        p.add_argument("--backend", help="ollama | claude-code | codex | gemini | anthropic-api | openai-api | gemini-api")
        p.add_argument("--model", help="model name (default: auto-pick local model / the CLI's default)")
        p.add_argument("--depth", choices=DEPTHS, help="quick | standard | thorough (default)")
        p.add_argument("--ollama-host", help="Ollama URL (default http://127.0.0.1:11434)")

    p = sub.add_parser("serve", help="start the web UI (default http://127.0.0.1:8765)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="bind address (keep this local-only)")
    p.add_argument("--backend", help="default backend for projects that don't choose one (default: ollama)")
    p.add_argument("--model", help="default model for that backend (default: auto / the CLI's own default)")
    p.add_argument("--ollama-host", help="Ollama URL (default http://127.0.0.1:11434)")
    p.add_argument("--num-ctx", type=int, help="Ollama context window in tokens (default 32768)")
    p.add_argument("--timeout", type=int, help="per-call LLM timeout in seconds")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("keys", help="manage API keys for the cloud backends (stored outside the repo)")
    p.add_argument("action", nargs="?", choices=["list", "set", "remove"], default="list")
    p.add_argument("name", nargs="?", help="e.g. GEMINI_API_KEY")
    p.add_argument("--value", help="the key (omit to be prompted without echo)")
    p.set_defaults(fn=cmd_keys)

    p = sub.add_parser("discover", help="show what papr-thinkr finds in a paper's folder")
    p.add_argument("folder")
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("backends", help="list LLM backends usable on this machine")
    p.add_argument("--ollama-host")
    p.set_defaults(fn=cmd_backends)

    p = sub.add_parser("review", help="review one manuscript")
    p.add_argument("manuscript", nargs="?", help="manuscript file (optional with --dir)")
    p.add_argument("--dir", help="a paper's folder: find the manuscript, reviews, code, data, figures and "
                                 "submitted PDF automatically (explicit options still win)")
    p.add_argument("--submitted-pdf", help="the line-numbered PDF the reviewers read (resolves 'line 123')")
    p.add_argument("--figures", help="folder of figure files submitted separately from the manuscript")
    p.add_argument("--reviews", nargs="*", default=[], help="reviewer/editor comment files (omit for a first draft)")
    p.add_argument("--code", nargs="*", default=[], help="analysis code files or folders")
    p.add_argument("--data", nargs="*", default=[], help="data files or folders")
    p.add_argument("--name", help="project name (default: manuscript file name + date)")
    p.add_argument("--title", help="manuscript title")
    p.add_argument("--journal", help="target journal (sets the reviewers' standards)")
    p.add_argument("--field", help="research field, e.g. 'visual neuroscience'")
    llm_opts(p)
    p.add_argument("--think", choices=["on", "off"], help="force model thinking on/off (default: model default)")
    p.add_argument("--no-independent", action="store_true",
                   help="with --reviews: only triage the reviewers' comments, skip the independent critique")
    p.add_argument("--project", help="re-review an existing project (slug) instead of creating a new one")
    p.add_argument("--fresh", action="store_true", help="ignore saved model calls; otherwise reuse completed matching calls")
    p.add_argument("--standards", nargs="*", help="field/journal reporting guidelines used as evidence for statistical and methods norms")
    p.add_argument("--out", help="report path (.docx or .md); default: projects/<slug>/reports/")
    p.add_argument("--json", help="also write machine-readable results to this file ('-' for stdout)")
    p.add_argument("--num-ctx", type=int, help="Ollama context window in tokens (default 32768)")
    p.add_argument("--timeout", type=int, help="per-call timeout in seconds")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("batch", help="review several papers listed in a JSON/TOML manifest")
    p.add_argument("manifest")
    p.add_argument("--out-dir", help="where to write reports (default: <manifest dir>/papr-reviews)")
    llm_opts(p)
    p.set_defaults(fn=cmd_batch)

    p = sub.add_parser("report", help="(re)write the review report for a project")
    p.add_argument("slug")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("letter", help="write the point-by-point response letter for a project")
    p.add_argument("slug")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_letter)

    p = sub.add_parser("submission", help="check revision evidence and export a submission bundle")
    p.add_argument("slug")
    p.add_argument("--check", action="store_true", help="audit response claims with the selected model")
    p.add_argument("--fresh", action="store_true", help="ignore saved response checks")
    p.add_argument("--out", help="write a ZIP containing the response letter, change summary, checks and source manifest")
    llm_opts(p)
    p.set_defaults(fn=cmd_submission)

    p = sub.add_parser("list", help="list projects")
    p.set_defaults(fn=cmd_list)

    args = ap.parse_args(argv)
    settings.apply_keys_to_env()
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        return 130
    except (LLMError, ProjectNotFound, documents.IngestError, FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
