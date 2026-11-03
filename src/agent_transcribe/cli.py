#!/usr/bin/env python3
"""Extract, file, check, or read explicitly selected archival records."""
import argparse
from collections import deque
import itertools
import hashlib
import json
from pathlib import Path
import subprocess
import sys
sys.dont_write_bytecode = True
from . import filing, scans, __version__


def bounded_integer(low, high):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number
    return parse


def _excerpt(line, number, terms=()):
    """Bound each output line while keeping a matching passage visible."""
    line = line.rstrip("\r\n")
    start = 0
    if len(line) > 1000 and terms:
        folded = line.casefold()
        positions = [folded.find(term.casefold()) for term in terms if term.casefold() in folded]
        # Case folding can expand characters. Locate the original character near
        # the first match before clipping, so non-ASCII matches stay visible.
        if positions:
            folded_position = min(positions)
            consumed = 0
            for index, char in enumerate(line):
                if consumed >= folded_position:
                    start = max(0, index - 200)
                    break
                consumed += len(char.casefold())
    return {"line": number, "text": line[start:start + 1000],
            "truncated": bool(start or len(line) > start + 1000)}


def read_selection(args):
    intake = None
    if args.fetch:
        intake = filing.fetch_selection(args.document, jobs=args.job)
    result = filing.preferred_search_paths(args.document, jobs=args.job, allow_discrepancies=True)
    result["selected"] = [{"path": path, "kind": "note" if Path(path).name == "README.md" else "source_text"}
                          for path in result["files"]]
    if intake is not None:
        result["intake"] = intake
    if not args.query:
        return result
    result.update({"queries": args.query, "query_mode": "case-insensitive literal OR",
                   "limit": args.limit, "context_lines": args.context,
                   "max_characters_per_excerpt_line": 1000, "matches": []})
    folded_terms = [term.casefold() for term in args.query]
    for selected in result["selected"]:
        if len(result["matches"]) >= args.limit:
            break
        try:
            with Path(selected["path"]).open(encoding="utf-8") as stream:
                before = deque(maxlen=args.context)
                following = deque(itertools.islice(stream, args.context + 1))
                number = 1
                while following:
                    current = following[0]
                    matched = [term for term, folded in zip(args.query, folded_terms)
                               if folded in current.casefold()]
                    if matched:
                        excerpt = [_excerpt(line, number - len(before) + offset)
                                   for offset, line in enumerate(before)]
                        excerpt += [_excerpt(line, number + offset, matched if offset == 0 else ())
                                    for offset, line in enumerate(following)]
                        result["matches"].append({**selected, "line": number,
                                                   "matched_terms": matched, "excerpt": excerpt})
                        if len(result["matches"]) >= args.limit:
                            break
                    before.append(following.popleft())
                    next_line = next(stream, None)
                    if next_line is not None:
                        following.append(next_line)
                    number += 1
        except (OSError, UnicodeError) as exc:
            result["errors"].append({"path": selected["path"], "error": str(exc)})
            result["complete"] = False
    result["limit_reached"] = len(result["matches"]) >= args.limit
    return result


def check_selection(args):
    result = {"documents": [], "errors": [], "valid": True, "complete": True}
    if args.fetch:
        result["intake"] = filing.fetch_selection(args.document, include_copies=True,
                                                  include_sources=args.verify_sources,
                                                  source_root=args.source_root)
    for document in dict.fromkeys(args.document):
        try:
            checked = filing.validate_document(document, verify_sources=args.verify_sources,
                                               source_root=args.source_root)
            result["documents"].append(checked)
            if checked["unavailable"] or checked["text_gaps"]:
                result["complete"] = False
        except (OSError, ValueError, RuntimeError) as exc:
            result["errors"].append({"document": str(Path(document).resolve()), "error": str(exc)})
            result["valid"] = result["complete"] = False
    return result


def _conversion_selection(args):
    """Pin selection controls independently from the fixed discovered list."""
    from .folder_intake import _absolute, _control
    selected = {"recursive": args.recursive,
                "sources_file": str(_absolute(args.sources_file)) if args.sources_file else None,
                "exclude": list(args.exclude),
                "reuse_from": [str(_absolute(p)) for p in args.reuse_from]}
    if args.sources_file:
        selected["sources_file_sha256"] = hashlib.sha256(_control(Path(args.sources_file))).hexdigest()
    return selected


