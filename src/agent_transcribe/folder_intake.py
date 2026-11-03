"""Frozen metadata inventories, read-only retained readings and exact input intake.

Planning never opens an original or retained text payload. Only explicitly scoped,
small, locally available control records are read. Receipt claims remain pending
until prepare_source verifies their hashes against the selected exact paths.
"""
from copy import deepcopy
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zipfile

from .common import IntegrityError, atomic, checksum, fetch, json_bytes

KINDS = {".pdf": "pdf", ".jpg": "image", ".jpeg": "image", ".png": "image",
         ".tif": "tiff", ".tiff": "tiff", ".txt": "txt", ".docx": "docx"}
MARKERS = {".processing.json", ".filing-current.json", ".filing.json", "conversion.json"}
# These are tooling/control directory names, not guesses based on source titles.
CONTROL_DIRS = {"__pycache__", "node_modules", "batch-inputs", "batch-receipts",
                "archive-system", "tooling", "receipts", "indexes"}
CONTROL_NAMES = {"AGENTS.md", "STATUS.md", "README.md", "TASKS.md"}
MAX_CONTROL_BYTES = 16 * 1024 * 1024
HASH = re.compile(r"[0-9a-f]{64}\Z")
COVERAGE = {"verified_complete", "partial", "no_recorded_processing", "unresolved"}


def _absolute(value):
    """Normalize dot segments, but reject symlinks before resolving anything."""
    raw = Path(value).expanduser().absolute()
    cursor = Path(raw.anchor)
    for part in raw.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise IntegrityError("Symlink path is not permitted: " + str(cursor))
    return Path(os.path.abspath(raw))


def _relative(root, value):
    if (not isinstance(value, str) or not value or "\n" in value or "\r" in value
            or "\x00" in value or "\\" in value):
        raise IntegrityError("Expected a safe root-relative path")
    part = Path(value)
    if part.is_absolute() or ".." in part.parts or not part.parts:
        raise IntegrityError("Path escapes its declared root: " + value)
    target = _absolute(root / part)
    if not target.is_relative_to(root):
        raise IntegrityError("Path escapes its declared root: " + value)
    return target


def _locator(folder, value):
    # Current receipts legitimately ascend to sibling original/text roots.
    if not isinstance(value, str) or not value or Path(value).is_absolute() or any(c in value for c in "\n\r\x00"):
        raise IntegrityError("Malformed relative receipt locator")
    return _absolute(folder / value)


def _dataless(info):
    return bool(getattr(info, "st_flags", 0) & 0x40000000)


