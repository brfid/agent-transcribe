"""Finite, scratch-only recovery. The runner alone publishes accepted outputs.

Every stage consumes its budget *before* execution. A crash therefore cannot
silently create an unlimited retry loop. Native candidates remain separate;
combined preferred text never masquerades as a merged Docling document.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
import codecs
import errno
import hashlib
import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import unicodedata
import zipfile
import xml.etree.ElementTree as ET

from .common import IntegrityError, checksum, file_record
from .pinned_availability import verify_pinned
from .docling_backend import inspect_source, render_units

ATTEMPT_CAPS = {"singleton": 1, "pdfium": 1, "qpdf": 1, "qpdf_convert": 1,
                "poppler": 1, "raster_convert": 1, "full_page": 1,
                "orientation": 4, "docx_salvage": 1, "text_decode": 1}
EXTERNAL_TIMEOUT = 120
MAX_ZIP_MEMBERS = 2048
MAX_ZIP_BYTES = 64 * 1024 * 1024
MAX_PDF_CHECK_PAGES = 10000


def _sha(path):
    return checksum(path)


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k != "document"}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _fatal(exc):
    if isinstance(exc, IntegrityError):
        raise exc
    # Any filesystem failure may be publication/storage failure. Only content
    # decode failures raised as ValueError/RuntimeError are recovered here.
    if isinstance(exc, OSError):
        raise exc


def _mechanical(text, native_empty=False):
    if native_empty or not text.strip():
        return "empty reading"
    bad = sum(c == "\ufffd" or (unicodedata.category(c) == "Cc" and c not in "\n\r\t") for c in text)
    if bad / len(text) > .02:
        return "replacement/control character ratio exceeds 2%"
    return None


def _suspicious(row):
    text = row.get("text", "")
    if _mechanical(text, row.get("native_text_empty", False)):
        return _mechanical(text, row.get("native_text_empty", False))
    tokens = text.split()
    if len(tokens) >= 12 and len(set(tokens)) <= 2:
        return "repeated-token reading"
    warnings = " ".join(map(str, row.get("recognition_warnings", []))).lower()
    if any(w in warnings for w in ("sparse", "orientation", "metadata", "embedded text")):
        return "reported orientation/metadata/sparse-text warning"
    return None


def _run_tool(command):
    proc = subprocess.run(command, capture_output=True, text=True, timeout=EXTERNAL_TIMEOUT)
    if any(phrase in (proc.stderr + proc.stdout).lower() for phrase in ("no space left", "disk quota exceeded")):
        raise OSError(errno.ENOSPC, "recovery tool reported exhausted storage")
    return proc


def _tool_version(name):
    executable = shutil.which(name)
    if not executable:
        return None
    proc = _run_tool([executable, "--version" if name == "qpdf" else "-v"])
    return (proc.stdout + proc.stderr).splitlines()[0] if proc.stdout + proc.stderr else "unknown"


def _backend_versions():
    versions = {}
    for name in ("docling", "docling-core", "docling-parse", "Pillow"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


class _Journal:
    def __init__(self, root, path, kind, start, end, options):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise IntegrityError("recovery scratch must not be a symlink")
        self.file = self.root / "recovery-state.json"
        self.identity = {"source_sha256": _sha(path), "kind": kind, "start": start, "end": end,
                         "settings": options, "caps": ATTEMPT_CAPS}
        if self.file.is_symlink():
            raise IntegrityError("recovery journal must not be a symlink")
        if self.file.exists():
            self.data = json.loads(self.file.read_text())
            if self.data.get("identity") != self.identity:
                raise IntegrityError("recovery source/settings changed; preserve scratch for inspection")
            for artifact in self.data.get("artifacts", []):
                target = self.root / artifact["path"]
                if not target.resolve().is_relative_to(self.root.resolve()) or target.is_symlink() or not target.is_file() or verify_pinned(target,artifact)["sha256"] != artifact["sha256"]:
                    raise IntegrityError("recovery artifact missing, changed or unsafe")
        else:
            self.data = {"identity": self.identity, "attempts": [], "artifacts": []}
            self.save()

    def save(self):
        temporary = self.root / "recovery-state.tmp"
        if temporary.exists() or temporary.is_symlink():
            raise IntegrityError("unresolved recovery journal temporary file")
        with temporary.open("x") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.file)

    def attempt(self, stage, uid, trigger, action, *, settings=None, tool=None):
        prior = [a for a in self.data["attempts"] if a["stage"] == stage and a["unit_id"] == uid]
        matching = [a for a in prior if a.get("settings", {}) == (settings or {})]
        if matching:
            previous = matching[-1]
            if previous.get("status") == "finished" and "outcome" in previous:
                value = deepcopy(previous["outcome"])
                native = previous.get("native_document")
                if native:
                    from docling_core.types.doc import DoclingDocument
                    value["document"] = DoclingDocument.load_from_json(self.root / native)
                return value
            # A started/crashed stage remains consumed. Other finite stages can
            # proceed; only the runner authorizes a fresh retry budget.
            if previous.get("status") in ("started", "failed"):
                return None
        if len(prior) >= ATTEMPT_CAPS[stage]:
            return None
        directory = self.root / f"attempt-{len(self.data['attempts']) + 1:04d}-{stage}"
        directory.mkdir(exist_ok=False)
        record = {"stage": stage, "unit_id": uid, "trigger": trigger, "settings": settings or {},
                  "tool_version": _tool_version(tool) if tool else _backend_versions(), "status": "started",
                  "directory": directory.name, "errors": [], "original_unit_mapping": [uid]}
        self.data["attempts"].append(record)
        self.save()
        try:
            value = action(directory, record)
        except (ValueError, RuntimeError, UnicodeError, ImportError, zipfile.BadZipFile, ET.ParseError, subprocess.SubprocessError) as exc:
            _fatal(exc)
            record.update(status="failed", errors=[f"{type(exc).__name__}: {exc}"])
            self.save()
            return None
        if isinstance(value, dict) and value.get("document") is not None:
            from docling_core.types.doc import ImageRefMode
            native = directory / "native-candidate.json"
            value["document"].save_as_json(native, image_mode=ImageRefMode.EMBEDDED)
            self.artifact(native, [uid], role="native recovery document")
            record["native_document"] = native.relative_to(self.root).as_posix()
        record["status"] = "finished"
        record["derivatives"] = [deepcopy(a) for a in self.data["artifacts"] if a["path"].startswith(directory.name + "/")]
        if isinstance(value, dict):
            record["outcome"] = _clean(value)
        self.save()
        return value

    def artifact(self, path, mapping, **extra):
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise IntegrityError("unsafe recovery derivative")
        value = {"path": path.relative_to(self.root).as_posix(), **file_record(path),
                 "original_unit_mapping": mapping, **extra}
        self.data["artifacts"].append(value)
        return value


def _fingerprints(path):
    """Independent count + ordered visible page fingerprints; never guess order."""
    import pypdfium2 as pdfium
    rows = []
    with pdfium.PdfDocument(path) as document:
        if len(document) > MAX_PDF_CHECK_PAGES:
            raise ValueError("PDF exceeds bounded rewrite-order validation budget")
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                finally:
                    textpage.close()
                bitmap = page.render(scale=.25)
                try:
                    pixels = bitmap.to_pil().tobytes()
                finally:
                    bitmap.close()
                rows.append([list(page.get_size()), hashlib.sha256(text.encode()).hexdigest(), hashlib.sha256(pixels).hexdigest()])
            finally:
                page.close()
    return rows


def _qpdf(path, directory, record, journal):
    executable = shutil.which("qpdf")
    if executable is None:
        raise RuntimeError("optional qpdf is unavailable")
    before = _fingerprints(path)
    target = directory / "rewritten.pdf"
    proc = _run_tool([executable, str(path), str(target)])
    record.update(command=[executable, str(path), str(target)], returncode=proc.returncode,
                  stdout=proc.stdout, stderr=proc.stderr)
    if proc.returncode not in (0, 3):
        raise RuntimeError(f"qpdf failed ({proc.returncode}): {proc.stderr}")
    after = _fingerprints(target)
    if not before or before != after:
        raise ValueError("qpdf rewrite failed independent count/order/content fingerprints")
    record.update(count_before=len(before), count_after=len(after), order_checked=True,
                  warning_exit_3=proc.returncode == 3)
    journal.artifact(target, list(range(1, len(before) + 1)), role="qpdf rewrite", warning_exit_3=proc.returncode == 3)
    return {"path": str(target), "expected_count": len(before), "warning_exit_3": proc.returncode == 3}


def _salvage_docx(path, directory, record, journal):
    units, unparsed, total = [], [], 0
    with zipfile.ZipFile(path) as package:
        members = package.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise ValueError("DOCX exceeds bounded member budget")
        name_counts = Counter(info.filename for info in members)
        for index, info in enumerate(members):
            name = info.filename
            if not name.startswith("word/") or not (name.endswith(".xml") or name.startswith("word/media/")):
                continue
            if info.flag_bits & 1:
                unparsed.append({"member": name, "error": "encrypted member; password required"})
                continue
            if info.file_size > MAX_ZIP_BYTES - total:
                unparsed.append({"member": name, "error": "bounded uncompressed-size budget exceeded"})
                continue
            member_locator = f"package:{name}"
            if name_counts[name] > 1:
                member_locator += f"!zip-entry:{index + 1}"
                unparsed.append({"member": name, "zip_entry": index + 1, "error": "duplicate ZIP member name; occurrence retained separately, identity ambiguous"})
            total += info.file_size
            try:
                data = package.read(info)
            except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
                unparsed.append({"member": name, "error": str(exc)})
                continue
            suffix = Path(name).suffix if Path(name).suffix.lower() in ('.xml', '.png', '.jpg', '.jpeg', '.gif', '.tif', '.tiff', '.bmp') else '.bin'
            media_error = None
            if name.startswith("word/media/") and suffix.lower() != ".bin":
                from PIL import Image
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        for frame in range(getattr(image, "n_frames", 1)):
                            image.seek(frame)
                            image.load()
                except (OSError, ValueError, SyntaxError, EOFError) as exc:
                    if isinstance(exc, OSError) and exc.errno is not None:
                        raise
                    media_error = str(exc)
                    suffix = ".bin"
                    unparsed.append({"member": name, "error": "undecodable media retained as raw bytes: " + media_error})
            target = directory / f"member-{index:04d}{suffix}"
            with target.open("xb") as stream:
                stream.write(data)
            journal.artifact(target, [member_locator], role="DOCX package member", package_member=name,
                             media_decode_status="failed" if media_error else "not_failed", media_error=media_error)
            if name.startswith("word/media/"):
                continue
            try:
                # ElementTree performs standard XML parsing, never regex repair.
                root = ET.fromstring(data)
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                paragraphs = []
                for number, paragraph in enumerate(root.iter(ns + "p"), 1):
                    text = "".join(element.text or "" for element in paragraph.iter(ns + "t"))
                    if text:
                        paragraphs.append({"locator": f"{member_locator}#paragraph:{number}", "text": text})
                tables = []
                for number, table in enumerate(root.iter(ns + "tbl"), 1):
                    rows = [["".join(t.text or "" for t in cell.iter(ns + "t")) for cell in row.findall(ns + "tc")] for row in table.findall(ns + "tr")]
                    tables.append({"locator": f"{member_locator}#table:{number}", "rows": rows})
                if paragraphs:
                    units.append({"unit_id": member_locator, "decode_status": "not_applicable", "conversion_status": "partial",
                                  "text": "\n\n".join(p["text"] for p in paragraphs), "doc_page_no": None, "transform": {},
                                  "structural_locators": paragraphs, "tables": tables,
                                  "recognition_warnings": ["partial readable ZIP/XML salvage; package completeness unknown"], "attempts": []})
            except ET.ParseError as exc:
                unparsed.append({"member": name, "error": str(exc)})
    record["unparsed_members"] = unparsed
    return {"document": None, "units": units, "status": "partial" if units else "failed", "errors": unparsed,
            "provenance": {"backend": "standard-zip-xml-salvage", "native_document": False, "pagination": "none", "unparsed_members": unparsed}}


def _decode_text(path, directory, record, journal):
    raw = Path(path).read_bytes()
    encoding, bom = "utf-8", False
    for marker, candidate in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                              (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"), (codecs.BOM_UTF8, "utf-8-sig")):
        if raw.startswith(marker):
            encoding, bom = candidate, True
            break
    ambiguity = False
    try:
        text = raw.decode(encoding, errors="strict")
    except UnicodeError as exc:
        record["strict_error"] = str(exc)
        if bom:
            raise ValueError("declared BOM decoding failed; refusing conflicting legacy decoding") from exc
        encoding, ambiguity = "cp1252", True
        text = raw.decode(encoding, errors="strict")
    record.update(encoding=encoding, decoding_ambiguity=ambiguity)
    warning = ["legacy cp1252 decoding candidate; encoding ambiguity unresolved"] if ambiguity else []
    target = directory / "decoded-utf8.txt"
    with target.open("x", encoding="utf-8") as stream:
        stream.write(text)
    journal.artifact(target, ["lines"], role="strict decoded candidate", encoding=encoding, decoding_ambiguity=ambiguity)
    return {"document": None, "units": [{"unit_id": f"line:{n}", "decode_status": "not_applicable", "conversion_status": "partial" if ambiguity else "ok", "text": line,
                                            "recognition_warnings": warning, "doc_page_no": None, "transform": {}, "attempts": []}
                                           for n, line in enumerate(text.splitlines(keepends=True), 1)],
            "status": "partial" if ambiguity else "ok", "errors": [], "provenance": {"backend": "strict-text-recovery", "encoding": encoding, "decoding_ambiguity": ambiguity, "native_document": False}}


def recover_range(backend, path, kind, start, end, result, *, scratch, rendered=()):
    """Recover only eligible original units, preserving all native candidates.

    ``variants[0]`` is always the unchanged baseline. When a candidate is selected
    the primary has no native document: export variants individually, then use
    primary unit text plus ``variant_index`` to link the preferred reading.
    Scratch belongs to one source/range and survives until runner publication.
    """
    path = Path(path)
    options = getattr(backend, "options", {})
    settings = options.frozen() if hasattr(options, "frozen") else asdict(options) if is_dataclass(options) else dict(options or {})
    journal = _Journal(scratch, path, kind, start, end, settings)
    output = {**result, "units": deepcopy(result.get("units", [])), "errors": deepcopy(result.get("errors", []))}
    baseline = {**result, "units": deepcopy(result.get("units", [])), "variant_role": "baseline"}
    output["variants"] = [baseline]
    output.setdefault("recognition_warnings", [])
    output["recognition_warnings"] = list(output["recognition_warnings"])
    render_evidence = {r["unit_id"]: r for r in rendered}
    for row in output["units"]:
        evidence = render_evidence.get(row["unit_id"], {})
        messages = list(evidence.get("recognition_warnings", [])) + list(evidence.get("errors", []))
        if messages:
            row.setdefault("recognition_warnings", []).extend("independent render: " + str(m) for m in messages)
    changed = False

    def candidate(value, stage, uid, *, select_failed=True, mechanical=False):
        nonlocal changed
        if not value:
            return
        value = {**value, "variant_role": "recovery candidate", "recovery_stage": stage}
        index = len(output["variants"])
        output["variants"].append(value)
        row = next((u for u in value.get("units", []) if u["unit_id"] == uid), None)
        current = next((u for u in output["units"] if u["unit_id"] == uid), None)
        if row is None or row.get("conversion_status") == "failed" or current is None:
            return
        eligible = (select_failed and current.get("conversion_status") == "failed") or (mechanical and _mechanical(current.get("text", ""), current.get("native_text_empty", False)) and not _mechanical(row.get("text", ""), row.get("native_text_empty", False)))
        if eligible:
            replacement = deepcopy(row)
            replacement["variant_index"] = index
            replacement["recognition_warnings"] = list(replacement.get("recognition_warnings", [])) + ["automatically selected recovery candidate; baseline retained"]
            output["units"][output["units"].index(current)] = replacement
            changed = True
        elif row.get("text") != current.get("text"):
            current.setdefault("recognition_warnings", []).append(f"alternative {stage} reading retained; no automatic preference")

    def conversion(stage, uid, source=path, source_kind=kind, mode="default", source_unit=None):
        def action(directory, record):
            value = backend.convert_range(source, source_kind, source_unit or uid, source_unit or uid, mode=mode, scratch=directory)
            if source_unit is not None:
                for row in value.get("units", []):
                    row["unit_id"] = uid
                    row["transform"] = {**row.get("transform", {}), "recovery_original_unit": uid, "coordinate_space": "raster derivative"}
                value.setdefault("provenance", {})["original_mapping"] = {str(source_unit): uid}
            return value
        return journal.attempt(stage, uid, "failed original unit" if stage != "full_page" else "mechanically suspicious/empty reading", action, settings={"mode": mode, "source_kind": source_kind})

    if kind == "txt" and result.get("status") != "ok":
        value = journal.attempt("text_decode", "lines", "strict default text decoding failed", lambda d, r: _decode_text(path, d, r, journal))
        if value:
            output["variants"].append({**value, "recovery_stage": "text_decode"})
            output.update(units=deepcopy(value["units"]), status=value["status"], document=None)
            changed = True
    elif kind == "docx" and result.get("status") != "ok":
        value = journal.attempt("docx_salvage", "package", "built-in OOXML conversion failed/partial", lambda d, r: _salvage_docx(path, d, r, journal))
        if value:
            output["variants"].append({**value, "recovery_stage": "docx_salvage"})
            # Preserve readable main-body partial text as well as healthy
            # members; a damaged header must not discard the main document.
            existing = {u["unit_id"]: u for u in output["units"]}
            for recovered in value["units"]:
                previous = existing.get(recovered["unit_id"])
                if previous is None:
                    output["units"].append(deepcopy(recovered))
                    changed = True
                elif previous.get("conversion_status") == "failed":
                    output["units"][output["units"].index(previous)] = deepcopy(recovered)
                    changed = True
            output["recognition_warnings"].append("DOCX package salvage remains partial; unparsed/missing members are gaps")
            output["errors"].extend(value.get("errors", []))
    elif kind in ("pdf", "image", "tiff"):
        inventory = inspect_source(path, kind)
        output["recovery_inventory"] = inventory
        if inventory["expected_count"] is None:
            output["recognition_warnings"].append("original unit total unknown; source remains incomplete")
        known = {u["unit_id"] for u in output["units"]}
        for uid in range(start, end + 1):
            if uid not in known:
                output["units"].append({"unit_id": uid, "conversion_status": "failed", "decode_status": "unknown", "text": "", "error": "range omitted original unit", "recognition_warnings": [], "attempts": [], "doc_page_no": uid, "transform": {}})
        for row in list(output["units"]):
            uid = row["unit_id"]
            if row.get("conversion_status") == "failed":
                candidate(conversion("singleton", uid), "singleton", uid)
        if kind == "pdf":
            for row in list(output["units"]):
                if row.get("conversion_status") == "failed":
                    candidate(conversion("pdfium", row["unit_id"], mode="pdfium"), "pdfium", row["unit_id"])
            failed = [u["unit_id"] for u in output["units"] if u.get("conversion_status") == "failed"]
            if failed:
                repaired = journal.attempt("qpdf", "source", "PDF parser failures remain", lambda d, r: _qpdf(path, d, r, journal), tool="qpdf")
                if repaired:
                    for uid in failed:
                        candidate(conversion("qpdf_convert", uid, source=Path(repaired["path"])), "qpdf_convert", uid)
            for row in list(output["units"]):
                uid = row["unit_id"]
                if row.get("conversion_status") != "failed":
                    continue
                def poppler(directory, record, uid=uid):
                    executable = shutil.which("pdftoppm")
                    if not executable:
                        raise RuntimeError("optional Poppler pdftoppm unavailable")
                    target = directory / "page"
                    command = [executable, "-f", str(uid), "-l", str(uid), "-singlefile", "-r", str(72 * settings.get("images_scale", 3)), "-png", str(path), str(target)]
                    proc = _run_tool(command)
                    record.update(command=command, returncode=proc.returncode, stderr=proc.stderr)
                    if proc.returncode != 0 or not target.with_suffix(".png").is_file():
                        raise RuntimeError(f"Poppler page rescue failed: {proc.stderr}")
                    image = target.with_suffix(".png")
                    journal.artifact(image, [uid], role="Poppler individual-page raster", transform={"images_scale": settings.get("images_scale", 3)})
                    return {"path": str(image)}
                raster = journal.attempt("poppler", uid, "PDF parser repair exhausted", poppler, tool="pdftoppm")
                if raster:
                    candidate(conversion("raster_convert", uid, source=Path(raster["path"]), source_kind="image", source_unit=1), "raster_convert", uid)
        for row in list(output["units"]):
            uid = row["unit_id"]
            trigger = _suspicious(row)
            if row.get("conversion_status") != "failed" and trigger:
                candidate(conversion("full_page", uid, mode="full_page"), "full_page", uid, mechanical=True)
            # Only reported orientation/metadata/sparse errors or failed image
            # decode enter this one phase; never combine it with parser modes.
            warnings = " ".join(map(str, row.get("recognition_warnings", []))) + " " + str(row.get("error", ""))
            orientation = any(token in warnings.lower() for token in ("orientation", "metadata", "dpi", "sparse"))
            if kind in ("image", "tiff") and row.get("conversion_status") == "failed":
                orientation = True
            if not orientation:
                continue
            candidates = []
            for rotation in (0, 90, 180, 270):
                def orient(directory, record, rotation=rotation):
                    from PIL import Image
                    # C applies safe EXIF/DPI normalization exactly once.
                    renders = render_units(path, kind, [uid], directory / "pixels", rotate_clockwise=rotation, images_scale=settings.get("images_scale", 3))
                    rendered_row = renders[0]
                    if rendered_row["decode_status"] != "ok" and kind in ("image", "tiff"):
                        # Invalid orientation is metadata, not proof that pixels
                        # are missing. Pillow may decode them without applying
                        # an invalid tag; declare that uncertainty explicitly.
                        try:
                            with Image.open(path) as original:
                                original.seek(uid - 1)
                                tag = original.getexif().get(274, 1)
                                if not (isinstance(tag, bool) or tag not in range(1, 9)):
                                    raise ValueError("orientation render failed: " + str(rendered_row.get("errors")))
                                pixels = original.copy().convert("RGB")
                        except OSError as exc:
                            # Pillow reports malformed source bytes as errno-less
                            # OSError. Limit this downgrade to input decoding;
                            # output saves and real filesystem errors stay fatal.
                            if exc.errno is not None:
                                raise
                            raise ValueError("image pixels are undecodable: " + str(exc)) from exc
                        if rotation:
                            pixels = pixels.rotate(-rotation, expand=True)
                        pixels.info.clear()
                        image = directory / "invalid-metadata.png"
                        pixels.save(image, format="PNG", dpi=(72, 72))
                        rendered_row = {"decode_status": "ok", "asset": image.name,
                                        "transform": {"invalid_exif_orientation": repr(tag),
                                                      "exif_normalized": False,
                                                      "metadata_removed": True, "dpi_fallback": [72, 72],
                                                      "rotate_clockwise": rotation, "orientation_uncertain": True}}
                    elif rendered_row["decode_status"] != "ok":
                        raise ValueError("orientation render failed: " + str(rendered_row.get("errors")))
                    else:
                        image = directory / "pixels" / rendered_row["asset"]
                    journal.artifact(image, [uid], role="orientation candidate", transform=rendered_row["transform"])
                    # These pixels are normalized; neutralize explicit source
                    # rotation in a separate backend to avoid applying it twice.
                    from .docling_backend import BackendOptions, DoclingBackend
                    candidate_backend = backend
                    if settings.get("rotate_clockwise", 0):
                        candidate_backend = DoclingBackend(BackendOptions(**{**settings, "rotate_clockwise": 0}))
                    value = candidate_backend.convert_range(image, "image", 1, 1, mode="full_page", scratch=directory)
                    for unit in value.get("units", []):
                        unit.update(unit_id=uid, transform=rendered_row["transform"])
                    value.setdefault("provenance", {})["original_mapping"] = {"1": uid}
                    return value
                value = journal.attempt("orientation", uid, "reported orientation/metadata/sparse or decode failure", orient, settings={"rotate_clockwise": rotation, "mode": "full_page"})
                if value:
                    candidates.append(value)
            useful = [v for v in candidates if any(u.get("conversion_status") == "ok" and not _mechanical(u.get("text", ""), u.get("native_text_empty", False)) for u in v.get("units", []))]
            # A sole usable candidate is mechanical evidence; competing readable
            # candidates require inspection, regardless of character counts.
            for value in candidates:
                candidate(value, "orientation", uid, select_failed=len(useful) == 1 and value is useful[0], mechanical=len(useful) == 1 and value is useful[0])
            if len(useful) > 1:
                output["recognition_warnings"].append(f"unit {uid}: ambiguous orientation candidates retained")
    if changed:
        output["document"] = None
    statuses = {u.get("conversion_status") for u in output["units"]}
    output["status"] = "ok" if statuses == {"ok"} else "partial" if statuses.intersection({"ok", "partial"}) else "failed"
    if output.get("recovery_inventory", {}).get("expected_count", 0) is None and output["status"] == "ok":
        output["status"] = "partial"
    if kind == "txt" and any(v.get("provenance", {}).get("decoding_ambiguity") for v in output["variants"]):
        output["status"] = "partial"
    if kind == "docx" and any(v.get("recovery_stage") == "docx_salvage" for v in output["variants"]):
        output["status"] = "partial" if output["units"] else "failed"
    output["recovery_attempts"] = deepcopy(journal.data["attempts"])
    output["recovery_artifacts"] = deepcopy(journal.data["artifacts"])
    output["recovery_scratch"] = str(journal.root)
    output["recovery_exhausted"] = True
    journal.save()
    return output
