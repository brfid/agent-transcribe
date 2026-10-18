"""Explicit, resumable scan filing and read-only preferred-text selection.

Descriptive prose belongs to README.md. Recipes are proposed source assignments;
receipts record the accepted source map, exact exports and mechanical evidence.
No directory walk selects originals or starts OCR.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import quote, unquote

from .common import IntegrityError, atomic, atomic_copy, checksum, fetch, json_bytes, now

START = "<!-- agent-transcribe:source-map:start -->"
END = "<!-- agent-transcribe:source-map:end -->"
DISPOSITIONS = {"document", "context", "review", "unavailable"}
RECEIPT = ".filing-current.json"
COMBINED = "OCR discovery text.txt"


def _digest(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _relative(root, value):
    """Recipe source/output names never escape their explicitly supplied root."""
    if not isinstance(value, str):
        raise ValueError("A path must be a string")
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or "\n" in value or "\r" in value:
        raise ValueError("Expected a nonempty root-relative path: " + str(value))
    result = root / path
    current = root
    if current.is_symlink():
        raise IntegrityError("A filing path root is a symlink: " + str(current))
    for component in path.parts:
        current = current / component
        if current.is_symlink():
            raise IntegrityError("A filing path component is a symlink: " + str(current))
    if not result.resolve().is_relative_to(root.resolve()):
        raise IntegrityError("Path escapes its root or is a symlink: " + str(result))
    return result


def _locator(root, value):
    """Receipt locators may ascend to original/text roots, but not through symlinks."""
    path = Path(value)
    if path.is_absolute():
        raise IntegrityError("A new filing receipt must use relative locators")
    result = root / path
    current = root
    if current.is_symlink():
        raise IntegrityError("A recorded root became a symlink: " + str(current))
    for component in path.parts:
        current = current / component
        if current.is_symlink():
            raise IntegrityError("A recorded path became a symlink: " + str(current))
    return result.resolve()


def _rel(path, root):
    return os.path.relpath(path, root)


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise IntegrityError("Expected an existing regular file: " + str(path))
    if getattr(path.stat(), "st_flags", 0) & 0x40000000:
        raise IntegrityError("File is dataless; exact-path fetch required: " + str(path))


def _verify(path, expected):
    _regular(path)
    if checksum(path) != expected:
        raise IntegrityError("Recorded bytes changed: " + str(path))


@contextlib.contextmanager
def _lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / ".filing.lock"
    if lock.is_symlink():
        raise IntegrityError("Filing lock is a symlink")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntegrityError("Another filing writer owns " + str(folder)) from exc
        yield


def _block(text):
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise IntegrityError("README contains damaged or repeated source-map markers")
    if START not in text:
        return None
    start, end = text.index(START), text.index(END) + len(END)
    if end < start:
        raise IntegrityError("README source-map markers are reversed")
    return text[start:end]


def _merge_readme(text, new_block, old_hash):
    old = _block(text)
    if old == new_block:
        return text
    if (None if old is None else _digest(old)) != old_hash:
        raise IntegrityError("README source membership was edited; reconcile it before filing")
    if old is None:
        return text.rstrip() + "\n\n" + new_block + "\n"
    return text.replace(old, new_block, 1)


def _link(path):
    return quote(str(path), safe="/")


def _reading_order(rows):
    specified = [r.get("reading_order") for r in rows if r.get("reading_order") is not None]
    if len(specified) != len(set(specified)):
        raise IntegrityError("Repeated asserted reading positions; reconcile source order")
    if specified and len(specified) == len(rows):
        return sorted(rows, key=lambda row: row["reading_order"]), "supported reading order"
    return rows, "declared filing order; capture order and physical page order are not inferred"


def _map(folder, state):
    ordered, order_note = _reading_order(state["sources"])
    lines = [START, "## Source map", "", "Displayed in " + order_note + ". "
             "Filing numbers preserve filenames; capture and reading positions are separate evidence.", "",
             "| Filing scan | Capture position | Reading position | Original | Image | Preferred text | Disposition | Text coverage |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    root = _locator(folder, state["source_root"])
    for row in ordered:
        source = _rel(root / row["source"], folder)
        image = f"[image](<{_link(row['copy'])}>)" if row.get("copy") else "Unavailable"
        coverage = "Explicit gap" if row.get("gap_reason") else "Machine reading; unverified"
        label = row["source"].replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
        disposition = row["disposition"]
        lines.append(f"| {row['order']:04d} | {row.get('capture_order', 'Unspecified')} | {row.get('reading_order', 'Unspecified')} | [{label}](<{_link(source)}>) | {image} | "
                     f"[text](<{_link(row['text'])}>) | {disposition} | {coverage} |")
    evidence = list(dict.fromkeys(r["reading_order_evidence"] for r in ordered if r.get("reading_order_evidence")))
    if evidence:
        lines += ["", "Reading-order evidence:", "", *["- " + item for item in evidence]]
    lines += ["", f"Selected sources: {len(state['sources'])}. Unavailable originals: "
              f"{sum(r['disposition'] == 'unavailable' for r in state['sources'])}. "
              f"Explicit text gaps: {sum(bool(r.get('gap_reason')) for r in state['sources'])}.", END]
    return "\n".join(lines)


def _markdown_targets(text, root):
    # Handles both <URL> targets and balanced parentheses in ordinary links.
    targets = []
    position = 0
    while True:
        start = text.find("](", position)
        if start < 0:
            break
        i = start + 2
        if i < len(text) and text[i] == "<":
            end = text.find(">)", i)
            if end < 0:
                break
            value, position = text[i + 1:end], end + 2
        else:
            depth, end = 1, i
            while end < len(text) and depth:
                depth += (text[end] == "(") - (text[end] == ")")
                end += 1
            if depth:
                break
            value, position = text[i:end - 1], end
        if "://" not in value and not value.startswith("#"):
            targets.append((root / unquote(value.split("#", 1)[0])).resolve())
    return targets


def _current_snapshot(result):
    if (not isinstance(result, dict) or type(result.get("schema")) is not int
            or result["schema"] != 1 or result.get("kind") != "scan-filing"
            or type(result.get("assembly_version")) is not int or result["assembly_version"] != 1
            or "legacy_receipt" in result):
        raise ValueError("Unsupported filing receipt format")
    if (not isinstance(result.get("sources"), list) or not result["sources"]
            or not isinstance(result.get("source_root"), str)
            or not isinstance(result.get("readme_block_sha256"), str)
            or not isinstance(result.get("combined"), dict)
            or not {"path", "sha256"} <= result["combined"].keys()):
        raise IntegrityError("Malformed filing receipt")
    required = {"source", "order", "disposition", "text", "text_sha256"}
    if any(not isinstance(row, dict) or not required <= row.keys()
           or row.get("disposition") not in DISPOSITIONS or "legacy" in row
           for row in result["sources"]):
        raise ValueError("Unsupported filing source format")
    return result


def _load(folder):
    path = folder / RECEIPT
    if path.exists():
        _regular(path)
        return _current_snapshot(json.loads(path.read_text()))
    if (folder / ".filing.json").exists():
        raise ValueError("Unsupported filing receipt format; prepare a current snapshot separately")
    return None


def validate_document(folder, *, verify_sources=False, source_root=None, verify_copies=True):
    """Validate one explicitly selected document; no fetches or OCR are performed."""
    folder = Path(folder).resolve()
    if (folder / ".filing.pending.json").exists():
        raise IntegrityError("Filing publication is pending: " + str(folder))
    state = _load(folder)
    if state is None:
        raise ValueError("No supported filing receipt: " + str(folder))
    readme = folder / "README.md"
    _regular(readme)
    prose = readme.read_text()
    block = _block(prose)
    if state.get("readme_block_sha256") and (block is None or _digest(block) != state["readme_block_sha256"]):
        raise IntegrityError("README source membership differs from the filing receipt")
    targets = _markdown_targets(block if block is not None else prose, folder)
    if block is not None:
        mapping_rows = [line for line in block.splitlines() if re.match(r"^\|\s*\d+\s*\|", line)]
        ordered, _ = _reading_order(state["sources"])
        if len(mapping_rows) != len(ordered):
            raise IntegrityError("README source-map row count differs from receipt membership")
        root_for_rows = _locator(folder, state["source_root"])
        for line, row in zip(mapping_rows, ordered):
            row_targets = _markdown_targets(line, folder)
            expected = [(root_for_rows / row["source"]).resolve(), (folder / row["text"]).resolve()]
            if row.get("copy"):
                expected.append((folder / row["copy"]).resolve())
            if any(path not in row_targets for path in expected):
                raise IntegrityError("README row maps different source/copy/text membership")
    target_positions = []
    sources, exports, copies = set(), set(), set()
    reference_root = _locator(folder, state["source_root"])
    root = Path(source_root).resolve() if source_root else reference_root
    for index, row in enumerate(state["sources"], 1):
        if row["order"] != index or row["source"] in sources:
            raise IntegrityError("Duplicate sources or invalid source order")
        sources.add(row["source"])
        original = _relative(root, row["source"])
        text = _relative(folder, row["text"])
        if text in exports:
            raise IntegrityError("Two sources share one preferred export")
        exports.add(text)
        _verify(text, row["text_sha256"])
        if text.resolve() not in targets:
            raise IntegrityError("README omits preferred-text membership: " + row["source"])
        if _relative(reference_root, row["source"]).resolve() not in targets:
            raise IntegrityError("README omits original membership: " + row["source"])
        if row.get("copy"):
            copy = _relative(folder, row["copy"])
            if copy in copies:
                raise IntegrityError("Two sources share one scan copy")
            copies.add(copy)
            if verify_copies:
                _verify(copy, row["source_sha256"])
            else:
                if copy.is_symlink() or not copy.is_file():
                    raise IntegrityError("Recorded copy is missing or a symlink: " + str(copy))
            if copy.stat().st_size != row["source_bytes"] or copy.stat().st_mtime_ns != row["source_mtime_ns"]:
                raise IntegrityError("Copy size or modification time differs: " + str(copy))
            if copy.resolve() not in targets:
                raise IntegrityError("README omits scan membership: " + row["source"])
            target_positions.append((row["source"], targets.index(copy.resolve())))
            if verify_sources:
                _verify(original, row["source_sha256"])
        elif row["disposition"] != "unavailable":
            raise IntegrityError("Readable source has no scan disposition")
    displayed, _ = _reading_order(state["sources"])
    positions = dict(target_positions)
    expected_positions = [positions[row["source"]] for row in displayed if row.get("copy")]
    if expected_positions != sorted(expected_positions):
        raise IntegrityError("README scan order differs from the receipt")
    combined = _relative(folder, state["combined"]["path"])
    _verify(combined, state["combined"]["sha256"])
    expected = _assembled(folder, state["sources"])
    if combined.read_bytes() != expected:
        raise IntegrityError("Combined text differs from preferred source order")
    return {"document": str(folder), "selected": len(sources), "copies": len(copies),
            "text_exports": len(exports), "unavailable": sum(r["disposition"] == "unavailable" for r in state["sources"]),
            "text_gaps": sum(bool(r.get("gap_reason")) for r in state["sources"]),
            "source_bytes_verified": verify_sources, "copy_bytes_verified": verify_copies, "valid": True}


def fetch_selection(documents=(), *, jobs=(), include_copies=False, include_sources=False, source_root=None):
    """Exact-file intake for an explicitly declared reading/check selection.

