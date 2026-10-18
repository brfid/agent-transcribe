"""Use established OCR tools; retain original filenames and page delimiters."""
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import quote
from .common import job, run, checksum, prepare_sources, IntegrityError

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
OCR_LANGUAGE = "eng"
OCR_PSM = 3


class ExtractionError(RuntimeError):
    """An independent source could not produce discovery text."""


def page_output_prefix(source_key):
    """Nested originals use a reserved subtree; files cannot collide with directories."""
    if len(Path(source_key).parts) == 1:
        return "pages/" + source_key
    return "pages/by-source/" + source_key + "/extract"


def _scan_receipt(data):
    """Require the current extraction format without upgrading retained evidence."""
    if not isinstance(data, dict) or not isinstance(data.get("specification"), dict):
        raise ValueError("Unsupported scan processing format; expected a current receipt")
    specification = data["specification"]
    settings = specification.get("settings")
    if (type(data.get("schema")) is not int or data["schema"] != 2
            or specification.get("kind") != "scans"
            or specification.get("source_path_base") != "source_root"
            or not isinstance(settings, dict) or settings.get("adapter_version") != 4):
        raise ValueError("Unsupported scan processing format; use a current receipt or explicit retained-text index")
    if (not isinstance(data.get("source_root"), dict)
            or not isinstance(data["source_root"].get("relative_to_job"), str)
            or not data["source_root"]["relative_to_job"]
            or Path(data["source_root"]["relative_to_job"]).is_absolute()
            or not isinstance(specification.get("sources"), list)
            or not specification["sources"]):
        raise IntegrityError("Malformed scan processing receipt")
    return specification


def receipt_sources(receipt, data=None, *, source_root=None):
    """Resolve current source locators without modifying the retained receipt.

    Returns (source key, resolved original, step key, text output relative to job).
    Root overrides are explicit; no filename-only matching is performed.
    """
    receipt = Path(receipt).resolve()
    data = json.loads(receipt.read_text()) if data is None else data
    specification = _scan_receipt(data)
    base = (Path(source_root).resolve() if source_root else
            (receipt.parent / data["source_root"]["relative_to_job"]).resolve())
    rows, seen = [], set()
    for key in specification["sources"]:
        if not isinstance(key, str):
            raise IntegrityError("Invalid collection-relative source locator")
        part = Path(key)
        if part.is_absolute() or ".." in part.parts or not part.parts or key in seen:
            raise IntegrityError("Invalid or repeated collection-relative source locator")
        seen.add(key)
        source = (base / part).resolve()
        if not source.is_relative_to(base):
            raise IntegrityError("Source locator escapes its collection root")
        rows.append((key, source, key, page_output_prefix(key) + ".txt"))
    return rows


def source_root_for_job(output, sources, explicit=None):
    """Keep the root and source-relative names stable across archive relocation."""
    ledger = Path(output).expanduser() / ".processing.json"
    old = json.loads(ledger.read_text()) if ledger.is_file() else None
    if old is not None:
        _scan_receipt(old)
    if explicit:
        root = Path(explicit).expanduser().resolve()
    elif old:
        expected = old["specification"]["sources"]
        candidates = []
        for source, key in zip(sources, expected):
            part = Path(key)
            if part.is_absolute() or ".." in part.parts or not part.parts:
                raise IntegrityError("Invalid collection-relative source locator")
            if len(part.parts) > len(source.parents):
                raise ValueError("Inputs/settings differ from this job. Use a new output folder.")
            candidate = source.parents[len(part.parts) - 1]
            if candidate / part != source:
                raise ValueError("Inputs/settings differ from this job. Use a new output folder.")
            candidates.append(candidate)
        if len(sources) != len(expected) or len(set(candidates)) != 1:
            raise ValueError("Inputs/settings differ from this job. Use a new output folder.")
        root = candidates[0]
    else:
        root = Path(os.path.commonpath([str(p.parent) for p in sources]))
    if (root.exists() and not root.is_dir()) or any(not p.is_relative_to(root) for p in sources):
        raise ValueError("Every selected source must be inside the declared source root")
    return root


