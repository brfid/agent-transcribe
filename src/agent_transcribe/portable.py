"""Portable, checked views of bounded Docling results; no inference or publication.

The orchestrator owns source copies, immutable acceptance and conversion.json.
These functions write only the explicitly supplied staging/item directories.
"""
import copy
import json
import os
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from .common import IntegrityError, atomic, checksum, json_bytes
from .docling_backend import native_text_empty


_VISIBLE = {"body", "furniture", "notes"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".bmp"}


def _path(root, relative):
    """Require an ordinary contained path (parents may not be symlinks)."""
    root = Path(root).absolute()
    value = str(relative)
    rel = PurePosixPath(value)
    if not value or rel.is_absolute() or "\\" in value or any(p in ("..", ".") for p in rel.parts):
        raise IntegrityError(f"Unsafe relative path: {value!r}")
    target = root.joinpath(*rel.parts)
    for part in (root, *(parent for parent in target.parents if parent.is_relative_to(root)), target):
        if part.is_symlink():
            raise IntegrityError(f"Symlink is not a portable artifact: {part}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise IntegrityError(f"Artifact escapes bundle: {value}")
    return target


def _record(root, path):
    path = Path(path)
    return {"path": path.relative_to(root).as_posix(), "sha256": checksum(path), "size": path.stat().st_size}


def _files(root):
    root = Path(root)
    records = []
    for path in sorted(root.rglob("*")):
        _path(root, path.relative_to(root).as_posix())
        if path.is_file():
            records.append(_record(root, path))
    return records


def _write(path, value):
    atomic(path, json_bytes(value) if not isinstance(value, str) else value, replace=False)


def _url(path, fragment=""):
    return quote(str(path).replace(os.sep, "/"), safe="/") + fragment


def _link(label, path, fragment=""):
    label = str(label).replace("[", r"\[").replace("]", r"\]")
    return f"[{label}]({_url(path, fragment)})"


def _literal(text):
    """Display archival wording literally; it is not bundle Markdown syntax."""
    fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", text)), default=3))
    return fence + "text\n" + text + ("" if text.endswith("\n") else "\n") + fence + "\n"


def _markdown_links(text):
    """Only rendered links count; fenced transcription/code contains no links."""
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            token, rest = marker.groups()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not rest.strip():
                fence = None
            continue
        if fence is None:
            yield from re.finditer(r"(!?)\[[^\]]*\]\((<[^>]*>|[^\s)]*)(?:\s+[^)]*)?\)", line)


def _locator(source, unit, unit_kind=None):
    kind = unit_kind or {"pdf": "page", "tiff": "frame", "image": "page", "txt": "line", "docx": "package"}.get(source.get("kind"), "unit")
    return {"source": source["relative_path"], "unit_kind": kind, "unit_id": unit}


def _label(locator):
    return f"{locator['source']} — {locator['unit_kind']} {locator['unit_id']}"


def _source(source):
    # Absolute operating paths are optional historical metadata, never opening links.
    return {key: copy.deepcopy(source[key]) for key in ("relative_path", "kind", "order", "size", "mtime_ns", "sha256", "hash_status", "prior_coverage") if key in source}


def export_part(result, directory, *, source, rendered=()):
    """Export a new staged part, retaining original-unit identity and native IDs.

    ``rendered`` paths are already rebased to ``directory``. Independent renders
    survive inference failure. A document is never manufactured for plain text.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Render checkpoints may preexist; semantic outputs must be entirely new.
    for name in ("document.json", "document.md", "units", "crops", "part.json"):
        if (directory / name).exists():
            raise IntegrityError(f"Part output already exists: {directory / name}")
    for existing in directory.rglob("*"):
        _path(directory, existing.relative_to(directory).as_posix())
    units = copy.deepcopy(result.get("units", []))
    seen = set()
    for row in units:
        uid = row["unit_id"]
        if uid in seen:
            raise IntegrityError(f"Duplicate original unit: {uid}")
        seen.add(uid)
        row["original_locator"] = _locator(source, uid, row.get("unit_kind"))
    renders = {}
    for row in rendered:
        uid = row["unit_id"]
        if uid in renders:
            raise IntegrityError(f"Duplicate rendered unit: {uid}")
        value = copy.deepcopy(row)
        if value.get("asset"):
            asset = _path(directory, value["asset"])
            if not asset.is_file():
                raise IntegrityError(f"Missing rendered checkpoint: {asset}")
        renders[uid] = value
    if set(renders) - seen:
        raise IntegrityError("Rendered units are absent from conversion accounting")
    document = result.get("document")
    errors = list(result.get("errors", []))
    warnings = list(result.get("warnings", []))
    refs = []
    native = None
    markdown = None
    if document is not None:
        from docling_core.types.doc import ContentLayer, DocItem, ImageRefMode, PictureItem, TableItem
        layers = {layer for layer in ContentLayer if layer.value in _VISIBLE}
        opts = {"artifacts_dir": Path("assets"), "image_mode": ImageRefMode.REFERENCED}
        document.save_as_json(directory / "document.json", ensure_ascii=False, **opts)
        document.save_as_markdown(directory / "document.md", included_content_layers=layers, traverse_pictures=True, **opts)
        native, markdown = "document.json", "document.md"
        page_map = {}
        for row in units:
            page = row.get("doc_page_no")
            if page is not None:
                if page in page_map:
                    raise IntegrityError(f"Ambiguous Docling page mapping: {page}")
                page_map[page] = row
        for item, _ in document.iterate_items(traverse_pictures=True, included_content_layers=set(ContentLayer)):
            if not getattr(item, "prov", ()) and source.get("kind") == "docx" and len(units) == 1:
                refs.append({"item_ref": item.self_ref, "unit_id": units[0]["unit_id"], "original_locator": units[0]["original_locator"], "doc_page_no": None, "provenance_index": None, "content_layer": item.content_layer.value, "label": item.label.value, "crop": None, "structural_locator": item.self_ref})
            for index, prov in enumerate(getattr(item, "prov", ())):
                row = page_map.get(prov.page_no)
                # DOCX provenance can be structural, with no original physical page.
                if row is None and source.get("kind") == "docx" and len(units) == 1:
                    row = units[0]
                if row is None:
                    raise IntegrityError(f"Unmapped native provenance page {prov.page_no}: {item.self_ref}")
                page = document.pages.get(prov.page_no)
                ref = {"item_ref": item.self_ref, "unit_id": row["unit_id"], "original_locator": row["original_locator"], "doc_page_no": prov.page_no, "provenance_index": index, "bbox": prov.bbox.model_dump(mode="json"), "charspan": list(prov.charspan), "content_layer": item.content_layer.value, "label": item.label.value, "page_size": page.size.model_dump(mode="json") if page and page.size else None, "pixel_size": page.image.size.model_dump(mode="json") if page and page.image else None, "transform": row.get("transform", {}), "crop": None}
                if isinstance(item, (PictureItem, TableItem)):
                    # FloatingItem.get_image ignores span when it has an image.
                    # The base method uses provenance origin conversion and scale.
                    try:
                        image = DocItem.get_image(item, document, prov_index=index)
                    except ValueError as exc:
                        image = None
                        ref["crop_error"] = f"Invalid provenance geometry: {exc}"
                    if image is not None:
                        path = Path("crops") / f"{len(refs):06d}-span-{index}.png"
                        (directory / path).parent.mkdir(exist_ok=True)
                        image.save(directory / path)
                        ref["crop"] = path.as_posix()
                    else:
                        ref.setdefault("crop_error", "No full-page pixels for provenance crop")
                        warnings.append(f"Missing crop for {item.self_ref} span {index}")
                refs.append(ref)
        if any(getattr(item, "content_layer", None).value not in _VISIBLE for item, _ in document.iterate_items(included_content_layers=set(ContentLayer), traverse_pictures=True)):
            warnings.append("Native JSON also retains background/invisible layers; readable text includes body, furniture and notes only")
    else:
        warnings.append("No native Docling document; text is pass-through or recovery salvage")
    (directory / "units").mkdir()
    for index, row in enumerate(units, 1):
        render = renders.get(row["unit_id"])
        if render is not None:
            row["render"] = render
            row["decode_status"] = render["decode_status"]
        row.setdefault("recognition_warnings", [])
        row.setdefault("attempts", [])
        row.setdefault("transform", {})
        row["native_json"] = native
        row["item_refs"] = [r for r in refs if r["unit_id"] == row["unit_id"]]
        path = Path("units") / f"{index:06d}.md"
        # Native page export includes structured tables, furniture and picture children.
        if document is not None and row.get("doc_page_no") is not None:
            document.save_as_markdown(directory / path, page_no=row["doc_page_no"], artifacts_dir=Path("../assets"), image_mode=ImageRefMode.REFERENCED, included_content_layers=layers, traverse_pictures=True)
            content = (directory / path).read_text()
        else:
            content = _literal(row.get("text", "")) if row.get("text", "").strip() else ""
        if source.get("kind") in ("pdf", "image", "tiff") and document is not None:
            row["text_empty"] = native_text_empty(document, row.get("doc_page_no"))
        elif source.get("kind") in ("pdf", "image", "tiff") and isinstance(row.get("native_text_empty"), bool):
            # Mixed/selected recovery has no combined native document. Retain
            # each unit's native evidence instead of reinterpreting Markdown.
            row["text_empty"] = row["native_text_empty"]
        else:
            row["text_empty"] = not bool(row.get("text", content).strip())
        header = f"# {_label(row['original_locator'])}\n\nCandidate machine reading. Recognition accuracy is unverified.\n\n"
        if row.get("conversion_status") != "ok":
            header += f"Conversion gap: {row.get('error') or row.get('conversion_status', 'unknown')}.\n\n"
        if row["text_empty"]:
            header += "No searchable text detected; blankness has not been visually verified.\n\n"
        if not content.strip():
            content = "[No text detected; blankness has not been visually verified.]\n"
        if render and render.get("asset"):
            header += _link("Full-page/frame image", os.path.relpath(directory / render["asset"], (directory / path).parent)) + "\n\n"
        # The page exporter just created this file; prepend its explicitly labeled view.
        if (directory / path).exists():
            atomic(directory / path, header + content)
        else:
            _write(directory / path, header + content)
        row["text_path"] = path.as_posix()
    manifest = {"kind": "docling-part", "schema": 1, "source": _source(source), "units": units, "item_refs": refs, "native_json": native, "markdown": markdown, "native_document": document is not None, "status": result.get("status"), "errors": errors, "warnings": warnings, "provenance": copy.deepcopy(result.get("provenance", {})), "files": _files(directory)}
    _write(directory / "part.json", manifest)
    manifest["files"].append(_record(directory, directory / "part.json"))
    return manifest


def _part_manifest(part):
    return part.get("manifest", part.get("export", part))


def _expected(inventory):
    rows = inventory.get("units", [])
    ids = [row["unit_id"] if isinstance(row, dict) else row for row in rows]
    count = inventory.get("expected_count")
    if not ids and count is not None and inventory.get("unit_kind") in ("page", "frame"):
        ids = list(range(1, count + 1))
    if len(ids) != len(set(ids)):
        raise IntegrityError("Duplicate independently enumerated original units")
    if count is not None and len(ids) != count:
        raise IntegrityError("Independent unit enumeration disagrees with expected count")
    return ids


def _check_accounting(inventory, units):
    ids = [row["unit_id"] for row in units]
    if len(ids) != len(set(ids)):
        raise IntegrityError("Duplicate original unit across accepted parts")
    expected = _expected(inventory)
    if set(ids) != set(expected):
        raise IntegrityError(f"Original-unit accounting differs: expected {expected}, got {ids}")
    return inventory.get("expected_count") is not None


def write_item(bundle_root, item_relative, source, inventory, parts, *, reuse=None):
    """Write checked source map and preferred/candidate views; never copy sources."""
    bundle_root = Path(bundle_root)
    item = _path(bundle_root, item_relative)
    item.mkdir(parents=True, exist_ok=True)
    original_relative = "Originals/" + source["relative_path"]
    original = _path(bundle_root, original_relative)
    if not original.is_file() or not source.get("sha256") or checksum(original) != source["sha256"]:
        raise IntegrityError("Copied original is missing or differs from prepared source")
    units = []
    parts_out = []
    for part in parts:
        relative = part["relative_path"]
        part_dir = _path(item, relative)
        manifest = _part_manifest(part)
        _verify_records(part_dir, manifest.get("files", []))
        parts_out.append({"relative_path": relative, "manifest": copy.deepcopy(manifest)})
        for row in manifest.get("units", []):
            row = copy.deepcopy(row)
            row["part"] = relative
            row["original_locator"] = _locator(source, row["unit_id"], inventory.get("unit_kind"))
            units.append(row)
    known = _check_accounting(inventory, units)
    ordered = {uid: i for i, uid in enumerate(_expected(inventory))}
    units.sort(key=lambda row: ordered[row["unit_id"]])
    mapped = [f"# {source['relative_path']}\n\nCandidate page/frame-mapped machine reading. Recognition accuracy is unverified.\n"]
    original_link = os.path.relpath(original, item)
    for row in units:
        uid = row["unit_id"]
        fragment = f"#page={uid}" if inventory.get("unit_kind") == "page" and source.get("kind") == "pdf" else ""
        mapped.append(f"\n## {_label(row['original_locator'])}\n\n" + _link("Original", original_link, fragment) + "\n")
        render = row.get("render", {})
        if render.get("asset"):
            mapped.append("\n" + _link("Full-page/frame image", f"{row['part']}/{render['asset']}") + "\n")
        text_path = row.get("text_path")
        if text_path:
            path = _path(item, f"{row['part']}/{text_path}")
            # Link to the native page view rather than transplanting relative links.
            mapped.append("\n" + _link("Structured unit view", f"{row['part']}/{text_path}") + "\n")
            # Backend text is deliberately plain: tables remain in linked native view.
            text = row.get("text", "")
        else:
            text = row.get("text", "")
        if row.get("conversion_status") != "ok":
            mapped.append(f"\n[Conversion gap: {row.get('error') or row.get('conversion_status', 'unknown')}]\n")
        mapped.append("\n" + (_literal(text) if text.strip() else "[No text detected; blankness is unverified.]") + "\n")
    mapped_text = "\n".join(mapped)
    candidate = "pages.md" if reuse else None
    preferred_role = "accepted source-level reading" if reuse else "preferred mapped machine reading"
    if reuse:
        retained = Path(reuse["text_path"])
        if retained.is_symlink() or not retained.is_file() or checksum(retained) != reuse.get("text_sha256"):
            raise IntegrityError("Accepted text changed or is unavailable")
        if reuse.get("source_sha256") != source["sha256"]:
            raise IntegrityError("Accepted text belongs to a different original")
        accepted_bytes = retained.read_bytes()
        accepted = accepted_bytes.decode("utf-8")
        atomic(item / "accepted.txt", accepted_bytes, replace=False)
        gaps = reuse.get("gaps", [])
        _write(item / "reading.md", f"# {source['relative_path']}\n\nPreferred accepted source-level reading. Original page boundaries are not inferred.\n\n" + _link("Original", original_link) + "\n\nRecorded gaps: " + json.dumps(gaps, ensure_ascii=False) + "\n\n" + _link("Verbatim accepted text", "accepted.txt") + "\n\n" + _literal(accepted))
        _write(item / "pages.md", mapped_text)
    else:
        _write(item / "reading.md", mapped_text)
    notes = (reuse.get("notes_text") or reuse.get("notes")) if reuse else None
    readme = f"# {source['relative_path']}\n\n" + _link("Preferred reading", "reading.md") + "\n\n" + _link("Copied original", original_link) + "\n\n" + _link("Source map", "source-map.json") + "\n\nDiscovery text is not a verified quotation source. Cite exact original page/frame or structural/line locator; inspect consequential wording.\n"
    if candidate:
        readme += "\n" + _link("Additional mapped candidate", candidate) + "\n"
    if len(parts) > 1:
        readme += "\nPart boundaries preserve native IDs. Reading-order and table continuity across parts are not guaranteed.\n"
    if notes:
        notes = notes if isinstance(notes, str) else json.dumps(notes, ensure_ascii=False)
        # A retained README can refer to its old tree. Keep those references as
        # labeled evidence, never promise that they are local bundle assets.
        notes = re.sub(r"!?\[([^\]]*)\]\(([^)]+)\)", lambda m: m[1] + " (retained reference: " + m[2] + ")", notes)
        readme += "\n## Retained descriptive notes (not source wording)\n\n" + notes + "\n"
    _write(item / "README.md", readme)
    image_required = inventory.get("unit_kind") in ("page", "frame")
    complete = known and not any(ref.get("crop_error") for part in parts_out for ref in part["manifest"].get("item_refs", [])) and all(row.get("conversion_status") == "ok" and (not image_required or (row.get("render", {}).get("decode_status") == "ok" and row.get("render", {}).get("asset"))) for row in units)
    reuse_record = {key: copy.deepcopy(value) for key, value in (reuse or {}).items() if key not in ("text_path", "receipt_path", "notes_path")}
    result = {"kind": "docling-source-map", "schema": 1, "source": _source(source), "original": original_relative, "inventory": copy.deepcopy(inventory), "units": units, "parts": parts_out, "reuse": reuse_record or None, "preferred_role": preferred_role, "reading_path": "reading.md", "candidate_path": candidate, "coverage_accounted": known, "conversion_complete": bool(complete), "publication_complete": True, "warnings": [warning for part in parts_out for warning in part['manifest'].get('warnings', [])], "files": _files(item)}
    _write(item / "source-map.json", result)
    result["map_path"] = "source-map.json"
    result["files"].append(_record(item, item / "source-map.json"))
    return result


def write_bundle_views(bundle_root, receipt):
    """Write root views for the receipt; the caller accepts their manifest."""
    root = Path(bundle_root)
    sources = receipt.get("sources", [])
    sources = list(sources.values()) if isinstance(sources, dict) else sources
    readme = ["# Converted source bundle\n\n", "Discovery text is unverified. Originals, page/frame images and native structure are retained with exact original locators.\n\n"]
    readings = []
    issues = ["# Gaps and warnings\n\n"]
    maps = []
    groups = {}
    for entry in sources:
        relative = entry.get("item_relative")
        source = entry.get("source", {})
        if not relative:
            issues.append(f"- {source.get('relative_path', 'Unknown source')}: no published item; {entry.get('error', entry.get('state', 'pending'))}\n")
            continue
        path = _path(root, f"{relative}/source-map.json")
        if not path.is_file():
            issues.append(f"- {source.get('relative_path', relative)}: item publication pending\n")
            continue
        source_map = json.loads(path.read_text())
        maps.append(source_map)
        inherited = source_map.get("reuse") or {}
        group = inherited.get("name") if inherited.get("group") else "Original folder order (provisional)"
        groups.setdefault(group, []).append("- " + _link(source_map['source']['relative_path'], f"{relative}/README.md") + "\n")
        readings.append(f"{relative}/reading.md\t{source_map['preferred_role']}\n")
        if source_map.get("candidate_path"):
            readings.append(f"{relative}/{source_map['candidate_path']}\tadditional mapped candidate\n")
        for row in source_map["units"]:
            if row.get("conversion_status") != "ok" or (source_map["inventory"].get("unit_kind") in ("page", "frame") and not row.get("render", {}).get("asset")):
                issues.append(f"- {_label(row['original_locator'])}: conversion={row.get('conversion_status')}, image={row.get('render', {}).get('decode_status', 'missing')}\n")
            for warning in row.get("recognition_warnings", []):
                issues.append(f"- {_label(row['original_locator'])}: recognition warning: {warning}\n")
        for warning in source_map.get("warnings", []):
            issues.append(f"- {source_map['source']['relative_path']}: {warning}\n")
    for group, links in groups.items():
        readme.append("\n## " + str(group).replace("\n", " ") + "\n\n")
        readme.extend(links)
    state = receipt.get("state", receipt.get("status", "pending"))
    closeout = ("# Mechanical closeout\n\n" + f"Receipt state: {state}.\n\n" + f"Discovery conversion: {sum(bool(m.get('conversion_complete')) for m in maps)} of {len(sources)} source items complete; see issues.md for retained gaps.\n\n" + "Identification/grouping: not established by conversion. Historical descriptions and relationships require original inspection.\n\n" + "Filing/publication: conversion.json owns accepted checkpoints and completion. Pending publication is incomplete.\n")
    contents = {"README.md": "".join(readme), "readings.txt": "".join(readings), "issues.md": "".join(issues), "closeout.md": closeout}
    for name, text in contents.items():
        _write(root / name, text)
    return {"files": [_record(root, root / name) for name in contents]}


def _verify_records(root, records):
    for record in records:
        path = _path(root, record["path"])
        if not path.is_file() or path.stat().st_size != record["size"] or checksum(path) != record["sha256"]:
            raise IntegrityError(f"Missing or changed artifact: {path}")


def _resolve_link(root, parent, uri, *, image=False):
    parsed = urlsplit(str(uri).strip().strip("<>"))
    if parsed.scheme or parsed.netloc:
        if not image and parsed.scheme in ("http", "https", "mailto"):
            return None  # A reference is not a required bundle asset.
        raise IntegrityError(f"External required artifact: {uri[:300]}")
    if not parsed.path:
        return None
    decoded = unquote(parsed.path)
    if Path(decoded).is_absolute() or "\\" in decoded:
        raise IntegrityError(f"Absolute/unsafe artifact reference: {uri}")
    target = parent / decoded
    for part in (target, *(parent for parent in target.parents if parent.is_relative_to(Path(root).absolute()))):
        if part.is_symlink():
            raise IntegrityError(f"Symlink reference: {uri}")
    target = target.resolve()
    if not target.is_relative_to(Path(root).resolve()) or not target.is_file():
        raise IntegrityError(f"Broken or escaping artifact reference: {uri}")
    return target


def _json_image_links(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uri" and isinstance(child, str):
                yield child
            else:
                yield from _json_image_links(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_image_links(child)


def verify_bundle(root):
    """Verify accepted bytes, links, images and independently counted accounting.

    Valid pending checkpoints return 2. Integrity/publication tampering returns 1.
    Recognition warnings never become claims of transcription accuracy.
    """
    root = Path(root)
    errors, warnings = [], []
    accounted = complete = publication = True
    try:
        _path(root, "conversion.json")
        receipt = json.loads((root / "conversion.json").read_text())
        if receipt.get("kind") != "docling-folder" or receipt.get("schema") != 1:
            raise IntegrityError("Unsupported conversion receipt")
        _verify_records(root, receipt.get("files", []))
        entries = receipt.get("sources", [])
        entries = list(entries.values()) if isinstance(entries, dict) else entries
        seen_sources = set()
        for entry in entries:
            source = entry.get("source", {})
            name = source.get("relative_path")
            if name in seen_sources:
                raise IntegrityError(f"Duplicate selected source: {name}")
            seen_sources.add(name)
            relative = entry.get("item_relative")
            if relative:
                # Accepted file pins apply even during pending publication: a
                # deleted accepted map must not become a harmless pending gap.
                _verify_records(_path(root, relative), entry.get("files", entry.get("map", {}).get("files", [])))
            if not relative or not _path(root, f"{relative}/source-map.json").is_file():
                accounted = complete = publication = False
                continue
            item = _path(root, relative)
            source_map = json.loads((item / "source-map.json").read_text())
            _verify_records(item, source_map.get("files", []))
            if source_map.get("kind") != "docling-source-map" or source_map.get("schema") != 1:
                raise IntegrityError("Unsupported source map")
            if source_map["source"].get("relative_path") != name or source_map["source"].get("sha256") != source.get("sha256"):
                raise IntegrityError("Source map conflicts with frozen source identity")
            if source_map["original"] != "Originals/" + name:
                raise IntegrityError("Copied-original path conflicts with frozen identity")
            original = _path(root, source_map["original"])
            if checksum(original) != source["sha256"]:
                raise IntegrityError("Original copy hash changed")
            known = _check_accounting(source_map["inventory"], source_map["units"])
            accounted &= known
            for part in source_map.get("parts", []):
                manifest = _part_manifest(part)
                _verify_records(_path(item, part["relative_path"]), manifest.get("files", []))
                complete &= not any(ref.get("crop_error") for ref in manifest.get("item_refs", []))
            for row in source_map["units"]:
                locator = row.get("original_locator")
                if locator != _locator(source, row["unit_id"], source_map["inventory"].get("unit_kind")):
                    raise IntegrityError("Original locator changed")
                complete &= row.get("conversion_status") == "ok"
                if source_map["inventory"].get("unit_kind") in ("page", "frame"):
                    render = row.get("render", {})
                    complete &= render.get("decode_status") == "ok" and bool(render.get("asset"))
                warnings.extend(row.get("recognition_warnings", []))
            warnings.extend(source_map.get("warnings", []))
            complete &= known
        # Validate every stored image and every Markdown/native image URI, including
        # pending checkpoints. No Docling import is needed to inspect a bundle.
        for path in root.rglob("*"):
            _path(root, path.relative_to(root).as_posix())
            if not path.is_file():
                continue
            if path.suffix.lower() in _IMAGE_SUFFIXES:
                try:
                    from PIL import Image
                except ImportError as exc:
                    raise ValueError("Pillow is required to verify stored image assets") from exc
                try:
                    with Image.open(path) as image:
                        for frame in range(getattr(image, "n_frames", 1)):
                            image.seek(frame)
                            image.load()
                except (OSError, ValueError, TypeError) as exc:
                    # Pillow also raises TypeError for malformed TIFF directories
                    # without dimensions. Only the image decoder is in this boundary.
                    if path.is_relative_to(root / "Originals"):
                        complete = False
                        warnings.append(f"Original image decode gap ({path.relative_to(root)}): {exc}")
                    else:
                        raise IntegrityError(f"Undecodable derivative image: {path.relative_to(root)}: {exc}") from exc
            if path.suffix == ".md":
                for match in _markdown_links(path.read_text()):
                    _resolve_link(root, path.parent, match.group(2), image=bool(match.group(1)))
            elif path.suffix == ".json":
                data = json.loads(path.read_text())
                # Only native URI fields are required image links; absolute source
                # operating paths and old receipt metadata remain provenance.
                for uri in _json_image_links(data):
                    _resolve_link(root, path.parent, uri, image=True)
        state = receipt.get("state", receipt.get("status", "pending"))
        publication &= state in ("complete", "completed", "partial", "finished") and receipt.get("publication_complete", True)
        if receipt.get("coverage_accounted") is False:
            accounted = False
        if receipt.get("conversion_complete") is False:
            complete = False
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    valid = not errors
    return {"valid": valid, "coverage_accounted": bool(valid and accounted), "conversion_complete": bool(valid and accounted and complete), "publication_complete": bool(valid and publication), "recognition_warnings": warnings, "errors": errors, "exit_code": 1 if errors else (0 if accounted and complete and publication else 2)}