Controls are fetched before their referenced text is resolved. Reading never
fetches original or copied images unless the caller expressly requests them.
"""
    documents = list(dict.fromkeys(Path(p).resolve() for p in documents))
    jobs = list(dict.fromkeys(Path(p).resolve() for p in jobs))
    controls, receipts = [], []
    for folder in documents:
        current = folder / RECEIPT
        if not current.exists():
            raise ValueError("No current filing receipt: " + str(folder))
        controls.append(current)
        controls.append(folder / "README.md")
    job_receipts = [p / ".processing.json" if p.is_dir() else p for p in jobs]
    for receipt in job_receipts:
        controls.append(receipt)
        if (receipt.parent / "README.md").exists():
            controls.append(receipt.parent / "README.md")
    controls = list(dict.fromkeys(controls))
    def intake(paths):
        # Bound command-line length and coordination work while retaining exact paths.
        for offset in range(0, len(paths), 100):
            receipt, ok = fetch(paths[offset:offset + 100])
            receipts.append(receipt)
            if not ok:
                raise IntegrityError("Exact-file selection intake failed: " + json.dumps(receipt.get("failures", [])))
    intake(controls)
    content = []
    for folder in documents:
        state = _load(folder)
        content.append(_relative(folder, state["combined"]["path"]))
        actual_source_root = Path(source_root).resolve() if source_root else _locator(folder, state["source_root"])
        for row in state["sources"]:
            content.append(_relative(folder, row["text"]))
            if include_copies and row.get("copy"):
                content.append(_relative(folder, row["copy"]))
            if include_sources and row["disposition"] != "unavailable":
                content.append(_relative(actual_source_root, row["source"]))
    from .scans import receipt_sources
    for receipt in job_receipts:
        state = json.loads(receipt.read_text())
        for key, original, step, relative in receipt_sources(receipt, state, source_root=source_root):
            result = state.get("steps", {}).get(step, {})
            if result.get("status") == "done" and relative in result.get("outputs", {}):
                content.append(_relative(receipt.parent, relative))
            if include_sources:
                content.append(original)
    content = list(dict.fromkeys(p for p in content if p not in controls))
    intake(content)
    return {"control_files": len(controls), "content_files": len(content),
            "includes_copies": include_copies, "includes_originals": include_sources,
            "receipts": receipts}


def preferred_search_paths(documents=(), *, jobs=(), allow_discrepancies=False):
    """Select README and exactly one preferred source export per declared member.