def reuse_records(indexes, sources, root=None, *, identity_base=None, source_root=None):
    """Reuse selected extracts, including a previous scan job's own receipt."""
    records, identities = {}, {}
    selected = set(sources)
    for filename in indexes:
        path = Path(filename).expanduser().resolve()
        base = Path(root).expanduser().resolve() if root else path.parent
        data = json.loads(path.read_text())
        receipt = (isinstance(data, dict) and isinstance(data.get("specification"), dict)
                   and data["specification"].get("kind") == "scans")
        if isinstance(data, dict) and "specification" in data and not receipt:
            raise ValueError("--reuse-index accepts scan receipts, not other processing jobs")
        if receipt:
            rows = []
            for key, source, step_key, relative in receipt_sources(path, data, source_root=root):
                if source not in selected:
                    continue
                step = data.get("steps", {}).get(step_key, {})
                if step.get("status") != "done" or relative not in step.get("outputs", {}):
                    raise ValueError("Retained scan text is not complete: " + str(source))
                text_path = path.parent / relative
                if not text_path.resolve().is_relative_to(path.parent.resolve()):
                    raise IntegrityError("Retained scan text escapes its job folder")
                rows.append({"source": str(source), "text": str(text_path),
                             "source_sha256": data["sources"][key]["sha256"],
                             "text_sha256": step["outputs"][relative]})
        else:
            if (not isinstance(data, dict) or type(data.get("schema")) is not int
                    or data["schema"] != 1 or data.get("kind") != "retained-text"
                    or set(data) != {"schema", "kind", "entries"}):
                raise ValueError("Expected a schema-1 retained-text index with kind retained-text")
            rows = data["entries"]
        required = {"source", "text", "source_sha256", "text_sha256"}
        if (not isinstance(rows, list)
                or any(not isinstance(row, dict) or set(row) != required
                       or not all(isinstance(row.get(field), str) and row[field] for field in ("source", "text"))
                       or not all(isinstance(row.get(field), str) and re.fullmatch(r"[0-9a-f]{64}", row[field])
                                  for field in ("source_sha256", "text_sha256")) for row in rows)):
            raise ValueError("Retained-text records require source, text, source_sha256 and text_sha256")
        selected_identity = {}
        for row in rows:
            source = (base / row["source"]).resolve()
            if source not in selected:
                continue
            text_path = (base / row["text"]).resolve()
            if not text_path.is_file():
                raise ValueError(f"Retained text missing: {text_path}; check --reuse-root")
            text_hash = checksum(text_path)
            if row["text_sha256"] != text_hash:
                raise IntegrityError("Retained scan text was modified: " + str(text_path))
            record = {"text": str(text_path), "sha256": row["source_sha256"], "record": str(path),
                      "text_sha256": text_hash}
            previous = records.get(str(source))
            if previous and (previous["text"], previous["sha256"]) != (record["text"], record["sha256"]):
                raise ValueError(f"Conflicting retained extracts for {source}; select one reuse index")
            records[str(source)] = record
            identity_key = source.relative_to(source_root).as_posix() if source_root else str(source)
            identity_text = os.path.relpath(text_path, identity_base) if identity_base else str(text_path)
            selected_identity[identity_key] = {"text": identity_text, "source_sha256": row["source_sha256"],
                                              "text_sha256": text_hash}
        # Verification timestamps in a receipt do not change its selected text identity.
        index_key = os.path.relpath(path, identity_base) if identity_base else str(path)
        identities[index_key] = (hashlib.sha256(json.dumps(selected_identity, sort_keys=True).encode()).hexdigest()
                                 if receipt else checksum(path))
    return records, identities


def pdf_pages(text):
    pages = text.split("\f")
    if pages[-1] == "":
        pages.pop()
    return pages


def pdf_text_notes(source, text):
    info = run(["pdfinfo", source]).stdout.decode()
    expected = int(re.search(r"^Pages:\s+(\d+)", info, re.M).group(1))
    pages = pdf_pages(text.read_text())
    if len(pages) != expected:
        raise ValueError("PDF page count and text page delimiters disagree")
    blank = [str(i) for i, page in enumerate(pages, 1) if not page.strip()]
    return (["No text on physical PDF pages " + ", ".join(blank) +
             "; inspect for blank, graphical, or image-only pages before using OCRmyPDF."] if blank else [])