def _control(path, *, limit=MAX_CONTROL_BYTES):
    path = _absolute(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or _dataless(info):
        raise RuntimeError("Control record unavailable locally: " + str(path))
    if info.st_size > limit:
        raise RuntimeError("Control record exceeds bounded read limit: " + str(path))
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    after = path.stat()
    if len(data) > limit or _identity(info) != _identity(after):
        raise IntegrityError("Control record changed during planning: " + str(path))
    return data


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _hash(value):
    return isinstance(value, str) and bool(HASH.fullmatch(value))


def _overlap(first, second):
    return first.is_relative_to(second) or second.is_relative_to(first)


def _excluded_paths(root, values):
    return [_relative(root, str(value)) for value in values]


def _guard(root, path, excluded, marker_cache=None):
    if any(path == e or path.is_relative_to(e) for e in excluded):
        return "explicit exclusion"
    relative = path.relative_to(root)
    if any(part.startswith(".") for part in relative.parts):
        return "hidden/bookkeeping path"
    if any(part in CONTROL_DIRS for part in relative.parts[:-1]):
        return "tooling/control directory"
    if path.name in CONTROL_NAMES or (path.is_dir() and path.name in CONTROL_DIRS):
        return "bookkeeping/control path"
    cursor = path if path.is_dir() else path.parent
    while cursor.is_relative_to(root):
        if marker_cache is not None and cursor in marker_cache:
            detected = marker_cache[cursor]
        else:
            detected = any((cursor / marker).exists() for marker in MARKERS)
            if marker_cache is not None:
                marker_cache[cursor] = detected
        if detected:
            return "detected derivative root: " + str(cursor)
        if cursor == root:
            break
        cursor = cursor.parent
    return None


def _enumerate(root, recursive, excluded, *, explicit=None):
    sources, omissions = [], []
    marker_cache = {}
    if explicit is not None:
        seen = set()
        for value in explicit:
            path = _relative(root, value)
            if path in seen:
                raise IntegrityError("Repeated selected source path: " + value)
            seen.add(path)
            reason = _guard(root, path, excluded, marker_cache)
            if reason:
                raise IntegrityError("Explicit source is guarded (" + reason + "): " + value)
            if path.exists() and not path.is_file():
                raise IntegrityError("Selected source is not a regular file: " + value)
            sources.append(path)
        return sources, omissions

    def visit(folder):
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            omissions.append({"path": str(folder.relative_to(root)), "reason": "unavailable directory: " + str(exc)})
            return
        for child in children:
            relative = child.relative_to(root).as_posix()
            if child.is_symlink():
                omissions.append({"path": relative, "reason": "symlink"})
                continue
            reason = _guard(root, child, excluded, marker_cache)
            if reason:
                omissions.append({"path": relative, "reason": reason})
            elif child.is_dir():
                if recursive:
                    visit(child)
                else:
                    omissions.append({"path": relative, "reason": "nonrecursive selection"})
            elif child.is_file():
                sources.append(child)
            else:
                omissions.append({"path": relative, "reason": "not a regular file"})
    reason = _guard(root, root, excluded, marker_cache)
    if reason:
        omissions.append({"path": ".", "reason": reason})
    else:
        visit(root)
    return sources, omissions


def _receipt_paths(values):
    paths, issues = [], []
    for value in values:
        supplied = _absolute(value)
        if supplied.is_file():
            paths.append(supplied)
        elif supplied.is_dir():
            # Metadata traversal only. Never descend into hidden history/staging.
            for folder, directories, names in os.walk(supplied, followlinks=False):
                directories[:] = sorted(d for d in directories if not d.startswith(".")
                                         and not (Path(folder) / d).is_symlink())
                paths.extend(Path(folder) / name for name in sorted(names)
                             if name in {".processing.json", ".filing-current.json", "conversion.json"})
        else:
            issues.append({"path": str(supplied), "kind": "unavailable_control", "error": "Reuse root does not exist"})
    return list(dict.fromkeys(paths)), issues


def _reuse_import(values, selected):
    """Return exact-path candidates and recorded failures without reading text."""
    paths, issues = _receipt_paths(values)
    records, history = {}, {}
    for receipt in paths:
        try:
            raw = _control(receipt)
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise IntegrityError("Control record must be a JSON object")
            pin = hashlib.sha256(raw).hexdigest()
            folder = receipt.parent
            rows = []
            if data.get("kind") == "scan-filing":
                from .filing import _current_snapshot
                _current_snapshot(data)
                if (folder / ".filing.pending.json").exists():
                    raise IntegrityError("Filing has pending publication")
                original_root = _locator(folder, data["source_root"])
                members = data["sources"]
                member_names = [r["source"] for r in members]
                filing_orders = [r["order"] for r in members]
                if (len(set(member_names)) != len(member_names)
                        or any(type(v) is not int or v < 1 for v in filing_orders)
                        or len(set(filing_orders)) != len(filing_orders)):
                    raise IntegrityError("Repeated filing membership or ambiguous filing order")
                reading = [r.get("reading_order") for r in members]
                has_order = any(value is not None for value in reading)
                if has_order and (any(type(v) is not int or v < 1 for v in reading)
                                  or len(set(reading)) != len(reading)):
                    raise IntegrityError("Ambiguous accepted reading order")
                for row in sorted(members, key=lambda r: r["reading_order"] if has_order else r["order"]):
                    original = _relative(original_root, row["source"])
                    if str(original) not in selected:
                        continue
                    gaps = [str(row[k]) for k in ("gap_reason", "reason") if row.get(k)]
                    if row.get("disposition") == "unavailable":
                        gaps.append("Recorded original unavailable")
                    if not row.get("preferred_text"):
                        gaps.append("No accepted preferred extraction recorded")
                    provenance = {"format": "scan-filing", "assembly_version": 1,
                                  "disposition": row["disposition"], "source": row["source"],
                                  "readme_path": str(folder / "README.md"),
                                  "readme_block_sha256": data["readme_block_sha256"],
                                  "preferred_text": deepcopy(row.get("preferred_text")),
                                  "complementary_text": deepcopy(row.get("complementary_text", []))}
                    linked = []
                    for choice in ([row["preferred_text"]] if row.get("preferred_text") else []) + row.get("complementary_text", []):
                        linked.append({"path": str(_locator(folder, choice["path"])), "sha256": choice["sha256"]})
                    rows.append((original, row.get("source_sha256"), row["text"], row["text_sha256"],
                                 gaps, {"group": str(folder), "name": folder.name,
                                        "notes": gaps, "capture_order": row.get("capture_order"),
                                        "reading_order": row.get("reading_order"),
                                        "reading_order_evidence": row.get("reading_order_evidence"),
                                        "filing_order": row["order"], "provenance": provenance,
                                        "linked_text_pins": linked,
                                        "source_size": row.get("source_bytes"),
                                        "source_mtime_ns": row.get("source_mtime_ns"),
                                        "notes_source": _notes_metadata(folder / "README.md")}, True))
            elif isinstance(data.get("specification"), dict) and data["specification"].get("kind") == "scans":
                from .scans import _scan_receipt, page_output_prefix
                spec = _scan_receipt(data)
                source_base = _locator(folder, data["source_root"]["relative_to_job"])
                keys = spec["sources"]
                if len(keys) != len(set(keys)):
                    raise IntegrityError("Repeated scan source locator")
                for key in keys:
                    original = _relative(source_base, key)
                    step_key, text = key, page_output_prefix(key) + ".txt"
                    if str(original) not in selected:
                        continue
                    # receipt_sources resolves locators; independently reject symlinks.
                    original = _absolute(original)
                    step = data.get("steps", {}).get(step_key, {})
                    source_pin = data.get("sources", {}).get(key, {}).get("sha256")
                    details = step.get("details", {})
                    gaps = []
                    for key_name in ("blank_text", "blank_pdf_pages"):
                        if details.get(key_name):
                            gaps.append({key_name: details[key_name]})
                    failure = data.get("intake_failures", {}).get(key)
                    if failure:
                        gaps.append({"intake_failure": failure})
                    if key in data.get("tiff_safety", {}):
                        gaps.append({"legacy_tiff_coverage": data["tiff_safety"][key]})
                    if step.get("status") != "done" or text not in step.get("outputs", {}):
                        history[str(original)] = {"coverage": "partial", "receipt_path": str(receipt),
                                                  "receipt_sha256": pin, "source_sha256": source_pin,
                                                  "reason": step.get("error") or failure or "No completed source extract"}
                        continue
                    rows.append((original, source_pin, text, step["outputs"][text], gaps,
                                 {"group": None, "name": original.name, "notes": deepcopy(details.get("notes", [])),
                                  "source_size": data.get("sources", {}).get(key, {}).get("bytes"),
                                  "provenance": {"format": "scans", "adapter_version": 4,
                                                 "source": key, "details": deepcopy(details)}}, False))
            elif data.get("kind") == "docling-folder" and data.get("schema") == 1:
                # Adequacy is a receipt claim pending verify_bundle at execution.
                portable_root = _absolute(data.get("source_root") or data.get("specification", {}).get("source_root") or folder)
                for member in data.get("sources", []):
                    source = member.get("source", {})
                    original = (_absolute(source["absolute_path"]) if source.get("absolute_path")
                                else _relative(portable_root, source["relative_path"]))
                    if str(original) not in selected:
                        continue
                    source_map = member.get("map", {})
                    item = _relative(folder, member["item_relative"])
                    reading = source_map.get("reading_path", "reading.md")
                    files = source_map.get("files", [])
                    reading_pin = next((f.get("sha256") for f in files if f.get("path") == reading), None)
                    if not _hash(reading_pin):
                        raise IntegrityError("Portable reading lacks a receipt hash pin")
                    gaps = list(source_map.get("gaps", []))
                    if member.get("state") != "complete":
                        gaps.append("Portable source publication is " + str(member.get("state")))
                    complete_claim = (data.get("coverage_accounted") is True and
                                      data.get("conversion_complete") is True and data.get("publication_complete") is True
                                      and member.get("state") == "complete")
                    rows.append((original, source.get("sha256"), str(_relative(item, reading)), reading_pin,
                                 gaps, {"group": source.get("reuse", {}).get("group") if source.get("reuse") else None,
                                        "name": original.name, "notes": [], "portable_bundle": str(folder),
                                        "portable_adequate": bool(complete_claim),
                                        "portable_verification": "pending current bundle verification",
                                        "provenance": {"format": "docling-folder", "source_map": deepcopy(source_map)},
                                        "source_size": source.get("size")}, True))
            elif data.get("kind") == "retained-text" and data.get("schema") == 1:
                if set(data) != {"kind", "schema", "entries"}:
                    raise IntegrityError("Malformed retained-text index")
                for row in data["entries"]:
                    original = _locator(folder, row["source"]) if not Path(row["source"]).is_absolute() else _absolute(row["source"])
                    if str(original) not in selected:
                        continue
                    text = _locator(folder, row["text"]) if not Path(row["text"]).is_absolute() else _absolute(row["text"])
                    rows.append((original, row["source_sha256"], str(text), row["text_sha256"], [],
                                 {"group": None, "name": original.name, "notes": [],
                                  "provenance": {"format": "retained-text", "mapping": deepcopy(row)}}, False))
            else:
                raise ValueError("Unsupported reuse control format")
            for original, source_pin, text, text_pin, gaps, extra, filed in rows:
                text_path = _absolute(text) if Path(text).is_absolute() else _relative(folder, text)
                if not _hash(source_pin) or not _hash(text_pin):
                    history[str(original)] = {"coverage": "unresolved", "reason": "Missing valid source/text hash", "receipt_path": str(receipt)}
                    continue
                record = {"text_path": str(text_path), "text_sha256": text_pin, "source_sha256": source_pin,
                          "receipt_path": str(receipt), "receipt_sha256": pin, "gaps": gaps,
                          "verification": "receipt pins only; source and text verification pending",
                          "accepted_filing": filed, **extra}
                records.setdefault(str(original), []).append(record)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
                raise
            issues.append({"path": str(receipt), "kind": "unavailable_control" if not isinstance(exc, IntegrityError) else "conflicting_control",
                           "error": str(exc)})
    chosen = {}
    for source, candidates in records.items():
        filing = [r for r in candidates if r["accepted_filing"]]
        preferred = filing or candidates
        identities = {(r["source_sha256"], r["text_sha256"], r["text_path"], r.get("group")) for r in preferred}
        prior = history.get(source)
        conflicting_history = (prior and _hash(prior.get("source_sha256"))
                               and any(r["source_sha256"] != prior["source_sha256"] for r in candidates))
        if len(identities) > 1 or len({r["source_sha256"] for r in candidates}) > 1 or conflicting_history:
            history[source] = {"coverage": "unresolved", "reason": "Conflicting accepted readings or source identities",
                               "candidates": candidates}
        else:
            chosen[source] = preferred[0]
            # A filed reading supersedes failed baseline extraction for the same
            # pinned original; an identity disagreement always remains unresolved.
            if prior and prior.get("coverage") == "unresolved":
                chosen.pop(source, None)
    return chosen, history, issues


def _notes_metadata(path):
    path = _absolute(path)
    try:
        info = path.stat()
        return {"path": str(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                "locally_available": not _dataless(info)}
    except OSError:
        return {"path": str(path), "size": None, "mtime_ns": None, "locally_available": False}


def _entry(root, path):
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise IntegrityError("Source is not a regular file: " + str(path))
        size, mtime = info.st_size, info.st_mtime_ns
        available = not _dataless(info)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise
        size = mtime = None
        available = False
    return {"relative_path": path.relative_to(root).as_posix(), "kind": KINDS.get(path.suffix.lower(), "unknown"),
            "size": size, "mtime_ns": mtime, "sha256": None, "hash_status": "pending_intake",
            "reuse": None, "prior_coverage": "no_recorded_processing", "locally_available": available}


def _folder_plan(root, output, *, recursive, explicit, exclude, reuse_from):
    root, output = _absolute(root), _absolute(output)
    if not root.is_dir():
        raise ValueError("Source root must be an existing directory")
    if _overlap(root, output):
        raise IntegrityError("Original and output directory trees overlap")
    excluded = _excluded_paths(root, exclude)
    paths, omissions = _enumerate(root, recursive, excluded, explicit=explicit)
    selected = {str(p) for p in paths}
    reused, history, issues = _reuse_import(reuse_from, selected)
    entries = []
    for path in paths:
        row = _entry(root, path)
        retained = reused.get(str(path))
        previous = history.get(str(path))
        if retained and previous and previous.get("coverage") == "unresolved":
            retained = None
        if retained:
            if row["kind"] == "tiff" and retained["provenance"]["format"] != "docling-folder":
                retained["gaps"].append("Legacy TIFF source-level reading lacks verified frame mapping; frame count pending")
            row.update(reuse=retained, sha256=retained["source_sha256"], hash_status="receipt_pin_pending_intake",
                       prior_coverage="partial" if retained["gaps"] else "verified_complete",
                       prior_coverage_basis="Authoritative receipt claim; exact original/text verification pending")
            # Verified_complete names recorded extraction coverage, never current
            # original verification; hash_status makes that distinction explicit.
            for field in ("capture_order", "reading_order", "reading_order_evidence"):
                if retained.get(field) is not None:
                    row[field] = retained[field]
        elif previous:
            row["prior_coverage"] = previous["coverage"]
            row["prior_evidence"] = previous
            if _hash(previous.get("source_sha256")):
                row.update(sha256=previous["source_sha256"], hash_status="receipt_pin_pending_intake")
        elif issues:
            row["prior_coverage"] = "unresolved"
            row["prior_evidence"] = {"reason": "Supplied reuse controls could not all be inspected; see collection control issues"}
        entries.append(row)
    if explicit is None:
        # Order accepted groups by scoped receipt order, preserving each receipt's
        # supported reading order, then append uncovered sources in path order.
        rank = {source: i for i, source in enumerate(reused)}
        entries.sort(key=lambda row: (0, rank[str(root / row["relative_path"])])
                     if str(root / row["relative_path"]) in rank else (1, row["relative_path"]))
    for order, row in enumerate(entries, 1):
        row["order"] = order
    return {"kind": "docling-folder", "schema": 1, "source_root": str(root), "output": str(output),
            "sources": entries, "exclusions": omissions, "issues": issues,
            "selection": {"recursive": bool(recursive), "fixed": True,
                          "order_basis": "explicit source list" if explicit is not None else "accepted reading/filing order, then relative path"}}


def build_folder_plan(root, output, *, recursive=False, sources_file=None, exclude=(), reuse_from=()):
    """Freeze safe exact source paths without opening source/text payloads.

    Source-list relative lines follow the existing CLI convention: relative to
    the list's own directory. Archive binding source lists are root-relative.
    """
    explicit = None
    if sources_file is not None:
        filename = _absolute(sources_file)
        raw = _control(filename).decode("utf-8-sig")
        base = _absolute(root)
        explicit = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            selected = _absolute(Path(line) if Path(line).is_absolute() else filename.parent / line)
            if not selected.is_relative_to(base):
                raise IntegrityError("Source-list path escapes source root: " + line)
            explicit.append(selected.relative_to(base).as_posix())
    return _folder_plan(root, output, recursive=recursive, explicit=explicit,
                        exclude=exclude, reuse_from=reuse_from)


def _sniff(path):
    with path.open("rb") as stream:
        head = stream.read(8192)
    if head.startswith(b"%PDF-") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return "tiff"
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
        return "image"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if "word/document.xml" in names:
                    return "docx"
        except (OSError, zipfile.BadZipFile):
            # A damaged DOCX still belongs to bounded package salvage.
            if path.suffix.lower() == ".docx":
                return "docx"
        return "unknown"
    if head.startswith((b"\x00\x00\xfe\xff", b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return "txt"
    if b"\x00" not in head:
        try:
            head.decode("utf-8")
            return "txt"
        except UnicodeDecodeError:
            if path.suffix.lower() == ".txt":
                return "txt"  # Recovery owns explicit legacy encoding candidates.
    return "unknown"


def _fetch_exact(path):
    last = None
    for _ in range(2):
        try:
            last, ok = fetch([path])
        except RuntimeError as exc:
            last = {"error": str(exc)}
            continue
        if ok:
            return last
    raise RuntimeError("Exact-path intake unavailable: " + str(path) + "; " + json.dumps(last))


def _verify_pin(path, expected, *, fetch_inputs=False):
    path = _absolute(path)
    if not _hash(expected):
        raise IntegrityError("Invalid recorded SHA-256 pin: " + str(path))
    intake = None
    if fetch_inputs:
        intake = _fetch_exact(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise IntegrityError("Input is not a regular file: " + str(path))
    if _dataless(before):
        raise RuntimeError("Dataless exact input requires fetch: " + str(path))
    value = checksum(path)
    after = path.stat()
    if _identity(before) != _identity(after) or value != expected:
        raise IntegrityError("Pinned input changed: " + str(path))
    return intake


def prepare_source(root, entry, *, fetch_inputs=False, notes_snapshot=None):
    """Bind exact original and retained reading hashes before conversion."""
    root = _absolute(root)
    result = deepcopy(entry)
    path = _relative(root, result["relative_path"])
    reason = _guard(root, path, ())
    if reason:
        raise IntegrityError("Selected source became guarded: " + reason)
    try:
        before_fetch = path.stat()
    except FileNotFoundError:
        if not fetch_inputs or result.get("size") is not None:
            raise
        before_fetch = None
    if before_fetch is not None and not stat.S_ISREG(before_fetch.st_mode):
        raise IntegrityError("Selected source is not a regular file")
    if before_fetch is not None and (result.get("size") is not None and before_fetch.st_size != result["size"]
            or result.get("mtime_ns") is not None and before_fetch.st_mtime_ns != result["mtime_ns"]):
        raise IntegrityError("Source metadata differs from frozen plan: " + str(path))
    intake = None
    if fetch_inputs:
        intake = _fetch_exact(path)
    elif "com~apple~CloudDocs" in path.parts:
        raise RuntimeError("Exact iCloud source intake requires fetch_inputs=True: " + str(path))
    before = path.stat()
    if _dataless(before):
        raise RuntimeError("Dataless source requires exact-path fetch: " + str(path))
    value = checksum(path)
    detected = _sniff(path)
    after = path.stat()
    if _identity(before) != _identity(after):
        raise IntegrityError("Original changed during intake: " + str(path))
    if (result.get("size") is not None and after.st_size != result["size"]
            or result.get("mtime_ns") is not None and after.st_mtime_ns != result["mtime_ns"]):
        raise IntegrityError("Original metadata changed during intake: " + str(path))
    if result.get("sha256") is not None and result["sha256"] != value:
        raise IntegrityError("Original differs from recorded source hash: " + str(path))
    retained = result.get("reuse")
    if retained:
        if retained.get("source_sha256") != value:
            raise IntegrityError("Retained text belongs to a different original")
        for linked, pin in [(retained["receipt_path"], retained["receipt_sha256"]),
                            (retained["text_path"], retained["text_sha256"])]:
            _verify_pin(linked, pin, fetch_inputs=fetch_inputs)
        for pin in retained.get("linked_text_pins", []):
            _verify_pin(pin["path"], pin["sha256"], fetch_inputs=fetch_inputs)
        # Strictly decode accepted text; never silently replace it with a candidate.
        accepted = Path(retained["text_path"]).read_bytes()
        if hashlib.sha256(accepted).hexdigest() != retained["text_sha256"]:
            raise IntegrityError("Accepted text changed during intake")
        accepted.decode("utf-8-sig")
        notes = retained.get("notes_source")
        if notes:
            note_path = _absolute(notes["path"])
            if notes_snapshot is not None:
                if notes_snapshot.get("notes_path") != str(note_path) or not isinstance(notes_snapshot.get("notes_text"), str):
                    raise IntegrityError("Invalid pinned document-note snapshot")
                note_bytes = notes_snapshot["notes_text"].encode("utf-8")
                pin = notes_snapshot.get("notes_sha256")
                # Older receipts decoded UTF-8 BOMs before storing the snapshot.
                if hashlib.sha256(note_bytes).hexdigest() != pin:
                    note_bytes = b"\xef\xbb\xbf" + note_bytes
                if hashlib.sha256(note_bytes).hexdigest() != pin or len(note_bytes) != notes.get("size"):
                    raise IntegrityError("Pinned document-note snapshot hash/size differs")
            else:
                if fetch_inputs:
                    _fetch_exact(note_path)
                note_info = note_path.stat()
                if ((notes.get("size") is not None and note_info.st_size != notes["size"])
                        or (notes.get("mtime_ns") is not None and note_info.st_mtime_ns != notes["mtime_ns"])):
                    raise IntegrityError("Document notes changed since frozen planning")
                note_bytes = _control(note_path)
            retained.update(notes_text=note_bytes.decode("utf-8-sig"), notes_path=str(note_path),
                            notes_sha256=hashlib.sha256(note_bytes).hexdigest())
            block_pin = retained["provenance"].get("readme_block_sha256")
            if block_pin:
                from .filing import _block
                block = _block(retained["notes_text"])
                if block is None or hashlib.sha256(block.encode()).hexdigest() != block_pin:
                    raise IntegrityError("Document README membership differs from accepted filing receipt")
            retained["provenance"]["notes_role"] = "Retained document README; descriptive evidence, not source transcription"
        retained["verification"] = "source, receipt and accepted text hashes verified"
    result.update(absolute_path=str(path), sha256=value, hash_status="verified_intake",
                  kind=detected, size=after.st_size, mtime_ns=after.st_mtime_ns,
                  planning_kind=entry.get("kind"), locally_available=True)
    if detected != entry.get("kind"):
        result["format_warning"] = "Content-detected format differs from suffix-based planning kind"
    if intake is not None:
        result["intake"] = intake
    return result


def build_archive_plan(root, *, bindings=None, exclusions=()):
    """Reconcile finite source sets; unknown/unbound material remains visible."""
    root = _absolute(root)
    if not root.is_dir():
        raise ValueError("Archive root must be an existing directory")
    excluded = _excluded_paths(root, exclusions)
    candidate_paths, omissions = _enumerate(root, True, excluded)
    jobs, unresolved, claimed = [], [], set()
    for number, binding in enumerate(bindings or [], 1):
        name = binding.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Archive bindings require a collection name")
        source_root = _absolute(root / binding["source_root"])
        if not source_root.is_relative_to(root):
            raise IntegrityError("Binding source root escapes selected archive")
        if not binding.get("destination"):
            raise ValueError("Archive bindings require a separate destination")
        output = _absolute(root / binding["destination"])
        explicit = binding.get("sources")
        if explicit is not None and not isinstance(explicit, list):
            raise ValueError("Binding sources must be a fixed list")
        reuse = [_absolute(root / p) for p in binding.get("reuse_from", [])]
        local_exclude = [e.relative_to(source_root).as_posix() for e in excluded if e.is_relative_to(source_root)]
        local_exclude.extend(binding.get("exclude", []))
        folder = _folder_plan(source_root, output, recursive=binding.get("recursive", False),
                              explicit=explicit, exclude=local_exclude, reuse_from=reuse)
        omissions.extend({**item, "collection": name, "source_root": str(source_root)} for item in folder["exclusions"])
        unresolved.extend({**issue, "collection": name} for issue in folder["issues"])
        by_action = {}
        for source in folder["sources"]:
            identity = str(source_root / source["relative_path"])
            if identity in claimed:
                raise IntegrityError("Overlapping archive source assignment: " + identity)
            claimed.add(identity)
            if source["kind"] == "unknown":
                source["context_disposition"] = "unsupported/context; suffix not in conversion allowlist"
            action = ("defer" if source["prior_coverage"] == "unresolved" or source["kind"] == "unknown" else
                      "reuse" if (source.get("reuse") or {}).get("portable_adequate") else
                      "enrich" if source.get("reuse") else "convert")
            by_action.setdefault(action, []).append(source)
        for part, (action, sources) in enumerate(by_action.items(), 1):
            job_id = f"job-{number:04d}-{part:02d}"
            # Split mixed legacy/new sets into separate fresh bundle folders.
            target = output / action if len(by_action) > 1 else output
            prerequisites = deepcopy(binding.get("prerequisites", []))
            requirements = deepcopy(binding.get("readiness_requirements", []))
            if not isinstance(requirements, list) or any(not isinstance(value, str) for value in requirements):
                raise IntegrityError("Readiness requirements must be a list of human-readable strings")
            state = binding.get("scope_state", "defer")
            if action == "defer":
                state = "defer"
                requirements.append("Resolve unsupported/context format or source identity/reading conflict")
            elif state == "defer":
                requirements.append("Execution approval required")
            jobs.append({"id": job_id, "action": action, "scope_state": state,
                         "source_root": str(source_root), "output": str(target), "sources": sources,
                         "collection": name, "status": binding.get("status"),
                         "priority": binding.get("priority", 100), "prerequisites": prerequisites,
                         "readiness_requirements": list(dict.fromkeys(requirements)),
                         "prior_evidence": list(dict.fromkeys(s["reuse"]["receipt_path"] for s in sources if s.get("reuse"))),
                         "exclusions": folder["exclusions"], "issues": folder["issues"]})
    jobs.sort(key=lambda j: (j["priority"], j["id"]))
    inventory = []
    job_by_source = {str(Path(j["source_root"]) / s["relative_path"]): j["id"] for j in jobs for s in j["sources"]}
    for source in candidate_paths:
        row = _entry(root, source)
        row["job_id"] = job_by_source.get(str(source))
        if row["job_id"] is None:
            row["disposition"] = "defer"
            row["reason"] = "Unbound material requires a named scope and separate destination"
            unresolved.append({"path": row["relative_path"], "kind": "unbound_source", "reason": row["reason"]})
        else:
            row["disposition"] = "job"
        inventory.append(row)
    # Explicit unavailable paths still belong to the finite inventory.
    known = {str(root / r["relative_path"]) for r in inventory}
    for job in jobs:
        for source in job["sources"]:
            path = Path(job["source_root"]) / source["relative_path"]
            if str(path) not in known:
                row = deepcopy(source)
                row.update(relative_path=path.relative_to(root).as_posix(), job_id=job["id"], disposition="job")
                inventory.append(row)
                known.add(str(path))
    plan = {"kind": "docling-archive-plan", "schema": 1, "source_root": str(root), "jobs": jobs,
            "exclusions": omissions, "unresolved": unresolved, "inventory": inventory,
            "selection_fixed": True,
            "coverage_note": "Receipt-derived prior extraction coverage; planning does not verify source/text payloads or authorize execution"}
    validate_archive_plan(plan)
    return plan


def _validate_archive_plan(plan):
    """Validate frozen scope structurally; never walk a folder to expand it."""
    if not isinstance(plan, dict) or plan.get("kind") != "docling-archive-plan" or type(plan.get("schema")) is not int or plan["schema"] != 1:
        raise IntegrityError("Unsupported consolidated archive plan")
    root = _absolute(plan["source_root"])
    if str(root) != plan["source_root"]:
        raise IntegrityError("Archive source root must be absolute and normalized")
    if not isinstance(plan.get("jobs"), list) or not isinstance(plan.get("inventory"), list):
        raise IntegrityError("Archive plan requires jobs and finite inventory")
    ids, selected, outputs = set(), set(), []
    selected_jobs = {}
    originals = []
    for job in plan["jobs"]:
        if job.get("id") in ids or not isinstance(job.get("id"), str):
            raise IntegrityError("Invalid or duplicate job ID")
        ids.add(job["id"])
        if job.get("action") not in {"reuse", "enrich", "convert", "defer"} or job.get("scope_state") not in {"ready", "defer"}:
            raise IntegrityError("Invalid action or scope state")
        source_root, output = _absolute(job["source_root"]), _absolute(job["output"])
        if str(source_root) != job["source_root"] or str(output) != job["output"] or not source_root.is_relative_to(root):
            raise IntegrityError("Job roots must be absolute and archive-constrained")
        if _overlap(source_root, output):
            raise IntegrityError("Job original/output trees overlap")
        if any(_overlap(output, prior) for prior in outputs):
            raise IntegrityError("Overlapping job output trees")
        outputs.append(output)
        originals.append(source_root)
        rows = job.get("sources")
        if not isinstance(rows, list) or not rows:
            raise IntegrityError("Jobs require a nonempty frozen source set")
        orders = set()
        for row in rows:
            path = _relative(source_root, row["relative_path"])
            if path in selected:
                raise IntegrityError("Overlapping exact source paths across jobs")
            selected.add(path)
            selected_jobs[path] = job["id"]
            if (type(row.get("order")) is not int or row["order"] < 1 or row["order"] in orders
                    or row.get("prior_coverage") not in COVERAGE or row.get("kind") not in {*KINDS.values(), "unknown"}):
                raise IntegrityError("Invalid source metadata or order")
            orders.add(row["order"])
            if (row.get("size") is not None and (type(row["size"]) is not int or row["size"] < 0)
                    or row.get("mtime_ns") is not None and type(row["mtime_ns"]) is not int):
                raise IntegrityError("Invalid frozen source stat metadata")
            if row["kind"] == "unknown" and job["action"] != "defer":
                raise IntegrityError("Unsupported/context inventory cannot be conversion-ready")
            if row.get("sha256") is not None and not _hash(row["sha256"]):
                raise IntegrityError("Invalid source SHA-256 pin")
            retained = row.get("reuse")
            if retained is not None:
                if not isinstance(retained, dict) or retained.get("source_sha256") != row.get("sha256"):
                    raise IntegrityError("Reuse source pin differs from frozen original identity")
                for field, pin_field in (("text_path", "text_sha256"), ("receipt_path", "receipt_sha256")):
                    target = _absolute(retained[field])
                    if str(target) != retained[field] or not _hash(retained.get(pin_field)):
                        raise IntegrityError("Invalid retained reading path/hash pin")
            reason = _guard(source_root, path, ())
            if reason:
                raise IntegrityError("Plan selects a guarded derivative/control: " + reason)
    # Dependencies are executable job-ID edges. Human approval/history notes
    # live in readiness_requirements and never become phantom graph vertices.
    dependents = {job_id: [] for job_id in ids}
    remaining = {}
    for job in plan["jobs"]:
        prerequisites = job.get("prerequisites", [])
        if (not isinstance(prerequisites, list)
                or any(not isinstance(value, str) or not value for value in prerequisites)):
            raise IntegrityError("Job prerequisites must be a list of job IDs")
        if len(set(prerequisites)) != len(prerequisites):
            raise IntegrityError("Repeated job prerequisite")
        if any(value not in ids for value in prerequisites):
            raise IntegrityError("Unknown job prerequisite")
        if job["id"] in prerequisites:
            raise IntegrityError("Job cannot depend on itself")
        requirements = job.get("readiness_requirements", [])
        if not isinstance(requirements, list) or any(not isinstance(value, str) for value in requirements):
            raise IntegrityError("Readiness requirements must be a list of human-readable strings")
        remaining[job["id"]] = len(prerequisites)
        for dependency in prerequisites:
            dependents[dependency].append(job["id"])
    ready = [job_id for job_id, count in remaining.items() if count == 0]
    position = 0
    while position < len(ready):
        for job_id in dependents[ready[position]]:
            remaining[job_id] -= 1
            if remaining[job_id] == 0:
                ready.append(job_id)
        position += 1
    if len(ready) != len(ids):
        raise IntegrityError("Cyclic job prerequisites")
    for output in outputs:
        if any(_overlap(output, original) for original in originals):
            raise IntegrityError("A job output overlaps another original tree")
    inventory_seen = set()
    accounted = set()
    for row in plan["inventory"]:
        path = _relative(root, row["relative_path"])
        if path in inventory_seen:
            raise IntegrityError("Repeated inventory path")
        inventory_seen.add(path)
        if row.get("job_id") is not None:
            if row["job_id"] not in ids or path not in selected:
                raise IntegrityError("Inventory job identity does not match frozen sources")
            expected = selected_jobs[path]
            if row["job_id"] != expected:
                raise IntegrityError("Inventory names the wrong source job")
            accounted.add(path)
        elif row.get("disposition") != "defer" or not row.get("reason"):
            raise IntegrityError("Unassigned inventory requires an explicit defer reason")
    if accounted != selected:
        raise IntegrityError("Frozen jobs and archive inventory do not account for the same sources")
    return None


def validate_archive_plan(plan):
    """Raise IntegrityError for malformed, overlapping or unsafe frozen plans."""
    try:
        return _validate_archive_plan(plan)
    except IntegrityError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise IntegrityError("Malformed archive plan: " + str(exc)) from exc


def write_archive_plan(plan, path):
    validate_archive_plan(plan)
    target = _absolute(path)
    atomic(target, json_bytes(plan))
    readable = target.with_suffix(".md")
    lines = ["# Frozen archive conversion plan", "", plan.get("coverage_note", ""), "",
             "Originals and accepted readings remain read-only. Exact source sets follow.", ""]
    for job in plan["jobs"]:
        lines += [f"## {job['id']}: {job['collection']} — {job['action']} ({job['scope_state']})", "",
                  f"Source root: `{job['source_root']}`", f"Output: `{job['output']}`", "",
                  "Job prerequisites: " + (json.dumps(job.get("prerequisites", []), ensure_ascii=False) if job.get("prerequisites") else "none"),
                  "Readiness requirements: " + (json.dumps(job.get("readiness_requirements", []), ensure_ascii=False) if job.get("readiness_requirements") else "none"), "",
                  "| Order | Exact original relative path | Prior coverage | Receipt hash pin |", "| --- | --- | --- | --- |"]
        for source in job["sources"]:
            name = source["relative_path"].replace("|", "&#124;")
            lines.append(f"| {source['order']} | {name} | {source['prior_coverage']} | {source.get('sha256') or 'pending exact intake'} |")
        lines.append("")
    lines += ["## Deferred/unresolved", "", *["- " + json.dumps(item, ensure_ascii=False) for item in plan["unresolved"]], "",
              "## Exclusions", "", *["- " + json.dumps(item, ensure_ascii=False) for item in plan["exclusions"]], ""]
    atomic(readable, "\n".join(lines))
    return {"json": str(target), "markdown": str(readable)}


def load_archive_plan(path):
    plan = json.loads(_control(_absolute(path), limit=256 * 1024 * 1024))
    validate_archive_plan(plan)
    return plan