def conversion_selection(args):
    from .common import IntegrityError
    from .folder_intake import build_folder_plan, _absolute
    selection = _conversion_selection(args)
    output, root = _absolute(args.output), _absolute(args.folder)
    ledger = output / "conversion.json"
    if ledger.exists():
        if ledger.is_symlink():
            raise IntegrityError("Symlink conversion receipt requires inspection")
        receipt = json.loads(ledger.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or not isinstance(receipt.get("specification"), dict):
            raise IntegrityError("Malformed conversion receipt")
        spec = receipt["specification"]
        if not isinstance(spec.get("sources"), list):
            raise IntegrityError("Malformed frozen source list")
        if receipt.get("kind") != "docling-folder" or receipt.get("schema") != 1:
            raise IntegrityError("Unsupported conversion receipt")
        if spec.get("source_root") != str(root) or spec.get("selection", {}) != selection:
            raise IntegrityError("Frozen selection controls differ; preserve this bundle and inspect the command")
        return {"kind": "docling-folder", "schema": 1, "source_root": spec["source_root"],
                "output": str(output), "sources": spec["sources"], "selection": selection,
                "exclusions": receipt.get("plan_exclusions", []), "issues": []}
    plan = build_folder_plan(args.folder, args.output, recursive=args.recursive,
                             sources_file=args.sources_file, exclude=args.exclude, reuse_from=args.reuse_from)
    plan["selection"] = selection
    return plan


def _conversion_options(args):
    from .docling_backend import BackendOptions
    return BackendOptions(artifacts_path=str(Path(args.artifacts_path).resolve()) if args.artifacts_path else None,
                          rotate_clockwise=args.rotate_clockwise, raster_images_scale=args.raster_images_scale)


def archive_selection(args):
    from .folder_intake import build_archive_plan, write_archive_plan, _control
    bindings, exclusions = None, list(args.exclude)
    if args.bindings:
        data = json.loads(_control(Path(args.bindings)))
        if isinstance(data, list):
            bindings = data
        elif isinstance(data, dict) and isinstance(data.get("bindings"), list):
            bindings = data["bindings"]
            exclusions = list(data.get("exclusions", [])) + exclusions
        else:
            raise ValueError("Bindings must be a list or an object containing a bindings list")
    plan = build_archive_plan(args.root, bindings=bindings, exclusions=exclusions)
    files = write_archive_plan(plan, args.output)
    return {**plan, "plan_files": files, "execution_authorized": False}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-transcribe", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="mode", required=True)
    scan = sub.add_parser("scan", help="Extract an explicitly ordered scan/document batch")
    selection = scan.add_mutually_exclusive_group(required=True)
    selection.add_argument("--source", action="append", help="Repeat in document/capture order")
    selection.add_argument("--sources-file", help="UTF-8 ordered paths, one per line; relative to this list")
    scan.add_argument("--source-root", help="Collection root for portable source-relative locators")
    scan.add_argument("--reuse-index", action="append", default=[], help="Retained-text index or scan .processing.json")
    scan.add_argument("--reuse-root", help="Base for retained-text paths or a scan receipt's source root")
    scan.add_argument("--pdf", action="store_true", help="Assemble a searchable PDF as well as text")
    scan.add_argument("--output", required=True, help="A separate document derivative folder")
    scan.add_argument("--fetch", action="store_true", help="Verify the exact inputs using installed icmat")
    scan.add_argument("--best-effort", action="store_true", help="Continue independent intake/extraction failures; exit 2 for gaps")
    scan.add_argument("--ocr-image-pdfs", action="store_true", help="One local OCRmyPDF skip pass for PDF pages lacking native text")
    scan.add_argument("--rotate-clockwise", type=int, choices=(0, 90, 180, 270), default=0,
                      help="Explicit image retry after EXIF normalization; fresh output folder required")
    scan.add_argument("--psm", type=int, choices=(3, 6, 11), default=3,
                      help="Image segmentation: automatic, uniform block, or sparse text; default 3")

    file = sub.add_parser("file", help="File an explicit reviewed assignment list into document folders")
    file.add_argument("--assignments", required=True, help="Versioned JSON source, text, and document assignments")
    file.add_argument("--output", required=True, help="Separate organized destination")
    file.add_argument("--source-root", help="Override the assignment list's source root")
    file.add_argument("--text-root", help="Override the assignment list's retained-text root")
    file.add_argument("--fetch", action="store_true", help="Fetch only assigned originals, text, and filing controls")

    reconcile = sub.add_parser("reconcile", help="Reconcile pinned preferred readings or text-gap flags; preserve source membership")
    reconcile.add_argument("--document", required=True, help="One existing filed document folder")
    reconcile.add_argument("--changes", required=True, help="Pinned schema-1 reconciliation recipe with reason/evidence")
    reconcile.add_argument("--fetch", action="store_true", help="Fetch exact selected controls, text and source checks")

    check = sub.add_parser("check", help="Check explicit document folders and report integrity errors and text gaps")
    check.add_argument("--document", action="append", required=True, help="Repeat for each document folder")
    check.add_argument("--verify-sources", action="store_true", help="Also rehash original source bytes")
    check.add_argument("--source-root", help="Override the recorded source root for original verification")
    check.add_argument("--fetch", action="store_true", help="Fetch controls, text, copies, and originals only with --verify-sources")

    reading = sub.add_parser("reading", help="Select preferred text and notes, optionally search literal terms")
    reading.add_argument("--document", action="append", default=[], help="Repeat for each filed document folder")
    reading.add_argument("--job", action="append", default=[], help="Explicit extraction job or receipt; fallback for unfiled sources")
    reading.add_argument("--query", action="append", default=[], help="Repeat literal terms; match any term, ignoring case")
    reading.add_argument("--limit", type=bounded_integer(1, 1000), default=20, help="Maximum matching lines; default 20")
    reading.add_argument("--context", type=bounded_integer(0, 20), default=2, help="Lines before/after a match; default 2")
    reading.add_argument("--fetch", action="store_true", help="Fetch only selected controls and text; no scan/image/audio payloads")
    convert = sub.add_parser("convert", help="Convert a frozen folder or saved archive plan into portable local bundles")
    convert.add_argument("folder", nargs="?", help="Original folder; nonrecursive unless requested")
    convert.add_argument("--output", help="Separate derivative bundle (required for a folder)")
    convert.add_argument("--recursive", action="store_true")
    convert.add_argument("--sources-file", help="Ordered exact source paths instead of discovery")
    convert.add_argument("--reuse-from", action="append", default=[], help="Accepted legacy/portable reading root; repeatable")
    convert.add_argument("--exclude", action="append", default=[], help="Relative excluded path; repeatable")
    convert.add_argument("--fetch", action="store_true", help="Fetch only exact selected inputs with icmat")
    convert.add_argument("--plan", action="store_true", help="Print metadata-only frozen folder plan; no models, fetch or conversion")
    convert.add_argument("--plan-file", help="Execute only ready jobs in a saved consolidated archive plan")
    convert.add_argument("--retry-failed", action="store_true", help="Additional bounded attempts; retain accepted outputs")
    convert.add_argument("--use-pinned-notes", action="store_true", help="On resume, retain the verified intake-pinned document note snapshot")
    convert.add_argument("--artifacts-path", help="Explicit previously downloaded Docling model directory")
    convert.add_argument("--raster-images-scale", type=float, help="Opt-in native image/TIFF page and picture export scale; default retains scale 3; PDFs unchanged")
    convert.add_argument("--rotate-clockwise", type=int, choices=(0, 90, 180, 270), default=0)

    plan = sub.add_parser("plan", help="Save metadata-only consolidated archive selection; execution is separate")
    plan.add_argument("root")
    plan.add_argument("--output", required=True, help="Saved JSON plan; readable .md is written beside it")
    plan.add_argument("--bindings", help="JSON collection bindings list, or object with bindings and exclusions")
    plan.add_argument("--exclude", action="append", default=[])

    verify = sub.add_parser("verify", help="Verify a portable conversion bundle, including stored assets")
    verify.add_argument("bundle")
    args = parser.parse_args(argv)
    if args.mode == "convert":
        if args.plan_file:
            if args.folder or args.output or args.recursive or args.sources_file or args.reuse_from or args.exclude or args.plan:
                parser.error("--plan-file cannot combine with folder selection, --output or --plan")
        elif not args.folder or not args.output:
            parser.error("convert requires FOLDER --output DEST, or --plan-file PATH")
        if args.plan and (args.fetch or args.retry_failed or args.artifacts_path or args.rotate_clockwise or args.use_pinned_notes or args.raster_images_scale is not None):
            parser.error("--plan is metadata-only; omit --fetch, --retry-failed and conversion settings")
    if args.mode == "reading":
        if not args.document and not args.job:
            parser.error("reading requires at least one --document or --job")
        if any(not query.strip() for query in args.query):
            parser.error("--query must contain a non-whitespace literal term")
    try:
        if args.mode == "plan":
            result = archive_selection(args)
            status = 0
        elif args.mode == "verify":
            from .portable import verify_bundle
            result = verify_bundle(args.bundle)
            status = result["exit_code"]
        elif args.mode == "convert":
            if args.plan_file:
                from .conversion import run_archive_plan
                result = run_archive_plan(args.plan_file, options=_conversion_options(args),
                                          fetch_inputs=args.fetch, retry_failed=args.retry_failed,
                                          use_pinned_notes=args.use_pinned_notes)
            else:
                selected = conversion_selection(args)
                if args.plan:
                    result = selected
                else:
                    from .conversion import convert_folder
                    result = convert_folder(selected, options=_conversion_options(args),
                                            fetch_inputs=args.fetch, retry_failed=args.retry_failed,
                                            use_pinned_notes=args.use_pinned_notes)
            status = result.get("exit_code", 0)
        elif args.mode == "scan":
            result = scans.process(args)
            status = 0 if result["extraction_complete"] else 2
        elif args.mode == "file":
            result = filing.file_assignments(args.assignments, args.output, source_root=args.source_root,
                                              text_root=args.text_root, fetch_inputs=args.fetch)
            status = 2 if any(item["unavailable"] or item["text_gaps"] for item in result["documents"]) else 0
        elif args.mode == "reconcile":
            result = filing.reconcile_document(args.document, args.changes, fetch_inputs=args.fetch)
            status = 2 if result["unavailable"] or result["text_gaps"] else 0
        elif args.mode == "check":
            result = check_selection(args)
            status = 1 if not result["valid"] else (0 if result["complete"] else 2)
        else:
            result = read_selection(args)
            status = 0 if result["complete"] else 2
        print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        print("Interrupted; preserve this folder and repeat the exact command to resume.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return status


if __name__ == "__main__":
    sys.exit(main())