def extract_page(source, temporary, make_pdf=False, *, psm=OCR_PSM, rotate_clockwise=0, ocr_pdf=False):
    text, pdf, notes = temporary / "page.txt", None, []
    suffix = source.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        from PIL import Image, ImageOps
        with Image.open(source) as image:
            ocr_input = source
            if image.getexif().get(274, 1) != 1 or rotate_clockwise:
                ocr_input = temporary / "upright.png"
                upright = ImageOps.exif_transpose(image)
                if rotate_clockwise:
                    upright = upright.rotate(-rotate_clockwise, expand=True)
                    notes.append(f"Applied {rotate_clockwise} degrees clockwise after EXIF normalization.")
                upright.convert("RGB").save(ocr_input)
        run(["tesseract", ocr_input, temporary / "page", "-l", OCR_LANGUAGE,
             "--psm", psm, "txt", "tsv", *(["pdf"] if make_pdf else [])],
            env={"OMP_THREAD_LIMIT": "1"})
        if make_pdf:
            pdf = temporary / "page.pdf"
    elif suffix == ".pdf":
        run(["pdftotext", "-layout", "-enc", "UTF-8", source, text])
        notes.extend(pdf_text_notes(source, text))
        if ocr_pdf and any(not page.strip() for page in pdf_pages(text.read_text())):
            pdf = temporary / "ocr.pdf"
            run(["ocrmypdf", "--mode", "skip", "--output-type", "pdf", "--optimize", "0", "--jobs", "1", source, pdf],
                timeout=1200, env={"OMP_THREAD_LIMIT": "1"})
            run(["pdftotext", "-layout", "-enc", "UTF-8", pdf, text])
            notes.append("Native PDF page gaps triggered one local OCRmyPDF skip pass; original preserved.")
            notes.extend(pdf_text_notes(source, text))
        elif make_pdf:
            pdf = source
    elif suffix == ".txt":
        text.write_text(source.read_text(encoding="utf-8-sig"))
    elif suffix == ".docx":
        with zipfile.ZipFile(source) as archive:
            if archive.getinfo("word/document.xml").file_size > 100_000_000:
                raise ValueError("DOCX main XML exceeds extraction limit")
            tree = ET.fromstring(archive.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = tree.findall(".//w:body//w:p", ns)
        text.write_text("\n\n".join(f"[paragraph {i}] " + "".join(n.text or "" for n in p.findall(".//w:t", ns))
                                  for i, p in enumerate(paragraphs, 1)))
        notes.append("Native DOCX main-body paragraph/table text only; headers, footnotes, comments, and embedded images need separate review.")
    else:
        raise ValueError("Unsupported scan format; retain and record separately: " + suffix)
    return text, pdf, notes


def source_arguments(args):
    filename = getattr(args, "sources_file", None)
    if not filename:
        return args.source
    path = Path(filename).expanduser().resolve()
    if getattr(path.stat(), "st_flags", 0) & 0x40000000:
        raise ValueError("Fetch this exact ordered source list before reading it: " + str(path))
    return [str(Path(line).expanduser() if Path(line).expanduser().is_absolute()
                else path.parent / line)
            for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def publish_partial_view(current, sources, failures, step_key, text_path, source_header):
    """Publish an immutable, explicitly incomplete snapshot; never replace human text."""
    failure_by_source = {row["source"]: row for row in failures}
    parts = ["PARTIAL DISCOVERY READING — selected inputs are not all currently verified/extracted.\n",
             "Machine text is unchecked. Failure blocks are metadata, not source wording.\n",
             "Older snapshots remain evidence; consult the command result for the current reading view.\n"]
    for source in sources:
        parts.extend(["\n\n", source_header(source), "\n"])
        failure = failure_by_source.get(str(source))
        if failure:
            label = "SOURCE UNAVAILABLE" if failure["stage"] == "intake" else "EXTRACTION FAILED"
            parts.append(f"[{label}: {failure['error']}]\n")
        if current.completed(step_key(source)):
            if failure:
                parts.append("[SAVED TEXT BELOW: output hash verified; original unavailable for current verification.]\n")
            text = (current.folder / text_path(source)).read_text()
            if source.suffix.lower() == ".pdf":
                for i, page in enumerate(pdf_pages(text), 1):
                    parts.extend([f"=== Physical PDF page: {i} ===\n", page if page.strip() else "[NO EXTRACTED TEXT ON THIS PAGE]\n"])
            else:
                parts.append(text if text.strip() else "[NO EXTRACTED TEXT FROM THIS SOURCE]\n")
        else:
            parts.append("[NO SAVED TEXT AVAILABLE FOR THIS SOURCE]\n")
    content = "".join(parts)
    digest = hashlib.sha256(content.encode()).hexdigest()
    name = "partial-view:" + digest
    views = current.state.setdefault("partial_views", {})
    if digest not in views:
        views[digest] = {"step": name, "path": f"partial-reading-{len(views) + 1:04d}.txt"}
        current.save()
    relative = views[digest]["path"]
    def build(tmp):
        target = tmp / "partial.txt"
        target.write_text(content)
        return {relative: target}, {"partial": True, "selected": len(sources), "failures": len(failures)}
    current.step(name, build)
    current.state["current_reading"] = {"path": relative, "partial": True, "sha256": digest}
    current.save()
    return current.folder / relative


def process(args):
    best_effort = getattr(args, "best_effort", False)
    sources = prepare_sources(source_arguments(args), allow_missing=best_effort)
    source_root = source_root_for_job(args.output, sources, getattr(args, "source_root", None))
    def source_key(source):
        return source.relative_to(source_root).as_posix()
    def step_key(source):
        return source_key(source)
    def text_path(source):
        return page_output_prefix(step_key(source)) + ".txt"
    def source_header(source):
        return f"=== Original (source-root-relative): {source_key(source)} ===\n"
    indexes = [Path(p).expanduser().resolve() for p in args.reuse_index]
    reuse_root = getattr(args, "reuse_root", None)
    psm, rotate = getattr(args, "psm", OCR_PSM), getattr(args, "rotate_clockwise", 0)
    ocr_pdf = getattr(args, "ocr_image_pdfs", False)
    if psm not in (3, 6, 11) or rotate not in (0, 90, 180, 270):
        raise ValueError("Supported image retries: PSM 3, 6 or 11; clockwise rotation 0, 90, 180 or 270")
    if (rotate or psm != OCR_PSM) and (indexes or any(p.suffix.lower() not in IMAGE_SUFFIXES for p in sources)):
        raise ValueError("Targeted image retries require image inputs and fresh OCR, without --reuse-index")
    if args.pdf and any(p.suffix.lower() not in IMAGE_SUFFIXES | {".pdf"} for p in sources):
        raise ValueError("--pdf requires image/PDF inputs; process native text and contextual formats without it")
    reuse, index_identities = reuse_records(indexes, sources, reuse_root,
                                           identity_base=Path(args.output).resolve(),
                                           source_root=source_root)
    settings = {"language": OCR_LANGUAGE, "psm": psm, "exif_upright": True, "make_pdf": args.pdf,
                "rotate_clockwise": rotate, "pdf_ocr": ocr_pdf, "reuse_indexes": index_identities,
                "reuse_text_sha256": {source_key(Path(source)): record["text_sha256"] for source, record in reuse.items()},
                "reuse_root": os.path.relpath(Path(reuse_root).expanduser().resolve(), Path(args.output).resolve()) if reuse_root else None,
                "adapter_version": 4}
    if any(p.suffix.lower() in IMAGE_SUFFIXES or ocr_pdf and p.suffix.lower() == ".pdf" for p in sources):
        settings["tesseract"] = run(["tesseract", "--version"]).stdout.decode().splitlines()[0]
    if ocr_pdf and any(p.suffix.lower() == ".pdf" for p in sources):
        settings["ocrmypdf"] = run(["ocrmypdf", "--version"]).stdout.decode().strip()
    with job(args.output, "scans", sources, settings, args.fetch, best_effort=best_effort,
             source_root=source_root) as current:
        # Old partial views remain evidence and must never conceal a human change.
        for view in current.state.get("partial_views", {}).values():
            previous = current.state["steps"].get(view["step"], {})
            if previous.get("status") == "done" or previous.get("prepared_outputs"):
                current.step(view["step"], None)
        extraction_started = time.monotonic()
        results, failures = [], []
        for source in sources:
            intake_error = current.state.get("intake_failures", {}).get(source_key(source))
            if intake_error:
                if current.completed(step_key(source)):
                    results.append(current.step(step_key(source), None))  # Verify saved text even without fresh source access.
                failures.append({"source": str(source), "stage": "intake", "error": intake_error["error"]})
                continue
            def build_page(tmp, source=source):
                record = reuse.get(str(source))
                notes, retained = [], None
                if record:
                    if record["sha256"] != current.source_checksum(source):
                        raise IntegrityError("Retained text source hash differs from the selected original: " + str(source))
                    retained = Path(record["text"]).read_bytes()
                    if hashlib.sha256(retained).hexdigest() != record["text_sha256"]:
                        raise IntegrityError("Retained text changed during this job: " + record["text"])
                    notes.append("Retained extract: " + record["text"] + "; SHA-256 " + record["text_sha256"])
                adequate = retained is not None and bool(retained.decode("utf-8-sig").strip())
                retained_page_gaps = (record and adequate and ocr_pdf and source.suffix.lower() == ".pdf"
                                      and any(not page.strip() for page in pdf_pages(retained.decode("utf-8-sig"))))
                tsv = tmp / "page.tsv"
                if record and adequate and not retained_page_gaps:
                    text = tmp / "page.txt"
                    text.write_bytes(retained)
                    pdf = None
                    if source.suffix.lower() == ".pdf":
                        notes.extend(pdf_text_notes(source, text))
                        if args.pdf:
                            pdf = source
                    elif args.pdf:
                        layer = tmp / "pdf-layer"
                        layer.mkdir()
                        _, pdf, extra = extract_page(source, layer, make_pdf=True, psm=psm,
                                                     rotate_clockwise=rotate, ocr_pdf=ocr_pdf)
                        tsv = layer / "page.tsv"
                        notes.extend(extra)
                        notes.append("Existing plain text reused; fresh OCR creates the PDF/TSV layer, which may differ from that text.")
                    method = "reused existing text with matching recorded source hash"
                else:
                    text, pdf, extra = extract_page(source, tmp, make_pdf=args.pdf, psm=psm,
                                                    rotate_clockwise=rotate, ocr_pdf=ocr_pdf)
                    notes.extend(extra)
                    method = "native text or Tesseract"
                    if retained_page_gaps:
                        method = "fresh PDF extraction/OCR after retained page gaps"
                        notes.append("Retained PDF text with page gaps was preserved; fresh extraction/OCR attempted.")
                    elif record and not adequate:
                        method = "fresh extraction after blank retained text"
                        notes.append("Blank retained text was preserved; one fresh extraction was attempted.")
                files = {text_path(source): text}
                if pdf:
                    files[page_output_prefix(step_key(source)) + ".pdf"] = pdf
                if tsv.exists():
                    files[page_output_prefix(step_key(source)) + ".tsv"] = tsv
                if source.suffix.lower() == ".pdf":
                    for i, page in enumerate(pdf_pages(text.read_text()), 1):
                        leaf = tmp / f"physical-page-{i:04d}.txt"
                        locator = (f"Original (source-root-relative): {source_key(source)}\n"
                                   f"Original location at extraction: {source}\n")
                        leaf.write_text(locator + f"Physical PDF page: {i}\n"
                                        "Machine extraction; wording unverified.\n\n" +
                                        (page if page.strip() else "[NO EXTRACTED TEXT ON THIS PAGE]\n"))
                        files[f"{page_output_prefix(step_key(source))}.pages/page-{i:04d}.txt"] = leaf
                blank_pages = ([i for i, page in enumerate(pdf_pages(text.read_text()), 1) if not page.strip()]
                               if source.suffix.lower() == ".pdf" else [])
                return files, {"source": source_key(source), "method": method, "notes": notes,
                               "blank_pdf_pages": blank_pages,
                               "blank_text": not bool(text.read_text().strip()), "has_pdf": bool(pdf)}
            def build(tmp):
                try:
                    return build_page(tmp)
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    if (isinstance(exc, IntegrityError)
                            or isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)):
                        raise
                    raise ExtractionError(str(exc)) from exc
            try:
                results.append(current.step(step_key(source), build))
            except ExtractionError as exc:
                if not best_effort:
                    raise
                failures.append({"source": str(source), "stage": "extraction", "error": str(exc)})
        def assemble(tmp):
            with (tmp / "document.txt").open("w") as output:
                for source in sources:
                    output.write("\n\n" + source_header(source) + "\n")
                    text = (current.folder / text_path(source)).read_text()
                    if source.suffix.lower() == ".pdf":
                        for i, page in enumerate(pdf_pages(text), 1):
                            output.write(f"=== Physical PDF page: {i} ===\n\n")
                            output.write(page if page.strip() else "[NO EXTRACTED TEXT ON THIS PAGE]\n")
                    else:
                        output.write(text if text.strip() else "[NO EXTRACTED TEXT FROM THIS SOURCE]\n")
            outputs = {"document.txt": tmp / "document.txt"}
            if args.pdf:
                if not all(r["has_pdf"] for r in results):
                    raise ValueError("One source has no PDF representation; retain the text and review it separately")
                parts = [current.folder / (page_output_prefix(step_key(p)) + ".pdf") for p in sources]
                if len(parts) == 1:
                    shutil.copyfile(parts[0], tmp / "document.pdf")
                else:
                    run(["pdfunite", *parts, tmp / "document.pdf"])
                outputs["document.pdf"] = tmp / "document.pdf"
            return outputs, {"source_count": len(sources), "review_required": True}
        if not failures:
            current.step("combined-output", assemble)
            current.state["current_reading"] = {"path": "document.txt", "partial": False,
                                                 "sha256": checksum(current.folder / "document.txt")}
        partial_text = None
        if failures:
            partial_text = publish_partial_view(current, sources, failures, step_key, text_path, source_header)
        current.state.setdefault("timings", {})["last_extraction_and_assembly_seconds"] = round(time.monotonic() - extraction_started, 6)
        source_rows = "\n".join(
            f"{i}. [{source_key(p)}](<{quote(os.path.relpath(p, current.folder), safe='/')}>) — `{text_path(p)}`"
            for i, p in enumerate(sources, 1))
        reading_notes = "\n".join(
            f"- {r['source']}: {r['method']}. " +
            ("EMPTY TEXT. " if r["blank_text"] else "") + "; ".join(r["notes"])
            for r in results)
        failure_notes = "\n".join(f"- {r['source']}: {r['stage']} failed: {r['error']}" for r in failures)
        current.write_readme(
            f"# {current.folder.name}\n\n"
            "Title, historical date and document boundaries: unreviewed. Record printed/supplied titles "
            "and date evidence here; distinguish physical PDF pages from printed labels.\n\n"
            "## Batch declaration\n\n"
            "Purpose: best-effort discovery text. Scope: only the ordered sources below; no automatic expansion.\n"
            + "Source locators are relative to the collection root recorded in .processing.json. "
              "Original links are relative to this folder.\n" +
            "Carried-in open documents, stopping condition and exact next unprocessed source: record here.\n\n"
            f"## Original sources in order\n\n{source_rows}\n\n"
            f"## Extraction at README creation\n\n{reading_notes}\n\n"
            f"Failures at creation: {len(failures)}.\n{failure_notes}\n\n"
            "The processing receipt and command result own current mechanical coverage after retries. "
            "This README is created once and preserves all human additions. Successful text under pages/ "
            "is usable for discovery even while other sources fail. document.txt is assembled only after "
            "all selected sources extract successfully; blank text remains explicitly flagged. "
            "Partial reading snapshots are labeled partial-reading-NNNN.txt; the command result and "
            "processing receipt identify the current view. Older snapshots remain attempt evidence.\n\n"
            "## Reading and review\n\n"
            "Search per-source text using the paths above. Physical PDF-page text is beside its source's "
            "text output in the matching .pages/page-NNNN.txt directory. "
            "Treat source headers as locators, not original wording. Search spelling variants and similar terms; "
            "OCR misses are not evidence of absence. Inspect the original before relying on exact wording, "
            "numbers or consequential title/date/boundary claims.\n\n"
            "Visual review coverage: none. Record checked pages and methods here. Identification/grouping "
            "and filing: unreviewed; neither gates discovery text. Record printed/supplied titles, date evidence, "
            "physical/printed page locators, cover/enclosure relationships, alternate captures and gaps.\n")
        return {"sources": len(sources), "extracted": len(results), "extraction_complete": not failures,
                "blank_text": sum(r["blank_text"] for r in results), "review_required": True,
                "text_gaps": [{"source": str(current.source_path(r["source"])), "blank_text": r["blank_text"],
                               "blank_pdf_pages": r.get("blank_pdf_pages", [])} for r in results
                              if r["blank_text"] or r.get("blank_pdf_pages")],
                "failures": failures, "output": str(current.folder),
                "source_root": str(source_root),
                "timings": current.state.get("timings", {}),
                "partial_text": str(partial_text) if partial_text else None,
                "reading_text": str(partial_text or current.folder / "document.txt"),
                "combined_text": str(current.folder / "document.txt") if not failures else None}