The caller supplies document folders explicitly. On discrepancy default selection
fails; opt-in partial selection returns errors alongside only validated documents.
"""
    files, errors, seen_folders, seen_sources, gaps, job_coverage = [], [], set(), {}, [], []
    for value in documents:
        folder = Path(value).resolve()
        if folder in seen_folders:
            continue
        seen_folders.add(folder)
        try:
            validate_document(folder, verify_copies=False)
            state = _load(folder)
            root = _locator(folder, state["source_root"])
            members = []
            for row in state["sources"]:
                identity = str(_relative(root, row["source"]).resolve())
                if identity in seen_sources:
                    raise IntegrityError("Source has multiple primary filing dispositions: " + identity)
                members.append((identity, row))
            files.append(str(folder / "README.md"))
            for identity, row in members:
                seen_sources[identity] = str(folder)
                files.append(str(folder / row["text"]))
                if row.get("gap_reason") or row["disposition"] == "unavailable":
                    gaps.append({"document": str(folder), "source": identity,
                                 "kind": "unavailable_original" if row["disposition"] == "unavailable" else "recognition_gap",
                                 "reason": row.get("gap_reason") or row.get("reason"), "gap_marker_available": True})
        except (OSError, ValueError, RuntimeError) as exc:
            if not allow_discrepancies:
                raise
            errors.append({"document": str(folder), "error": str(exc)})
    # Explicit extraction jobs keep successful text visible while filing is pending.
    # A filed preferred export takes precedence for the same original path. Two
    # unfiled variants require an explicit choice instead of arbitrary selection.
    from .scans import receipt_sources
    for value in dict.fromkeys(str(Path(p).resolve()) for p in jobs):
        supplied = Path(value)
        receipt = supplied / ".processing.json" if supplied.is_dir() else supplied
        folder = receipt.parent
        try:
            _regular(receipt)
            data = json.loads(receipt.read_text())
            candidates, missing, accepted, superseded = [], [], [], 0
            for key, original, step_key, relative in receipt_sources(receipt, data):
                identity = str(original)
                if identity in seen_sources:
                    if seen_sources[identity].startswith("job:"):
                        raise IntegrityError("Multiple unfiled extraction readings; select one job for " + identity)
                    superseded += 1
                    continue
                step = data.get("steps", {}).get(step_key, {})
                if step.get("status") != "done" or relative not in step.get("outputs", {}):
                    missing.append({"job": str(folder), "source": identity, "kind": "unavailable_text",
                                    "reason": data.get("intake_failures", {}).get(key, {}).get("error") or step.get("error") or "No completed source extract"})
                    continue
                path = _relative(folder, relative)
                _verify(path, step["outputs"][relative])
                candidates.append(str(path))
                accepted.append(identity)
                details = step.get("details", {})
                if details.get("blank_text") or details.get("blank_pdf_pages"):
                    missing.append({"job": str(folder), "source": identity, "kind": "recognition_gap",
                                    "blank_text": bool(details.get("blank_text")),
                                    "blank_pdf_pages": details.get("blank_pdf_pages", [])})
                if key in data.get("intake_failures", {}):
                    missing.append({"job": str(folder), "source": identity, "kind": "current_original_unavailable",
                                    "reason": data["intake_failures"][key].get("error"),
                                    "saved_text_retained": True})
            readme = folder / "README.md"
            if readme.exists():
                _regular(readme)
                readme.read_text()  # A listed but unreadable note is not search coverage.
                files.append(str(readme))
            files.extend(candidates)
            for identity in accepted:
                seen_sources[identity] = "job:" + str(folder)
            gaps.extend(missing)
            job_coverage.append({"job": str(folder), "selected": len(data["specification"]["sources"]),
                                 "per_source_text": len(candidates), "preferred_filed_sources": superseded,
                                 "gaps": len(missing), "reading_role": "Retained baseline extraction; no filing or visual review implied"})
        except (OSError, ValueError, RuntimeError) as exc:
            if not allow_discrepancies:
                raise
            errors.append({"job": str(folder), "error": str(exc)})
    return {"files": list(dict.fromkeys(files)), "errors": errors, "gaps": gaps,
            "jobs": job_coverage, "complete": not errors and not gaps,
            "scope": "Only explicit document folders and extraction jobs; source text once per original. "
                     "Filed preferred text takes precedence over an explicitly selected job's baseline text. "
                     "Copy metadata and preferred text checked; original/image bytes are not rehashed for reading."}


def _assembled(folder, rows, staged=None):
    ordered, order_note = _reading_order(rows)
    parts = ["Best-effort discovery text. Assembly uses " + order_note + ". Gaps remain visible.\n"
             f"Selected sources: {len(rows)}; unavailable originals: "
             f"{sum(r['disposition'] == 'unavailable' for r in rows)}; explicit text gaps: "
             f"{sum(bool(r.get('gap_reason')) for r in rows)}.\n\n"]
    for row in ordered:
        path = (staged or {}).get(row["text"], folder / row["text"])
        parts.append(path.read_text().rstrip() + "\n\n")
    return "".join(parts).encode()


def _snapshot(folder, generation, relative, source):
    target = _relative(folder, str(Path(".filing-history") / generation / relative))
    if target.exists():
        if checksum(target) != checksum(source):
            raise IntegrityError("A previous filing snapshot changed")
    else:
        atomic_copy(target, source)


def _publish_file(target, staged, previous_hash, mtime_ns=None):
    """Exact prepared bytes; callers retain the pending manifest until committed."""
    expected = checksum(staged)
    if target.exists():
        _regular(target)
        actual = checksum(target)
        if actual == expected:
            if mtime_ns is not None and target.stat().st_mtime_ns != mtime_ns:
                raise IntegrityError("Published scan modification time changed")
            return
        if previous_hash is None or actual != previous_hash:
            raise IntegrityError("Refusing to replace changed/untracked output: " + str(target))
        atomic(target, staged.read_bytes())
    else:
        if previous_hash is not None:
            raise IntegrityError("A previously recorded output is missing: " + str(target))
        # copy2 into a same-directory temporary file before exclusive publication
        # preserves timestamps even when interrupted immediately after publication.
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
        os.close(fd)
        try:
            shutil.copy2(staged, temp)
            with open(temp, "rb") as stream:
                os.fsync(stream.fileno())
            os.link(temp, target)
        finally:
            os.unlink(temp)
    _verify(target, expected)
    if mtime_ns is not None and target.stat().st_mtime_ns != mtime_ns:
        raise IntegrityError("Published scan modification time differs")


class _Publication:
    """Shared staged-byte preparation for filing and bounded reconciliation."""

    def __init__(self, folder):
        self.folder = folder
        self.stage = Path(tempfile.mkdtemp(prefix=".filing-stage-", dir=folder))
        self.outputs, self.staged, self.input_checks = [], {}, []

    def prepare(self, target, content=None, original=None, mtime=None, previous=None):
        _relative(self.folder, target)
        if target in self.staged:
            raise IntegrityError("Repeated prepared publication target: " + target)
        path = self.stage / f"output-{len(self.outputs):04d}"
        if original is not None:
            shutil.copy2(original, path)
        else:
            atomic(path, content, replace=False)
        if mtime is not None and path.stat().st_mtime_ns != mtime:
            raise IntegrityError("Prepared copy modification time differs")
        digest = checksum(path)
        self.outputs.append({"target": target, "staged": path.name, "sha256": digest,
                             "previous_sha256": previous, "mtime_ns": mtime})
        self.staged[target] = path
        return digest

    def checkpoint(self, state, old, recipe_identity, *, receipt_pin_target=None, **extra):
        folder = self.folder
        combined_hash = self.prepare(COMBINED, content=_assembled(folder, state["sources"], self.staged),
                                     previous=old["combined"]["sha256"] if old else None)
        state.update(combined={"path": COMBINED, "sha256": combined_hash}, assembly_version=1,
                     updated_at=now(), last_recipe_sha256=recipe_identity)
        prose = (folder / "README.md").read_text() if (folder / "README.md").exists() else ""
        prior_block = _block(prose)
        block = _map(folder, state)
        state["readme_block_sha256"] = _digest(block)
        if receipt_pin_target is not None:
            self.prepare(receipt_pin_target, content=json_bytes({"recipe_sha256": recipe_identity,
                         "receipt_sha256": _digest(json_bytes(state))}))
        pending = {"schema": 1, "recipe_identity": recipe_identity, "generation": self.stage.name[1:],
            "stage": self.stage.name, "outputs": self.outputs, "input_checks": self.input_checks,
            "receipt": state, "readme_block": block,
            "previous_readme_block_sha256": (old.get("readme_block_sha256") if old and old.get("readme_block_sha256")
                                             else _digest(prior_block) if prior_block else None),
            "previous_receipt_sha256": checksum(folder / RECEIPT) if (folder / RECEIPT).exists() else None,
            "readme_scaffold": f"# {folder.name}\n\nSupplied filing description. Record title/date evidence and review questions here.\n\n"
                               f"[Read discovery text](<OCR%20discovery%20text.txt>). Original wording remains unverified.\n"}
        pending.update(extra)
        atomic(folder / ".filing.pending.json", json_bytes(pending), replace=False)
        return pending


def _check_retained(folder, records):
    for item in records:
        path = _relative(folder, item["path"])
        _verify(path, item["sha256"])
        if item.get("mtime_ns") is not None and path.stat().st_mtime_ns != item["mtime_ns"]:
            raise IntegrityError("Retained copy modification time changed: " + str(path))


def _resume(folder, pending, *, fetch_inputs=False):
    if type(pending.get("schema")) is not int or pending["schema"] != 1:
        raise ValueError("Unsupported filing checkpoint format")
    _current_snapshot(pending["receipt"])
    if _block(pending["readme_block"]) != pending["readme_block"]:
        raise IntegrityError("Filing checkpoint lacks its current managed source map")
    if (not isinstance(pending.get("generation"), str)
            or Path(pending["generation"]).name != pending["generation"]
            or not pending["generation"].startswith("filing-stage-")):
        raise IntegrityError("Invalid filing generation")
    stage = _relative(folder, pending["stage"])
    if stage.parent != folder or stage.name != "." + pending["generation"]:
        raise IntegrityError("Invalid filing stage")
    receipt_target = _relative(folder, RECEIPT)
    if fetch_inputs:
        paths = [_locator(folder, item["path"]) for item in pending.get("input_checks", [])]
        paths += [_relative(stage, item["staged"]) for item in pending["outputs"]]
        paths += [_relative(folder, item["path"]) for item in pending.get("retained_checks", [])]
        paths += [_relative(folder, item["target"]) for item in pending["outputs"]
                  if _relative(folder, item["target"]).exists()]
        paths += [path for path in (receipt_target, folder / "README.md", folder / ".filing.pending.json")
                  if path.exists()]
        paths = list(dict.fromkeys(paths))
        for offset in range(0, len(paths), 100):
            receipt, ok = fetch(paths[offset:offset + 100])
            saved = _relative(folder, str(Path(".filing-history") / pending["generation"] /
                              ("resume-intake-" + _digest(json_bytes(receipt))[:16] + ".json")))
            if not saved.exists():
                atomic(saved, json_bytes(receipt), replace=False)
            if not ok:
                raise IntegrityError("Exact-file intake failed while resuming filing")
    if receipt_target.exists():
        _regular(receipt_target)
    # Refuse stale receipt state before publishing any prepared output, including
    # when resuming after only part of the output set was replaced.
    new_receipt_hash = _digest(json_bytes(pending["receipt"]))
    if receipt_target.exists():
        if checksum(receipt_target) not in {new_receipt_hash, pending.get("previous_receipt_sha256")}:
            raise IntegrityError("Filing receipt changed during publication")
    elif pending.get("previous_receipt_sha256") is not None:
        raise IntegrityError("A previously recorded filing receipt is missing")
    checks = pending.get("input_checks", [])
    for item in checks:
        _verify(_locator(folder, item["path"]), item["sha256"])
    readme = folder / "README.md"
    if readme.exists() or readme.is_symlink():
        _regular(readme)
    prose = readme.read_text() if readme.exists() else pending["readme_scaffold"]
    _merge_readme(prose, pending["readme_block"], pending["previous_readme_block_sha256"])
    stage = _relative(folder, pending["stage"])
    if stage.parent != folder or not stage.name.startswith(".filing-stage-"):
        raise IntegrityError("Invalid filing stage")
    expected_names = {record["staged"] for record in pending["outputs"]}
    if not stage.is_dir() or {p.name for p in stage.iterdir()} != expected_names:
        raise IntegrityError("Prepared filing files are missing or untracked")
    # Check the complete target set first: a conflict in a later output must not
    # cause an earlier output to be replaced as a side effect of inspection.
    _check_retained(folder, pending.get("retained_checks", []))
    for output in pending["outputs"]:
        prepared = _relative(stage, output["staged"])
        _verify(prepared, output["sha256"])
        target = _relative(folder, output["target"])
        if target.exists() or target.is_symlink():
            _regular(target)
            actual = checksum(target)
            if actual not in {output["sha256"], output.get("previous_sha256")}:
                raise IntegrityError("Changed output blocks filing resume: " + str(target))
            if output.get("mtime_ns") is not None and target.stat().st_mtime_ns != output["mtime_ns"]:
                raise IntegrityError("Published scan modification time changed")
        elif output.get("previous_sha256") is not None:
            raise IntegrityError("A previously recorded output is missing: " + str(target))
    for output in pending["outputs"]:
        prepared = _relative(stage, output["staged"])
        target = _relative(folder, output["target"])
        if target.exists() and checksum(target) != output["sha256"]:
            if checksum(target) != output.get("previous_sha256"):
                raise IntegrityError("Changed output blocks filing resume: " + str(target))
            _snapshot(folder, pending["generation"], output["target"], target)
        _publish_file(target, prepared, output.get("previous_sha256"), output.get("mtime_ns"))
    for item in checks:
        _verify(_locator(folder, item["path"]), item["sha256"])
    _check_retained(folder, pending.get("retained_checks", []))
    readme = folder / "README.md"
    if readme.exists() or readme.is_symlink():
        _regular(readme)
    prose = readme.read_text() if readme.exists() else pending["readme_scaffold"]
    merged = _merge_readme(prose, pending["readme_block"], pending["previous_readme_block_sha256"])
    if readme.exists() and merged != prose:
        # Human additions made after an interruption survive. The first snapshot is
        # retained, and a later human revision gets its own content-hash filename.
        history = "README-" + _digest(prose)[:16] + ".md"
        _snapshot(folder, pending["generation"], history, readme)
    if not readme.exists() or merged != prose:
        atomic(readme, merged)
    receipt = _relative(folder, RECEIPT)
    if receipt.exists():
        _regular(receipt)
    if receipt.exists() and checksum(receipt) != _digest(json_bytes(pending["receipt"])):
        if checksum(receipt) != pending.get("previous_receipt_sha256"):
            raise IntegrityError("Filing receipt changed during publication")
        _snapshot(folder, pending["generation"], RECEIPT, receipt)
    atomic(receipt, json_bytes(pending["receipt"]))
    (folder / ".filing.pending.json").unlink()
    result = validate_document(folder)
    # Remove only this successfully committed transaction's verified prepared files.
    # Interrupted/untracked stages and historical replaced outputs remain intact.
    for output in pending["outputs"]:
        prepared = _relative(stage, output["staged"])
        _verify(prepared, output["sha256"])
        prepared.unlink()
    stage.rmdir()
    return result


def _text_export(folder, source_root, row, preferred, complementary):
    lines = [f"Original: {row['source']}", f"Original root relative to this folder: {_rel(source_root, folder)}",
             f"Disposition: {row['disposition']}", "Machine discovery text; wording unverified.", ""]
    if row.get("reason"):
        lines += ["Original availability: " + row["reason"], ""]
    if row.get("gap_reason"):
        lines += ["[TEXT GAP: " + row["gap_reason"] + "]", ""]
    if preferred:
        lines += [f"Preferred extraction: {preferred['path']}", f"Extraction SHA-256: {preferred['sha256']}", "",
                  _locator(folder, preferred["path"]).read_text()]
    else:
        lines += ["[NO PREFERRED EXTRACTED TEXT: " + row["gap_reason"] + "]"]
    for item in complementary:
        lines += ["", f"=== Complementary extraction: {item['path']} ===",
                  "Supplementary reading; not independent evidence.", _locator(folder, item["path"]).read_text()]
    return "\n".join(lines).rstrip() + "\n"


def _fetch_filing_state(folder, recipe_identity, *, state=None):
    """Fetch only exact controls or outputs of one explicitly affected document."""
    _relative(folder, ".filing-history")
    if state is None:
        paths = [_relative(folder, name) for name in (RECEIPT, ".filing.pending.json", "README.md")
                 if (folder / name).exists() or (folder / name).is_symlink()]
        label = "controls"
    else:
        paths = [_relative(folder, state["combined"]["path"])]
        for row in state["sources"]:
            paths.append(_relative(folder, row["text"]))
            if row.get("copy"):
                paths.append(_relative(folder, row["copy"]))
        label = "outputs"
    paths = list(dict.fromkeys(paths))
    for offset in range(0, len(paths), 100):
        receipt, ok = fetch(paths[offset:offset + 100])
        content = json_bytes(receipt)
        target = _relative(folder, str(Path(".filing-history") / recipe_identity[:16] /
                           ("existing-" + label + "-intake-" + _digest(content)[:16] + ".json")))
        if target.exists():
            _verify(target, _digest(content))
        else:
            atomic(target, content, replace=False)
        if not ok:
            raise IntegrityError("Exact-file existing filing " + label + " intake failed")


def file_assignments(recipe_path, destination, *, source_root=None, text_root=None, fetch_inputs=False):
    """Apply explicit version-1 assignments. Returns coverage for this recipe only.

Recipe roots resolve relative to the recipe; runtime overrides allow relocation.
Each assignment requires source, document, description, disposition and source_sha256
(except unavailable). preferred_text/complementary_text contain path and sha256.
Missing text requires gap_reason; unavailable requires reason. No guessing occurs.
"""
    recipe_path, destination = Path(recipe_path).resolve(), Path(destination).absolute()
    if destination.is_symlink():
        raise IntegrityError("Destination cannot be a symlink")
    destination = destination.resolve()
    recipe_intake = None
    if fetch_inputs:
        recipe_intake, ok = fetch([recipe_path])
        if not ok:
            raise IntegrityError("Exact-file assignment recipe intake failed: " + json.dumps(recipe_intake.get("failures", [])))
    _regular(recipe_path)
    recipe = json.loads(recipe_path.read_text())
    if recipe.get("schema") != 1 or not isinstance(recipe.get("assignments"), list) or not recipe["assignments"]:
        raise ValueError("A schema-1 recipe needs nonempty explicit assignments")
    source_root = Path(source_root).resolve() if source_root else (recipe_path.parent / recipe.get("source_root", ".")).resolve()
    text_root = Path(text_root).resolve() if text_root else (recipe_path.parent / recipe.get("text_root", ".")).resolve()
    seen, groups = set(), {}
    for assignment in recipe["assignments"]:
        if not isinstance(assignment, dict) or not {"source", "document", "description", "disposition"} <= assignment.keys():
            raise ValueError("Each assignment needs source, document, description and disposition")
        if assignment["disposition"] != "unavailable" and not re.fullmatch(r"[0-9a-f]{64}", assignment.get("source_sha256", "")):
            raise ValueError("Readable original requires its exact source_sha256")
        choices = ([assignment["preferred_text"]] if assignment.get("preferred_text") else []) + assignment.get("complementary_text", [])
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get("path"), str) or not re.fullmatch(r"[0-9a-f]{64}", choice.get("sha256", "")):
                raise ValueError("Each chosen text requires path and exact sha256")
        for field in ("capture_order", "reading_order"):
            if field in assignment and (not isinstance(assignment[field], int) or isinstance(assignment[field], bool) or assignment[field] < 1):
                raise ValueError(field + " must be a positive explicit integer position")
        if assignment.get("reading_order") and not str(assignment.get("reading_order_evidence", "")).strip():
            raise ValueError("Asserted reading order requires reading_order_evidence")
        source = _relative(source_root, assignment["source"])
        folder = _relative(destination, assignment["document"])
        if folder.resolve() == destination.resolve():
            raise ValueError("Document folder must be below the organized destination")
        if source.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Filing destination must not contain source originals")
        identity = str(source.resolve())
        if identity in seen:
            raise ValueError("Each selected source needs exactly one primary disposition")
        seen.add(identity)
        if assignment.get("disposition") not in DISPOSITIONS:
            raise ValueError("Unknown source disposition")
        if assignment["disposition"] == "unavailable" and not assignment.get("reason"):
            raise ValueError("Unavailable source requires an explicit reason")
        if not assignment.get("preferred_text") and not assignment.get("gap_reason"):
            raise ValueError("Missing preferred text requires an explicit gap_reason")
        description = assignment.get("description", "")
        if not description or any(c in description for c in "/\\\n\r") or description in {".", ".."}:
            raise ValueError("Provide a safe descriptive filename prefix")
        groups.setdefault(folder, []).append(assignment)
    recipe_identity = _digest(json_bytes(recipe))
    results = []
    with _lock(destination):
        if recipe_intake is not None:
            intake_record = {"recipe": _rel(recipe_path, destination), "recipe_sha256": checksum(recipe_path),
                             "intake": recipe_intake}
            content = json_bytes(intake_record)
            saved = _relative(destination, ".filing-history/recipe-inputs/" + _digest(content) + ".json")
            if saved.exists():
                _verify(saved, _digest(content))
            else:
                atomic(saved, content, replace=False)
        for folder, assignments in groups.items():
            with _lock(folder):
                if fetch_inputs:
                    _fetch_filing_state(folder, recipe_identity)
                pending_path = folder / ".filing.pending.json"
                if pending_path.exists() or pending_path.is_symlink():
                    _regular(pending_path)
                    pending = json.loads(pending_path.read_text())
                    if pending["recipe_identity"] != recipe_identity:
                        raise IntegrityError("Resume the pending filing recipe before another recipe")
                    _resume(folder, pending, fetch_inputs=fetch_inputs)
                for special in (".filing-history", "README.md"):
                    _relative(folder, special)
                old = _load(folder)
                if old:
                    if fetch_inputs:
                        _fetch_filing_state(folder, recipe_identity, state=old)
                    validate_document(folder)
                state = json.loads(json.dumps(old)) if old else {"schema": 1, "kind": "scan-filing", "sources": []}
                if old:
                    old_root = _locator(folder, old["source_root"])
                    for row in state["sources"]:
                        try:
                            row["source"] = str((old_root / row["source"]).resolve().relative_to(source_root))
                        except ValueError as exc:
                            raise ValueError("source_root must encompass existing folder originals") from exc
                state["source_root"] = _rel(source_root, folder)
                existing = {r["source"]: r for r in state["sources"]}
                todo = []
                for assignment in assignments:
                    if assignment["source"] in existing:
                        row = existing[assignment["source"]]
                        if row.get("assignment_sha256") != _digest(json_bytes(assignment)):
                            raise IntegrityError("Source already filed with another assignment; reconcile explicitly")
                        # Repeating a recipe is a new verification of its explicitly
                        # selected inputs, not permission to trust stale input hashes.
                        rechecks = []
                        if assignment["disposition"] != "unavailable":
                            rechecks.append((_relative(source_root, assignment["source"]), assignment["source_sha256"]))
                        choices = ([assignment["preferred_text"]] if assignment.get("preferred_text") else []) + assignment.get("complementary_text", [])
                        rechecks += [(_relative(text_root, choice["path"]), choice["sha256"]) for choice in choices]
                        if not fetch_inputs and any("com~apple~CloudDocs" in p.parts for p, _ in rechecks):
                            raise ValueError("Use fetch_inputs for exact iCloud source/text intake")
                        if fetch_inputs and rechecks:
                            intake, ok = fetch([p for p, _ in rechecks])
                            saved = _relative(folder, str(Path(".filing-history") / recipe_identity[:16] / ("recheck-" + _digest(json_bytes(intake))[:16] + ".json")))
                            if not saved.exists():
                                atomic(saved, json_bytes(intake), replace=False)
                            if not ok:
                                raise IntegrityError("Exact-file intake failed on repeated filing assignment")
                        for path, digest in rechecks:
                            _verify(path, digest)
                    else:
                        todo.append(assignment)
                if not todo:
                    results.append(validate_document(folder))
                    continue
                publication = _Publication(folder)
                prepare, input_checks = publication.prepare, publication.input_checks
                for assignment in todo:
                    source = _relative(source_root, assignment["source"])
                    order = len(state["sources"]) + 1
                    name = f"{assignment['description']} - scan {order:04d} - {source.name}"
                    if len((name + ".txt").encode()) > 240:
                        raise ValueError("Description plus full original filename is too long; shorten the description")
                    row = {"source": assignment["source"], "order": order,
                           "disposition": assignment["disposition"], "gap_reason": assignment.get("gap_reason"),
                           "assignment_sha256": _digest(json_bytes(assignment))}
                    for field in ("capture_order", "reading_order", "reading_order_evidence"):
                        if field in assignment:
                            row[field] = assignment[field]
                    preferred, complementary = None, []
                    choices = ([assignment["preferred_text"]] if assignment.get("preferred_text") else []) + assignment.get("complementary_text", [])
                    paths = [source] if assignment["disposition"] != "unavailable" else []
                    paths += [_relative(text_root, choice["path"]) for choice in choices]
                    if not fetch_inputs and any("com~apple~CloudDocs" in path.parts for path in paths):
                        raise ValueError("Use fetch_inputs for exact iCloud source/text intake")
                    if fetch_inputs and paths:
                        receipt, ok = fetch(paths)
                        receipt_path = _relative(folder, str(Path(".filing-history") / recipe_identity[:16] / f"intake-{order:04d}.json"))
                        receipt_bytes = json_bytes(receipt)
                        if receipt_path.exists() and checksum(receipt_path) != _digest(receipt_bytes):
                            receipt_path = receipt_path.with_name(receipt_path.stem + "-" + _digest(receipt_bytes)[:16] + ".json")
                        if receipt_path.exists():
                            _verify(receipt_path, _digest(receipt_bytes))
                        else:
                            atomic(receipt_path, receipt_bytes, replace=False)
                        if not ok:
                            raise IntegrityError("Exact-file intake failed; no filing publication started")
                    for index, choice in enumerate(choices):
                        path = _relative(text_root, choice["path"])
                        _verify(path, choice["sha256"])
                        item = {"path": _rel(path, folder), "sha256": choice["sha256"]}
                        input_checks.append(item)
                        if index == 0 and assignment.get("preferred_text"):
                            preferred = item
                            if not path.read_text().strip():
                                row["gap_reason"] = row["gap_reason"] or "Preferred extraction is blank"
                        else:
                            complementary.append(item)
                    row.update(preferred_text=preferred, complementary_text=complementary)
                    if assignment["disposition"] != "unavailable":
                        _verify(source, assignment["source_sha256"])
                        stat = source.stat()
                        input_checks.append({"path": _rel(source, folder), "sha256": assignment["source_sha256"]})
                        row.update(source_sha256=assignment["source_sha256"], source_bytes=stat.st_size,
                                   source_mtime_ns=stat.st_mtime_ns, copy="Scans/" + name)
                        copied_hash = prepare(row["copy"], original=source, mtime=stat.st_mtime_ns)
                        if copied_hash != assignment["source_sha256"] or checksum(source) != copied_hash:
                            raise IntegrityError("Original changed while preparing its copy")
                    else:
                        row.update(copy=None, source_sha256=assignment.get("source_sha256"), reason=assignment["reason"])
                    row["text"] = "Source text/" + name + ".txt"
                    row["text_sha256"] = prepare(row["text"], content=_text_export(folder, source_root, row, preferred, complementary))
                    state["sources"].append(row)
                pending = publication.checkpoint(state, old, recipe_identity)
                results.append(_resume(folder, pending))
    return {"selected": len(recipe["assignments"]), "documents": results,
            "unavailable": sum(a["disposition"] == "unavailable" for a in recipe["assignments"]),
            "filing_complete": True, "recipe_sha256": recipe_identity}


def _intake_reconciliation(paths, fetch_inputs):
    """Intake only exact paths already selected by a recipe or accepted receipt."""
    paths = list(dict.fromkeys(paths))
    if not fetch_inputs:
        if any("com~apple~CloudDocs" in path.parts for path in paths):
            raise ValueError("Use fetch_inputs for exact iCloud reconciliation intake")
        return []
    receipts = []
    for offset in range(0, len(paths), 100):
        receipt, ok = fetch(paths[offset:offset + 100])
        receipts.append(receipt)
        if not ok:
            raise IntegrityError("Exact-file reconciliation intake failed: " + json.dumps(receipt.get("failures", [])))
    return receipts


def _reconciliation_recipe(recipe):
    allowed = {"schema", "expected_receipt_sha256", "reason", "evidence", "text_root", "changes"}
    if not isinstance(recipe, dict) or set(recipe) - allowed or type(recipe.get("schema")) is not int or recipe["schema"] != 1:
        raise ValueError("Expected a schema-1 reconciliation recipe with supported fields only")
    if not isinstance(recipe.get("expected_receipt_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", recipe["expected_receipt_sha256"]):
        raise ValueError("Reconciliation requires exact expected_receipt_sha256")
    for field in ("reason", "evidence"):
        if not isinstance(recipe.get(field), str) or not recipe[field].strip():
            raise ValueError("Reconciliation requires nonempty " + field)
    if not isinstance(recipe.get("text_root", "."), str) or not recipe.get("text_root", ".").strip() or Path(recipe.get("text_root", ".")).is_absolute():
        raise ValueError("text_root must be relative to the reconciliation recipe")
    if not isinstance(recipe.get("changes"), list) or not recipe["changes"]:
        raise ValueError("Reconciliation requires nonempty explicit changes")
    seen = set()
    editable = {"preferred_text", "complementary_text", "gap_reason"}
    for change in recipe["changes"]:
        if (not isinstance(change, dict) or set(change) - (editable | {"source", "gap_clear_reason"})
                or not editable.intersection(change)):
            raise ValueError("Each change may edit only preferred_text, complementary_text and gap_reason")
        source = change.get("source")
        if not isinstance(source, str) or not source or source in seen:
            raise ValueError("Each changed source must occur exactly once")
        seen.add(source)
        if "preferred_text" in change and change["preferred_text"] is not None and not isinstance(change["preferred_text"], dict):
            raise ValueError("preferred_text must be a reading object or null")
        if "complementary_text" in change and not isinstance(change["complementary_text"], list):
            raise ValueError("complementary_text must be an explicit list")
        choices = ([change["preferred_text"]] if change.get("preferred_text") is not None else []) + change.get("complementary_text", [])
        for choice in choices:
            if (not isinstance(choice, dict) or set(choice) != {"path", "sha256"}
                    or not isinstance(choice["path"], str) or not isinstance(choice["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", choice["sha256"])):
                raise ValueError("Each new reading requires only path and exact sha256")
        if "gap_reason" in change:
            gap = change["gap_reason"]
            if gap is not None and (not isinstance(gap, str) or not gap.strip()):
                raise ValueError("gap_reason must be nonempty text or explicitly justified null")
            if gap is None and (not isinstance(change.get("gap_clear_reason"), str) or not change["gap_clear_reason"].strip()):
                raise ValueError("Clearing a gap requires explicit gap_clear_reason")
        if "gap_clear_reason" in change and ("gap_reason" not in change or change["gap_reason"] is not None):
            raise ValueError("gap_clear_reason applies only to explicit null gap_reason")


def reconcile_document(document, changes_path, *, fetch_inputs=False):
    """Apply an immutable, receipt-pinned recipe to existing source readings only.

    Recipe schema 1 requires expected_receipt_sha256, reason, evidence and a
    nonempty changes list. Each change names an existing source and may supply
    preferred_text (object or null), complementary_text (list), and/or gap_reason
    (text or null). Absent fields are unchanged. Explicit null gap_reason requires
    gap_clear_reason. Reading objects contain root-relative path and exact sha256;
    optional text_root resolves relative to the recipe (default: its directory).
    Membership, copies, dispositions and all orders remain accepted as filed.
    """
    folder, recipe_path = Path(document).absolute(), Path(changes_path).absolute()
    if folder.is_symlink() or recipe_path.is_symlink():
        raise IntegrityError("Reconciliation document and recipe cannot be symlinks")
    folder, recipe_path = folder.resolve(), recipe_path.resolve()
    if not folder.is_dir():
        raise ValueError("Reconciliation requires an existing document folder")
    intakes = _intake_reconciliation([recipe_path], fetch_inputs)
    _regular(recipe_path)
    recipe_bytes = recipe_path.read_bytes()
    recipe_identity = _digest(recipe_bytes)
    recipe = json.loads(recipe_bytes)
    _reconciliation_recipe(recipe)
    text_root = _locator(recipe_path.parent, recipe.get("text_root", "."))
    for special in (".filing-history", "README.md", RECEIPT, ".filing.pending.json"):
        _relative(folder, special)
    with _lock(folder):
        pending_path = folder / ".filing.pending.json"
        controls = [folder / "README.md"]
        accepted_path = folder / RECEIPT
        if not accepted_path.exists():
            raise ValueError("Reconciliation requires a current filing receipt")
        controls.append(accepted_path)
        if pending_path.exists():
            controls.append(pending_path)
        intakes += _intake_reconciliation(controls, fetch_inputs)
        _verify(recipe_path, recipe_identity)
        if pending_path.exists():
            _regular(pending_path)
            pending = json.loads(pending_path.read_text())
            if (pending.get("operation") != "reconcile" or pending.get("recipe_identity") != recipe_identity
                    or pending.get("expected_receipt_sha256") != recipe["expected_receipt_sha256"]
                    or pending.get("previous_receipt_sha256") != recipe["expected_receipt_sha256"]
                    or {"path": _rel(recipe_path, folder), "sha256": recipe_identity} not in pending.get("input_checks", [])):
                raise IntegrityError("Resume the same identified pending recipe before reconciliation")
            result = _resume(folder, pending, fetch_inputs=fetch_inputs)
            return dict(result, reconciliation_complete=True, reconciled_sources=len(recipe["changes"]),
                        recipe_sha256=recipe_identity, resumed=True, repeated=False)
        _regular(accepted_path)
        old = _load(folder)
        if old is None:
            raise ValueError("No supported filing receipt to reconcile")
        accepted_hash = checksum(accepted_path)
        repeated = bool(old.get("reconciliations") and old["reconciliations"][-1].get("recipe_sha256") == recipe_identity
                        and old["reconciliations"][-1].get("expected_receipt_sha256") == recipe["expected_receipt_sha256"])
        result_pin_path = _relative(folder, ".filing-history/reconciliations/" + recipe_identity + "-result.json")
        if repeated:
            intakes += _intake_reconciliation([result_pin_path], fetch_inputs)
            _regular(result_pin_path)
            pin = json.loads(result_pin_path.read_text())
            if pin.get("recipe_sha256") != recipe_identity or pin.get("receipt_sha256") != accepted_hash:
                raise IntegrityError("Committed reconciliation receipt changed; review a fresh accepted snapshot")
        elif accepted_hash != recipe["expected_receipt_sha256"]:
            raise IntegrityError("Stale expected filing receipt; review a fresh accepted snapshot")
        state = json.loads(json.dumps(old))
        source_root = _locator(folder, state["source_root"])
        existing = {row["source"]: row for row in state["sources"]}
        outputs = [folder / "README.md", folder / RECEIPT,
                   _relative(folder, old["combined"]["path"])]
        retained = []
        for row in old["sources"]:
            outputs.append(_relative(folder, row["text"]))
            retained.append({"path": row["text"], "sha256": row["text_sha256"]})
            if row.get("copy"):
                outputs.append(_relative(folder, row["copy"]))
                retained.append({"path": row["copy"], "sha256": row["source_sha256"],
                                 "mtime_ns": row["source_mtime_ns"]})
        outputs_set = {path.resolve() for path in outputs}
        if recipe_path in outputs_set:
            raise ValueError("Reconciliation recipe must be separate from managed document outputs")
        input_checks = [{"path": _rel(recipe_path, folder), "sha256": recipe_identity}]
        changing_outputs = {folder / "README.md", folder / RECEIPT,
                            _relative(folder, old["combined"]["path"])}
        changing_outputs.update(_relative(folder, existing[change["source"]]["text"])
                                for change in recipe["changes"] if change["source"] in existing)
        changed_rows = []
        for change in recipe["changes"]:
            _relative(source_root, change["source"])
            if change["source"] not in existing:
                raise ValueError("Reconciliation source is outside accepted document membership: " + change["source"])
            row = existing[change["source"]]
            if row["disposition"] != "unavailable":
                original = _relative(source_root, row["source"])
                input_checks.append({"path": _rel(original, folder), "sha256": row["source_sha256"]})
            if not repeated:
                def reading(choice):
                    if choice is None:
                        return None
                    path = _relative(text_root, choice["path"])
                    if path.resolve() in outputs_set:
                        raise ValueError("A raw reading cannot be a managed document output")
                    return {"path": _rel(path, folder), "sha256": choice["sha256"]}
                if "preferred_text" in change:
                    row["preferred_text"] = reading(change["preferred_text"])
                if "complementary_text" in change:
                    row["complementary_text"] = [reading(choice) for choice in change["complementary_text"]]
                if "gap_reason" in change:
                    row["gap_reason"] = change["gap_reason"]
            choices = ([row["preferred_text"]] if row.get("preferred_text") else []) + row.get("complementary_text", [])
            identities = [_locator(folder, item["path"]) for item in choices]
            if any(path in changing_outputs for path in identities):
                raise ValueError("A retained raw reading aliases an output this reconciliation would replace")
            if len(identities) != len(set(identities)):
                raise ValueError("Repeated preferred/complementary reading for " + row["source"])
            if not row.get("preferred_text") and not row.get("gap_reason"):
                raise ValueError("Missing preferred text requires an explicit gap_reason")
            input_checks.extend(choices)
            changed_rows.append(row)
        intakes += _intake_reconciliation([path for path in outputs if path.exists()] +
                                         [_locator(folder, item["path"]) for item in input_checks], fetch_inputs)
        for item in input_checks:
            _verify(_locator(folder, item["path"]), item["sha256"])
        # This validates every existing export, combined reading, managed map and
        # scan copy, while original-byte intake remains limited to changed sources.
        result = validate_document(folder)
        for row in changed_rows:
            preferred = row.get("preferred_text")
            if preferred and not _locator(folder, preferred["path"]).read_text().strip() and not row.get("gap_reason"):
                raise ValueError("Blank preferred extraction requires an explicit gap_reason")
        if repeated:
            return dict(result, reconciliation_complete=True, reconciled_sources=len(recipe["changes"]),
                        recipe_sha256=recipe_identity, resumed=False, repeated=True)
        _verify(accepted_path, accepted_hash)
        publication = _Publication(folder)
        publication.input_checks = input_checks
        history_path = ".filing-history/reconciliations/" + recipe_identity + ".json"
        publication.prepare(history_path, content=recipe_bytes)
        if intakes:
            publication.prepare(".filing-history/reconciliations/" + recipe_identity + "-intake.json",
                                content=json_bytes(intakes))
        for row in changed_rows:
            row["text_sha256"] = publication.prepare(row["text"],
                content=_text_export(folder, source_root, row, row.get("preferred_text"), row.get("complementary_text", [])),
                previous=next(item["text_sha256"] for item in old["sources"] if item["source"] == row["source"]))
        state.setdefault("reconciliations", []).append({"recipe_sha256": recipe_identity, "recipe": history_path,
            "expected_receipt_sha256": recipe["expected_receipt_sha256"], "reason": recipe["reason"],
            "evidence": recipe["evidence"], "applied_at": now(),
            "changes": [{"source": change["source"], "fields": sorted(set(change) - {"source", "gap_clear_reason"}),
                         **({"gap_clear_reason": change["gap_clear_reason"]} if "gap_clear_reason" in change else {})}
                        for change in recipe["changes"]]})
        retained = [item for item in retained if item["path"] not in publication.staged]
        _verify(accepted_path, accepted_hash)
        pending = publication.checkpoint(state, old, recipe_identity, operation="reconcile",
            receipt_pin_target=_rel(result_pin_path, folder),
            previous_receipt_sha256=accepted_hash,
            expected_receipt_sha256=recipe["expected_receipt_sha256"], retained_checks=retained)
        result = _resume(folder, pending, fetch_inputs=fetch_inputs)
        return dict(result, reconciliation_complete=True, reconciled_sources=len(recipe["changes"]),
                    recipe_sha256=recipe_identity, resumed=False, repeated=False)
