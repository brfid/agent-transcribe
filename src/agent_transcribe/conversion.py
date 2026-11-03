"""Durable, bounded folder conversion; only this module publishes receipts.

Inference lives in a warm child. Native Docling objects never cross its pipe.
A sibling work directory retains render/prepared checkpoints until accepted.
"""
import contextlib
import copy
import errno
import fcntl
import hashlib
import json
import multiprocessing
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import time
import uuid

from .common import IntegrityError, atomic, checksum, file_record, json_bytes, now
from .pinned_availability import verify_pinned, policy_for, availability_scope, current_policy, set_worker_policy
from .clone_copy import atomic_clone_copy as atomic_copy, clone_copy2
from .docling_backend import BackendOptions, DoclingBackend, inspect_source, preflight, render_units, generated_image_budget
from .folder_intake import load_archive_plan, prepare_source, validate_archive_plan
from .portable import export_part, verify_bundle, write_bundle_views, write_item

RANGE_SIZE = 16
WORKER_WATCHDOG = 600
WORKER_CRASH_RESTARTS = 1
# Disk policy, not a hardware-size requirement or a guaranteed output bound.
# RGBA envelopes cover PNG overhead; copies allow retained render/prepared/native
# exports plus their accepted copies. Repair reserves are reused range by range.
_STORAGE_ASSET_COPIES = 8
_STORAGE_REPAIR_COPIES = 16
_STORAGE_METADATA_BYTES = 1024 * 1024


def _checkpoint_hook(event, receipt):
    """Fault-injection seam; production has no callback or external execution."""


def _safe(root, relative):
    root = Path(root).absolute()
    rel = PurePosixPath(str(relative))
    if not str(relative) or rel.as_posix() != str(relative) or rel.is_absolute() or "\\" in str(relative) or any(p in ("..", ".") for p in rel.parts):
        raise IntegrityError(f"Unsafe output path: {relative!r}")
    target = root.joinpath(*rel.parts)
    for node in (root, *target.parents, target):
        if node.is_symlink():
            raise IntegrityError(f"Symlink in conversion path: {node}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise IntegrityError("Output path escapes selected directory")
    return target


def _records(root, known=None):
    root = Path(root)
    records = []
    for path in sorted(root.rglob("*")):
        _safe(root, path.relative_to(root).as_posix())
        if path.is_file():
            relative=path.relative_to(root).as_posix()
            identity=verify_pinned(path,known[relative]) if known and relative in known else file_record(path)
            records.append({"path":relative,**identity})
    return sorted(records, key=lambda row: row["path"])


def _verify(root, records, *, exact=False):
    for row in records:
        path = _safe(root, row["path"])
        verify_pinned(path,row)
    if exact and _records(root,{r["path"]:r for r in records}) != sorted(records, key=lambda r: r["path"]):
        raise IntegrityError(f"Untracked output in immutable checkpoint: {root}")


def _fatal(exc):
    if isinstance(exc, IntegrityError) or isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        raise exc


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def _durable(root):
    for path in Path(root).rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in Path(root).rglob("*") if p.is_dir()), reverse=True):
        _sync_directory(path)
    _sync_directory(root)


def _save(root, receipt):
    atomic(Path(root) / "conversion.json", json_bytes(receipt))
    _sync_directory(root)
    _checkpoint_hook("receipt", receipt)


def _fingerprint(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def _failure(ids, reason, stage="worker"):
    return {"document": None, "status": "failed", "errors": [reason], "warnings": [reason], "units": [
        {"unit_id": uid, "decode_status": "unknown", "conversion_status": "failed", "text": "", "error": reason,
         "recognition_warnings": [], "attempts": [{"stage": stage, "outcome": "failed", "error": reason}], "transform": {}}
        for uid in ids]}


def _worker_main(connection, options):
    """Warm backend and recovery/export run in one isolated process."""
    try:
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
        if hasattr(os, "setsid"):
            os.setsid()
        backend = DoclingBackend(BackendOptions(**options))
        while True:
            task = connection.recv()
            if task is None:
                return
            set_worker_policy(task.get('availability'))
            try:
                with generated_image_budget(task["rendered"], task.get("image_byte_budget")) as image_limit:
                    result = backend.convert_range(task["path"], task["kind"], task["start"], task["end"], scratch=task["scratch"])
                    from .recovery import recover_range
                    result = recover_range(backend, task["path"], task["kind"], task["start"], task["end"], result,
                                           scratch=task["scratch"], rendered=task["rendered"])
                    variants = result.pop("variants", [])
                    result.setdefault("warnings", []).extend(str(error) for error in result.get("errors", []))
                    result["warnings"].extend(result.get("recognition_warnings", []))
                    # Empty/malformed returns still account for every independently known unit.
                    if task["kind"] == "txt" and task["ids"]:
                        result["units"] = [row for row in result.get("units", []) if row["unit_id"] in task["ids"]]
                    known = {row["unit_id"] for row in result.get("units", [])}
                    expected = set(task["ids"])
                    if known - expected and not task.get("allow_discovery"):
                        raise IntegrityError("Backend/recovery changed original-unit identity")
                    result.setdefault("units", []).extend(_failure([uid for uid in task["ids"] if uid not in known], "No conversion result for original unit")["units"])
                    recovery_files = []
                    scratch_root = Path(result.get("recovery_scratch", task["scratch"]))
                    for artifact in result.get("recovery_artifacts", []):
                        original = _safe(scratch_root, artifact["path"])
                        _verify(scratch_root, [artifact])
                        archive_path = "recovery/" + artifact["path"] + (".bytes" if artifact.get("role") == "native recovery document" else "")
                        target = _safe(task["directory"], archive_path)
                        atomic_copy(target, original)
                        recovery_files.append({**artifact, "path": archive_path})
                    state_path = scratch_root / "recovery-state.json"
                    if state_path.is_file():
                        atomic_copy(_safe(task["directory"], "recovery/recovery-state.json"), state_path)
                    preferred_render = copy.deepcopy(task["rendered"])
                    for artifact in recovery_files:
                        if artifact.get("role") != "Poppler individual-page raster":
                            continue
                        mapping = artifact.get("original_unit_mapping", [])
                        if len(mapping) != 1:
                            continue
                        uid = mapping[0]
                        prior = next((row for row in preferred_render if row["unit_id"] == uid), None)
                        if prior is not None and prior.get("asset"):
                            continue
                        from PIL import Image
                        with Image.open(_safe(task["directory"], artifact["path"])) as pixels:
                            pixels.load()
                            rescued = {"unit_id": uid, "decode_status": "ok", "asset": artifact["path"], "width": pixels.width, "height": pixels.height,
                                       "dpi": pixels.info.get("dpi"), "transform": artifact.get("transform", {}), "errors": [], "original_render_gap": prior}
                        if prior is not None:
                            preferred_render.remove(prior)
                        preferred_render.append(rescued)
                    result.setdefault("provenance", {})["generated_image_budget"] = image_limit
                    result["provenance"]["recovery_artifacts"] = recovery_files
                    result["provenance"]["recovery_attempts"] = result.get("recovery_attempts", [])
                    manifest = export_part(result, task["directory"], source=task["source"], rendered=preferred_render)
                    variant_records = []
                    for i, variant in enumerate(variants, 1):
                        # A variant references independent render checkpoints in its parent.
                        value = export_part(variant, Path(task["directory"]) / "variants" / f"{i:04d}", source=task["source"])
                        variant_records.append({"relative_path": f"variants/{i:04d}", "role": variant.get("variant_role", variant.get("role", "candidate")), "manifest": value})
                    manifest["variants"] = variant_records
                    # Include exports of all candidate readings in the immutable pin set.
                    manifest["files"] = _records(task["directory"])
                    connection.send({"ok": True, "manifest": manifest})
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                connection.send({"ok": False, "fatal": isinstance(exc, IntegrityError) or isinstance(exc, OSError),
                                 "errno": getattr(exc, "errno", None), "error": f"{type(exc).__name__}: {exc}"})
    finally:
        connection.close()


class _Worker:
    def __init__(self, options):
        self.options, self.process, self.connection = options, None, None
        self.restarts = 0

    def _start(self):
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        self.process = ctx.Process(target=_worker_main, args=(child, self.options), daemon=False)
        self.process.start()
        child.close()
        self.connection = parent

    def close(self, *, graceful=True):
        if self.process is not None:
            if graceful and self.process.is_alive():
                with contextlib.suppress(BrokenPipeError, EOFError, OSError):
                    self.connection.send(None)
                self.process.join(timeout=2)
            if self.process.is_alive():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.kill()
                self.process.join()
            self.connection.close()
            self.process = self.connection = None

    def request(self, task):
        # A crashed in-flight attempt may be repeated once, in a fresh directory.
        task = copy.deepcopy(task)
        while True:
            if self.process is None:
                self._start()
            try:
                self.connection.send(task)
                if not self.connection.poll(WORKER_WATCHDOG):
                    self.close(graceful=False)
                    response = {"ok": False, "error": f"External worker watchdog exceeded {WORKER_WATCHDOG} seconds"}
                else:
                    response = self.connection.recv()
                if "directory" in task:
                    response["directory"] = task["directory"]
                return response
            except (EOFError, BrokenPipeError, ConnectionResetError):
                self.close(graceful=False)
                if self.restarts >= WORKER_CRASH_RESTARTS:
                    return {"ok": False, "error": "Worker crashed; automatic restart budget exhausted", "directory": task["directory"]}
                self.restarts += 1
                # Export may have been interrupted. Keep its bytes, use a fresh attempt.
                previous = Path(task["directory"])
                fresh = previous.parent / (previous.name + "-restart-" + uuid.uuid4().hex)
                fresh.mkdir(parents=True)
                if (previous / "assets-rendered").exists():
                    shutil.copytree(previous / "assets-rendered", fresh / "assets-rendered", copy_function=clone_copy2)
                task["directory"] = str(fresh)
                task["scratch"] = str(fresh.parent / (fresh.name + "-scratch"))


def _publish(root, work, receipt, intent):
    """Replay prepared bytes, accepting identical already-published names only."""
    source_dir = _safe(work, intent["prepared"])
    _verify(source_dir, intent["files"], exact=True)
    prior = {r["path"]: r for r in intent.get("previous", [])}
    for row in intent["files"]:
        target = _safe(root, str(PurePosixPath(intent.get("prefix", "")) / row["path"]))
        source = _safe(source_dir, row["path"])
        if target.exists():
            candidates=[row]+([prior[row["path"]]] if row["path"] in prior else [])
            actual = {"path":row["path"],**verify_pinned(target,candidates)}
            if actual == row:
                continue
            if actual != prior.get(row["path"]):
                raise IntegrityError(f"Publication would overwrite changed/untracked output: {target}")
            # Preserve previously accepted view bytes before guarded view refresh.
            history = _safe(root, ".history/" + actual["sha256"] + "/" + row["path"] + ".bytes")
            if not history.exists():
                atomic_copy(history, target)
            atomic(target, source.read_bytes())
        else:
            atomic_copy(target, source)
        _sync_directory(target.parent)
        if intent is receipt.get("views_intent"):
            current = {r["path"]: r for r in receipt.get("files", [])}
            current[row["path"]] = row
            receipt["files"] = list(current.values())
            _save(root, receipt)
        _checkpoint_hook("publication_file", receipt)
    _checkpoint_hook("published", receipt)


def _ranges(inventory, kind):
    ids = [row["unit_id"] for row in inventory["units"]]
    if kind in ("txt", "docx") or not ids:
        return [(1, 1, ids)]
    ranges = []
    for i in range(0, len(ids), RANGE_SIZE):
        chunk = ids[i:i + RANGE_SIZE]
        if chunk != list(range(chunk[0], chunk[-1] + 1)):
            raise IntegrityError("Physical units must retain contiguous original numbering")
        ranges.append((chunk[0], chunk[-1], chunk))
    return ranges


def _validate_plan(plan):
    if plan.get("kind") != "docling-folder" or plan.get("schema") != 1:
        raise IntegrityError("Expected frozen docling-folder plan schema 1")
    root, output = Path(plan["source_root"]).absolute(), Path(plan["output"]).absolute()
    for value, path in ((plan["source_root"], root), (plan["output"], output)):
        if str(path) != str(value) or str(path) != os.path.normpath(str(path)):
            raise IntegrityError("Conversion roots must be absolute normalized paths")
        _safe(path.parent, path.name)
    if root.resolve().is_relative_to(output.resolve()) or output.resolve().is_relative_to(root.resolve()):
        raise IntegrityError("Original and output trees overlap")
    seen, orders = set(), set()
    for entry in plan["sources"]:
        _safe(root, entry["relative_path"])
        if entry["relative_path"] in seen or entry["order"] in orders or type(entry["order"]) is not int or entry["order"] < 1:
            raise IntegrityError("Duplicate source or invalid frozen order")
        seen.add(entry["relative_path"])
        orders.add(entry["order"])
    return root, output


@contextlib.contextmanager
def _lock(output):
    output.mkdir(parents=True, exist_ok=True)
    path = _safe(output, ".conversion.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntegrityError("This output already has an active writer") from exc
        yield


def _accepted_files(receipt):
    records = list(receipt.get("files", []))
    for entry in receipt["sources"]:
        if entry.get("original_file"):
            records.append(entry["original_file"])
        for row in entry.get("files", []):
            records.append({**row, "path": entry["item_relative"] + "/" + row["path"]})
        for part in entry.get("parts", []):
            for row in part["manifest"]["files"]:
                records.append({**row, "path": entry["item_relative"] + "/" + part["relative_path"] + "/" + row["path"]})
        for version in entry.get("previous_versions", []):
            for row in version["files"]:
                records.append({**row, "path": version["item_relative"] + "/" + row["path"]})
    unique = {r["path"]: r for r in records}
    return list(unique.values())


def _verify_resume(output, receipt):
    pending_views = receipt.get("views_intent")
    alternatives = {r["path"]: r for r in pending_views["files"]} if pending_views and receipt.get("view_signature") != pending_views.get("signature") else {}
    retained = set()
    for entry in receipt["sources"]:
        if entry.get("retained_from") and entry["state"] == "pending":
            previous = next(v for v in entry["previous_versions"] if v["item_relative"] == entry["retained_from"])
            old_parts = {p["relative_path"] for p in previous["parts"]}
            for part in entry["parts"]:
                if part["relative_path"] in old_parts:
                    retained.update(entry["item_relative"] + "/" + part["relative_path"] + "/" + r["path"] for r in part["manifest"]["files"])
    for row in _accepted_files(receipt):
        path = _safe(output, row["path"])
        if not path.exists() and row["path"] in retained:
            continue  # A pinned retry copy intent is replayed after input validation.
        alternative = alternatives.get(row["path"])
        if alternative and path.is_file() and verify_pinned(path,[alternative,row])["sha256"] == alternative["sha256"]:
            continue
        _verify(output, [row])


def _verify_input(source):
    path = Path(source["absolute_path"])
    verify_pinned(path,source)


def _replay_retained(output, receipt):
    for entry in receipt["sources"]:
        if not entry.get("retained_from") or entry["state"] != "pending":
            continue
        previous = next((v for v in entry.get("previous_versions", []) if v["item_relative"] == entry["retained_from"]), None)
        if previous is None:
            raise IntegrityError("Retry lacks preserved accepted version")
        _verify(_safe(output, previous["item_relative"]), previous["files"])
        previous_parts = {p["relative_path"] for p in previous["parts"]}
        for part in entry["parts"]:
            if part["relative_path"] not in previous_parts:
                continue
            for row in part["manifest"]["files"]:
                src = _safe(output, previous["item_relative"] + "/" + part["relative_path"] + "/" + row["path"])
                dst = _safe(output, entry["item_relative"] + "/" + part["relative_path"] + "/" + row["path"])
                if dst.exists():
                    _verify(dst.parent, [{**row, "path": dst.name}])
                else:
                    atomic_copy(dst, src)


def _check_untracked(output, receipt):
    known={}
    def remember(path,row):known.setdefault(path,[]).append(row)
    for row in _accepted_files(receipt):remember(row["path"],row)
    allowed = {r["path"] for r in _accepted_files(receipt)} | {"conversion.json", ".conversion.lock"}
    for entry in receipt["sources"]:
        if entry.get("copy_intent"):
            allowed.add("Originals/" + entry["source"]["relative_path"])
        intents = [c["intent"] for c in entry.get("ranges", []) if c.get("intent")]
        if entry.get("item_intent"):
            intents.append(entry["item_intent"])
        for intent in intents:
            for row in intent["files"]:
                name = str(PurePosixPath(intent.get("prefix", "")) / row["path"])
                allowed.add(name)
                remember(name,row)
                path = _safe(output, name)
                if path.exists():
                    _verify(output, [{**row, "path": name}])
    root_intent = receipt.get("views_intent")
    if root_intent:
        previous = {r["path"]: r for r in root_intent.get("previous", [])}
        for row in root_intent["files"]:
            allowed.add(row["path"])
            remember(row["path"],row)
            if row["path"] in previous:remember(row["path"],previous[row["path"]])
            path = _safe(output, row["path"])
            if path.exists():
                actual = {"path":row["path"],**verify_pinned(path,known[row["path"]])}
                if actual != row and actual != previous.get(row["path"]):
                    raise IntegrityError("Root view differs from both accepted and prepared identity")
    for row in _records(output,known):
        if row["path"] in allowed:
            continue
        parts = PurePosixPath(row["path"]).parts
        # Root-view history identifies its immutable bytes in its directory name.
        if len(parts) >= 3 and parts[0] == ".history" and parts[1] == row["sha256"]:
            continue
        raise IntegrityError("Untracked conversion output requires inspection: " + row["path"])


def _reserve_intake_storage(output, receipt, *, fetch_inputs=False, retry_failed=False):
    """Reject an insufficient frozen-metadata lower bound before any fetch.

    No input stat, payload read, counting, rendering or availability probe runs
    here. Availability is only the last recorded flag; unknown sizes stay unknown.
    The detailed dimension-based budget follows verified intake separately.
    """
    originals = materialization = 0
    unknown = []
    for entry in receipt["sources"]:
        if entry["state"] == "unavailable" and not retry_failed:
            continue
        source = entry["source"]
        size = source.get("size")
        if size is None:
            unknown.append(source["relative_path"])
            continue
        if not entry.get("original_file"):
            originals += size
        flags = source.get("st_flags", 0)
        dataless = source.get("locally_available") is False or source.get("dataless") is True or (type(flags) is int and bool(flags & 0x40000000))
        if fetch_inputs and dataless:
            materialization += size
    required = originals + materialization + _STORAGE_METADATA_BYTES
    free = shutil.disk_usage(output).free
    receipt["intake_storage_lower_bound"] = {"schema": 1, "checked": now(), "remaining_original_bytes": originals,
        "recorded_dataless_materialization_bytes": materialization, "receipt_metadata_reserve_bytes": _STORAGE_METADATA_BYTES,
        "required_free_bytes": required, "available_free_bytes": free, "sufficient": free >= required, "unknown_size_sources": unknown,
        "limitation": "Frozen/last-recorded metadata only; no input reads or availability probes. Unknown sizes and later eviction are unbudgeted here. Page assets and repairs are checked after intake by storage_estimate."}
    _save(output, receipt)
    if free < required:
        raise OSError(errno.ENOSPC, f"Insufficient free storage before source intake: frozen lower bound {required} bytes; available {free}; see conversion.json intake_storage_lower_bound")


def _pixel_bytes(width, height):
    width, height = math.ceil(width), math.ceil(height)
    if width < 1 or height < 1:
        raise ValueError("Invalid raster dimensions")
    # Four channels, one PNG filter byte per row, chunk/header allowance.
    return width * height * 4 + height + 65536


_TXT_STORAGE_SCHEMA = 1
_TXT_LINE_RESERVE = 128 * 1024
_TXT_TEXT_COPIES = 64


def _text_storage_profile(source, inventory):
    """Budget retained per-line files and escaped text, without promoting text.

    Successful decoding uses the canonical BOM-aware strict decoder. Failed
    decoding keeps the existing conversion/salvage path and a byte-based upper
    allowance, never a guessed encoding or invented successful line inventory.
    """
    from .docling_backend import _read_text
    path = Path(source["absolute_path"])
    before = path.stat()
    detail = {"schema": _TXT_STORAGE_SCHEMA, "line_reserve_bytes": _TXT_LINE_RESERVE,
              "text_copy_allowance": _TXT_TEXT_COPIES,
              "limitation": "Conservative retained file/metadata estimate, not a guaranteed output or physical-space bound."}
    try:
        text, encoding = _read_text(path)
    except UnicodeError as exc:
        count = source["size"] + 1
        escaped = 6 * source["size"] + 2 * count
        detail.update(line_count=None, line_count_upper_allowance=count,
                      json_escaped_text_bytes=None, escaped_text_byte_allowance=escaped,
                      decode_status="strict decode failed; conversion/salvage unchanged",
                      decode_error=f"{type(exc).__name__}: {exc}", encoding=None)
    else:
        lines = text.splitlines(keepends=True)
        count = len(lines)
        expected = inventory.get("expected_count")
        if expected is not None and expected != count:
            raise IntegrityError("Text line inventory changed during storage estimation")
        escaped = sum(len(json.dumps(line, ensure_ascii=False).encode("utf-8")) for line in lines)
        detail.update(line_count=count, line_count_upper_allowance=count,
                      json_escaped_text_bytes=escaped, escaped_text_byte_allowance=escaped,
                      decode_status="canonical strict decode", encoding=encoding)
    after = path.stat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or after.st_size != source["size"] or checksum(path) != source["sha256"]):
        raise IntegrityError("Text source changed during storage estimation")
    reserve = _STORAGE_METADATA_BYTES + count * _TXT_LINE_RESERVE + _TXT_TEXT_COPIES * max(source["size"], escaped)
    detail["metadata_reserve_bytes"] = reserve
    return detail


def _storage_profile(source, inventory, options):
    """Read dimensions/package metadata and exact TXT text; never render or infer.

    A missing count or dimension remains explicitly unknown. Its reserve is a
    bounded next-range allowance, never an assertion about an unseen suffix.
    """
    kind, path = source["kind"], Path(source["absolute_path"])
    rows, warnings = [], []
    ids = [row["unit_id"] for row in inventory["units"]]
    metadata_bytes = max(_STORAGE_METADATA_BYTES, source["size"] * 8)
    text_storage = None
    if kind == "txt":
        text_storage = _text_storage_profile(source, inventory)
        metadata_bytes = max(metadata_bytes, text_storage["metadata_reserve_bytes"])
    if kind == "pdf":
        try:
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(path) as document:
                for uid in ids:
                    page = document[uid - 1]
                    try:
                        width, height = page.get_size()
                        rows.append({"unit_id": uid, "bytes": _pixel_bytes(width * options.images_scale, height * options.images_scale), "method": "PDF physical dimensions at requested image scale; RGBA/PNG envelope"})
                    finally:
                        page.close()
        except Exception as exc:
            _fatal(exc)
            warnings.append(f"Page dimension estimate incomplete: {type(exc).__name__}: {exc}")
    elif kind in ("image", "tiff"):
        try:
            from PIL import Image
            with Image.open(path) as image:
                for uid in ids:
                    try:
                        image.seek(uid - 1)
                        width, height = image.size
                        raw_dpi = image.info.get("dpi", (72, 72))
                        try:
                            dpi = [float(v) for v in raw_dpi]
                            if len(dpi) != 2 or any(not math.isfinite(v) or v <= 0 for v in dpi) or dpi == [1, 1]:
                                raise ValueError("invalid/fallback DPI")
                        except (TypeError, ValueError):
                            dpi = [72, 72]
                        # Independent renders retain original pixels; native
                        # Docling page exports may enlarge low-DPI sources.
                        scale = options.raster_images_scale if options.raster_images_scale is not None else options.images_scale
                        factor = max(1., 72 * scale / min(dpi))
                        envelope = _pixel_bytes(width * factor, height * factor)
                        if options.raster_images_scale is not None:
                            # At scale 1 the original pixels dominate; reserve
                            # either orientation's PNG row overhead as well.
                            envelope = max(envelope, _pixel_bytes(height * factor, width * factor))
                        rows.append({"unit_id": uid, "bytes": envelope, "method": "Frame dimensions/DPI; maximum of original and native scaled RGBA/PNG envelope"})
                    except Exception as exc:
                        _fatal(exc)
                        warnings.append(f"Frame {uid} dimension estimate unavailable: {type(exc).__name__}: {exc}")
        except Exception as exc:
            _fatal(exc)
            warnings.append(f"Image dimension estimate incomplete: {type(exc).__name__}: {exc}")
    elif kind == "docx":
        try:
            import zipfile
            with zipfile.ZipFile(path) as package:
                metadata_bytes = max(metadata_bytes, sum(row.file_size for row in package.infolist()) * 8)
        except Exception as exc:
            _fatal(exc)
            warnings.append(f"Package size estimate incomplete: {type(exc).__name__}: {exc}")
    known = {row["unit_id"] for row in rows}
    fallback = max([source["size"] * 4, _STORAGE_METADATA_BYTES] + [row["bytes"] for row in rows])
    if kind in ("pdf", "image", "tiff"):
        rows.extend({"unit_id": uid, "bytes": fallback, "method": "Unknown dimensions: source-size/known-unit reserve, not an upper bound"} for uid in ids if uid not in known)
    return {"source_sha256": source["sha256"], "images_scale": options.images_scale, "units": rows,
            **({"raster_images_scale": options.raster_images_scale} if options.raster_images_scale is not None else {}),
            **({"text_storage": text_storage} if text_storage is not None else {}),
            "metadata_bytes": metadata_bytes, "unknown_count": inventory.get("expected_count") is None,
            "unknown_dimensions": bool(set(ids) - known) if kind in ("pdf", "image", "tiff") else False,
            "unknown_range_unit_bytes": fallback, "warnings": warnings}


def _reserve_storage(output, receipt, options, *, retry_failed=False):
    """Checkpoint a conservative remaining-disk estimate before any publication.

    Actual ENOSPC remains fatal: compression, crops, variants and unknown units
    mean that this planning estimate cannot promise the final byte count.
    """
    originals = assets = prepared = metadata = retry_copies = 0
    range_bytes = repair_source = 0
    unknown, profiles = [], []
    signature = _fingerprint([(e["state"], e.get("item_relative"), e.get("files", [])) for e in receipt["sources"]])
    needs_views = receipt.get("view_signature") != signature
    retained_text_views = 0
    view_intent = receipt.get("views_intent")
    pending_view_publication = 0
    if view_intent and receipt.get("view_signature") != view_intent.get("signature"):
        pending_view_publication = sum(row["size"] for row in view_intent["files"])
        pending_view_publication += sum(row["size"] for row in view_intent.get("previous", []))
    for entry in receipt["sources"]:
        if entry["state"] == "unavailable":
            continue
        if entry["state"] == "complete" or entry["state"] == "partial" and not retry_failed:
            if needs_views and entry["source"]["kind"] == "txt":
                # An interruption can leave all sources accepted before root
                # views exist. Budget regeneration without changing complete
                # profiles or their historical estimate evidence.
                inventory = entry.get("inventory")
                if inventory is None:
                    inventory = inspect_source(entry["source"]["absolute_path"], "txt")
                retained_text_views += _text_storage_profile(entry["source"], inventory)["metadata_reserve_bytes"]
            continue
        source = entry["source"]
        if "inventory" not in entry:
            entry["inventory"] = inspect_source(source["absolute_path"], source["kind"])
        profile = entry.get("storage_profile")
        if profile is None:
            profile = entry["storage_profile"] = _storage_profile(source, entry["inventory"], options)
        if profile["source_sha256"] != source["sha256"] or profile["images_scale"] != options.images_scale or profile.get("raster_images_scale") != options.raster_images_scale:
            raise IntegrityError("Storage estimate differs from pinned source/settings")
        if source["kind"] == "txt" and profile.get("text_storage", {}).get("schema") != _TXT_STORAGE_SCHEMA:
            upgraded = _storage_profile(source, entry["inventory"], options)
            upgraded["metadata_bytes"] = max(profile["metadata_bytes"], upgraded["metadata_bytes"])
            entry.setdefault("storage_profile_history", []).append(copy.deepcopy(profile))
            profile = entry["storage_profile"] = upgraded
        profiles.append(source["relative_path"])
        metadata += profile["metadata_bytes"]
        if not entry.get("original_file"):
            originals += source["size"]
        completed = {row["unit_id"] for part in entry.get("parts", []) for row in part["manifest"].get("units", [])}
        prepared_ids = set()
        for chunk in entry.get("ranges", []):
            if chunk.get("intent") and chunk["state"] != "accepted":
                prepared_ids.update(chunk["ids"])
                prepared += sum(r["size"] for r in chunk["intent"]["files"])
        if entry.get("retained_from") and entry["state"] == "pending":
            for part in entry.get("parts", []):
                retry_copies += sum(row["size"] for row in part["manifest"]["files"] if not _safe(output, entry["item_relative"] + "/" + part["relative_path"] + "/" + row["path"]).exists())
        if entry["state"] == "partial" and retry_failed:
            retry_copies += sum(row["size"] for row in entry.get("files", []))
            completed = {row["unit_id"] for row in entry.get("map", {}).get("units", []) if row.get("conversion_status") == "ok" and (entry["inventory"]["unit_kind"] not in ("page", "frame") or row.get("render", {}).get("asset"))}
        pending = [row["bytes"] for row in profile["units"] if row["unit_id"] not in completed | prepared_ids]
        assets += sum(pending) * _STORAGE_ASSET_COPIES
        next_range = sum(sorted(pending, reverse=True)[:RANGE_SIZE])
        if profile["unknown_count"] or profile["unknown_dimensions"]:
            unknown.append(source["relative_path"])
            next_range = max(next_range, RANGE_SIZE * profile["unknown_range_unit_bytes"])
        range_bytes = max(range_bytes, next_range)
        # Rewrites/salvage can retain a source-sized derivative as scratch.
        repair_source = max(repair_source, source["size"] * 2)
    inflight = range_bytes * 2
    repair = range_bytes * _STORAGE_REPAIR_COPIES + repair_source
    required = originals + assets + prepared + metadata + retry_copies + inflight + repair + _STORAGE_METADATA_BYTES + retained_text_views + pending_view_publication
    free = shutil.disk_usage(output).free
    estimate = {"schema": 1, "checked": now(), "remaining_original_bytes": originals, "remaining_page_asset_bytes": assets,
                "prepared_publication_bytes": prepared, "metadata_and_views_bytes": metadata + _STORAGE_METADATA_BYTES,
                "retained_text_view_reserve_bytes": retained_text_views, "pending_view_publication_bytes": pending_view_publication,
                "retry_copy_bytes": retry_copies, "in_flight_range_bytes": inflight, "repair_reserve_bytes": repair,
                "required_free_bytes": required, "available_free_bytes": free, "sufficient": free >= required,
                "range_size": RANGE_SIZE, "asset_copy_allowance": _STORAGE_ASSET_COPIES, "repair_copy_allowance": _STORAGE_REPAIR_COPIES,
                "estimated_sources": profiles, "unknown_count_or_dimensions": unknown,
                "limitation": "Conservative metadata-based estimate, not a guaranteed output-size bound. Unknown counts/dimensions reserve one bounded next range, not an unseen suffix. Rechecked before each range; actual storage/publication failures stop."}
    receipt["storage_estimate"] = estimate
    _save(output, receipt)
    if free < required:
        raise OSError(errno.ENOSPC, f"Insufficient free storage for remaining originals, page assets and one in-flight repair range: need estimated {required} bytes; available {free}; see conversion.json storage_estimate")
    policy=current_policy()
    if policy is not None:policy['reserve_bytes']=required
    return estimate


def _source_pin(source):
    return {k: source.get(k) for k in ("relative_path", "sha256", "size", "mtime_ns", "kind", "reuse")}


_TEXT_RUNTIME = {"ready": True, "versions": {"text": "strict-v1"}, "models": []}


def _has_runtime_work(entry):
    return any(entry.get(key) for key in ("inventory", "storage_profile", "original_file", "ranges",
                                         "parts", "files", "item_relative", "previous_versions"))


def _pin_runtime(receipt, options, active):
    """Bind requirements monotonically; unavailable inputs do not select an engine."""
    old = receipt.get("runtime")
    if not active and old is None:
        return
    levels = {"text": 0, "package": 1, "ocr": 2}
    kinds = {entry["source"]["kind"] for entry in active}
    needed = 2 if kinds - {"txt", "docx", "unknown"} else 1 if "docx" in kinds else 0
    if old is None:
        previous = None
    elif old == _TEXT_RUNTIME:
        previous = 0
        if any(entry["source"]["kind"] not in {"txt", "unknown"} and _has_runtime_work(entry)
               for entry in receipt["sources"]):
            raise IntegrityError("Text-only runtime conflicts with modeled conversion work")
    else:
        previous = levels.get(receipt.get("runtime_requirement"))
        if previous is None:
            previous = 2 if old.get("models") or any(
                entry["source"]["kind"] not in {"txt", "docx", "unknown"} and _has_runtime_work(entry)
                for entry in receipt["sources"]) else 1
    target = max(needed, previous or 0)

    def identity(level):
        value = preflight(options, require_models=level == 2) if level else copy.deepcopy(_TEXT_RUNTIME)
        if not value["ready"]:
            raise RuntimeError("Conversion preflight failed: " + "; ".join(value["errors"]))
        return value

    # Check the already used engine before acquiring an additional requirement.
    current = identity(previous) if previous is not None else None
    if old is not None and old != current:
        raise IntegrityError("Conversion runtime/model identity changed")
    new = current if previous == target else identity(target)
    if old is not None and previous != target:
        receipt.setdefault("runtime_history", []).append({"identity": old,
            "requirement": next(k for k, v in levels.items() if v == previous),
            "upgraded_to": next(k for k, v in levels.items() if v == target), "at": now()})
    receipt["runtime"] = new
    receipt["runtime_requirement"] = next(k for k, v in levels.items() if v == target)


def _convert_folder_impl(plan, *, options=None, fetch_inputs=False, retry_failed=False, use_pinned_notes=False):
    """Execute exactly a frozen source set; return verified completion or gaps.

    Integrity, ownership, storage and publication errors raise, preserving state.
    Ordinary unavailable sources and exhausted recognition budgets remain gaps.
    """
    plan = copy.deepcopy(plan)
    root, output = _validate_plan(plan)
    opts = options if isinstance(options, BackendOptions) else BackendOptions(**(options or {}))
    settings = opts.frozen()
    with _lock(output):
        work = output.with_name(output.name + ".conversion-work")
        _safe(work.parent, work.name)
        work.mkdir(parents=True, exist_ok=True)
        ledger = output / "conversion.json"
        specification = {"source_root": str(root), "sources": plan["sources"], "selection": plan.get("selection", {}), "options": settings,
                         "range_size": RANGE_SIZE, "watchdog": WORKER_WATCHDOG, "crash_restarts": WORKER_CRASH_RESTARTS}
        if ledger.exists():
            receipt = json.loads(ledger.read_text())
            if receipt.get("kind") != "docling-folder" or receipt.get("schema") != 1 or receipt.get("specification") != specification:
                raise IntegrityError("Frozen source selection/settings differ from existing conversion")
            if len(receipt.get("sources", [])) != len(plan["sources"]) or any(e.get("source", {}).get("relative_path") != frozen["relative_path"] for e, frozen in zip(receipt["sources"], plan["sources"])):
                raise IntegrityError("Receipt source set differs from frozen specification")
            policy=current_policy()
            if policy is not None:policy['reserve_bytes']=receipt.get('storage_estimate',{}).get('required_free_bytes')
            _verify_resume(output, receipt)
            _check_untracked(output, receipt)
        else:
            if any(p.name != ".conversion.lock" for p in output.iterdir()):
                raise IntegrityError("Fresh output contains untracked files")
            if any(work.iterdir()):
                raise IntegrityError("Orphaned work directory requires inspection")
            receipt = {"kind": "docling-folder", "schema": 1, "created": now(), "state": "pending", "specification": specification,
                       "sources": [{"source": e, "state": "pending", "parts": []} for e in plan["sources"]], "files": [], "publication_complete": False}
            _save(output, receipt)
        _reserve_intake_storage(output, receipt, fetch_inputs=fetch_inputs, retry_failed=retry_failed)
        # Pin all original and retained-reading identities before replaying publication.
        for entry, frozen in zip(receipt["sources"], plan["sources"]):
            if entry["state"] == "unavailable" and not retry_failed:
                continue
            try:
                snapshot = entry["source"].get("reuse") if use_pinned_notes and entry.get("intake_pinned") else None
                prepared = prepare_source(root, frozen, fetch_inputs=fetch_inputs, notes_snapshot=snapshot)
            except (OSError, RuntimeError) as exc:
                _fatal(exc)
                if entry.get("intake_pinned") or entry.get("source", {}).get("hash_status") == "verified_intake":
                    raise IntegrityError("Previously pinned source is unavailable") from exc
                entry.update(state="unavailable", error=str(exc))
                _save(output, receipt)
                continue
            if (entry.get("intake_pinned") or entry["source"].get("hash_status") == "verified_intake") and _source_pin(entry["source"]) != _source_pin(prepared):
                raise IntegrityError("Original or retained reading changed since checkpoint")
            entry["source"] = prepared
            entry["intake_pinned"] = True
            if entry["state"] == "unavailable":
                entry["state"] = "pending"
            _save(output, receipt)
        active = [e for e in receipt["sources"] if e["state"] != "unavailable"]
        _pin_runtime(receipt, opts, active)
        _save(output, receipt)
        _reserve_storage(output, receipt, opts, retry_failed=retry_failed)
        # Publication replay follows current source/options/model validation.
        _replay_retained(output, receipt)
        pending_views = receipt.get("views_intent")
        if pending_views and receipt.get("view_signature") != pending_views.get("signature"):
            _publish(output, work, receipt, pending_views)
            receipt["files"] = pending_views["files"]
            receipt["view_signature"] = pending_views["signature"]
            _save(output, receipt)
        _verify(output, _accepted_files(receipt))
        worker = _Worker(settings)
        try:
            for entry in active:
                source = entry["source"]
                if entry["state"] in ("complete", "partial"):
                    if not retry_failed or entry["state"] == "complete":
                        continue
                    prior = {"item_relative": entry["item_relative"], "files": entry["files"], "map": entry["map"], "parts": entry["parts"]}
                    entry.setdefault("previous_versions", []).append(prior)
                    successful, failed = [], []
                    for part in prior["parts"]:
                        retained = copy.deepcopy(part)
                        retained["manifest"]["units"] = [r for r in retained["manifest"]["units"] if r.get("conversion_status") == "ok" and (entry["inventory"]["unit_kind"] not in ("page", "frame") or r.get("render", {}).get("asset"))]
                        successful.append(retained)
                        failed.extend(r["unit_id"] for r in part["manifest"]["units"] if r["unit_id"] not in {r["unit_id"] for r in retained["manifest"]["units"]})
                    # An unknown total may have no named failed units. No fabricated
                    # total is introduced: retry the structural sentinel once.
                    if source["kind"] in ("txt", "docx"):
                        retry_ranges = [{"start": 1, "end": 1, "ids": failed, "state": "pending"}] if failed or entry["inventory"]["expected_count"] is None else []
                    else:
                        retry_ranges = [{"start": uid, "end": uid, "ids": [uid], "state": "pending"} for uid in failed]
                    receipt.update(state="pending", publication_complete=False)
                    entry.update(state="pending", parts=successful, files=[], generation=entry.get("generation", 0) + 1,
                                 ranges=retry_ranges, retained_from=prior["item_relative"])
                    for key in ("item_intent", "map"):
                        entry.pop(key, None)
                generation = entry.get("generation", 0)
                slug = re.sub(r"[^\w.-]+", "-", Path(source["relative_path"]).name, flags=re.UNICODE)[:80] or "source"
                base = f"Items/{source['order']:06d}-{slug}"
                entry["item_relative"] = base if generation == 0 else base + f"-retry-{generation:04d}"
                _reserve_storage(output, receipt, opts)
                if entry.get("retained_from"):
                    _save(output, receipt)
                    _replay_retained(output, receipt)
                    _save(output, receipt)
                if not entry.get("original_file"):
                    destination = _safe(output, "Originals/" + source["relative_path"])
                    intent = entry.get("copy_intent")
                    if not intent:
                        intent = entry["copy_intent"] = {"sha256": source["sha256"], "size": source["size"]}
                        _save(output, receipt)
                    if destination.exists():
                        if verify_pinned(destination,intent)["sha256"] != intent["sha256"]:
                            raise IntegrityError("Copied original conflicts with prepared identity")
                    else:
                        if shutil.disk_usage(output).free < source["size"]:
                            raise OSError(errno.ENOSPC, "Insufficient space for byte-identical original copy")
                        atomic_copy(destination, source["absolute_path"])
                        os.utime(destination, ns=(Path(source["absolute_path"]).stat().st_atime_ns, source["mtime_ns"]))
                    if verify_pinned(destination,source)["sha256"] != source["sha256"] or verify_pinned(source["absolute_path"],source)["sha256"] != source["sha256"]:
                        raise IntegrityError("Original changed during copy publication")
                    os.utime(destination, ns=(destination.stat().st_atime_ns, source["mtime_ns"]))
                    entry["original_file"] = {"path": "Originals/" + source["relative_path"], **intent, "mtime_ns": source["mtime_ns"]}
                    _save(output, receipt)
                if "inventory" not in entry:
                    entry["inventory"] = inspect_source(_safe(output, "Originals/" + source["relative_path"]), source["kind"])
                    _save(output, receipt)
                if "ranges" not in entry:
                    entry["ranges"] = [{"start": a, "end": b, "ids": ids, "state": "pending"} for a, b, ids in _ranges(entry["inventory"], source["kind"])]
                    _save(output, receipt)
                for number, chunk in enumerate(entry["ranges"], 1):
                    if chunk["state"] == "accepted":
                        continue
                    _reserve_storage(output, receipt, opts)
                    checkpoint = f"{source['order']:06d}/g{generation:04d}/r{number:06d}"
                    if "render" not in chunk:
                        render_dir = _safe(work, checkpoint + "/render")
                        if render_dir.exists():
                            # Interrupted renderer has no accepted checkpoint; preserve it.
                            checkpoint += "-" + uuid.uuid4().hex
                            render_dir = _safe(work, checkpoint + "/render")
                        render_dir.mkdir(parents=True)
                        rendered = [] if source["kind"] == "unknown" else render_units(_safe(output, "Originals/" + source["relative_path"]), source["kind"], chunk["ids"], render_dir,
                                                rotate_clockwise=opts.rotate_clockwise, images_scale=opts.images_scale)
                        _durable(render_dir)
                        chunk["render"] = {"directory": render_dir.relative_to(work).as_posix(), "rows": rendered, "files": _records(render_dir)}
                        _save(output, receipt)
                        _checkpoint_hook("rendered", receipt)
                    render = chunk["render"]
                    _verify(_safe(work, render["directory"]), render["files"], exact=True)
                    if "intent" not in chunk:
                        attempt = _safe(work, checkpoint + "/attempt-" + uuid.uuid4().hex)
                        attempt.mkdir(parents=True)
                        shutil.copytree(_safe(work, render["directory"]), attempt / "assets-rendered", copy_function=clone_copy2)
                        rows = copy.deepcopy(render["rows"])
                        for row in rows:
                            if row.get("asset"):
                                row["asset"] = "assets-rendered/" + row["asset"]
                        task = {"path": str(_safe(output, "Originals/" + source["relative_path"])), "kind": source["kind"], "source": source, "start": chunk["start"], "end": chunk["end"],
                                "ids": chunk["ids"], "allow_discovery": entry["inventory"]["expected_count"] is None or source["kind"] == "docx", "directory": str(attempt), "scratch": str(attempt.parent / (attempt.name + "-scratch")), "rendered": rows,
                                "availability": current_policy(), "image_byte_budget": max((r["bytes"] for r in entry["storage_profile"]["units"] if r["unit_id"] in chunk["ids"]), default=entry["storage_profile"]["unknown_range_unit_bytes"])}
                        if source["kind"] == "unknown":
                            gap = _failure([], "Unsupported/context source format; no converter or models were invoked", "format")
                            gap["warnings"] = list(gap["errors"])
                            response = {"ok": True, "manifest": export_part(gap, attempt, source=source)}
                        else:
                            _checkpoint_hook("inference", receipt)
                            response = worker.request(task)
                        attempt = Path(response.get("directory", attempt))
                        if not response["ok"]:
                            if response.get("fatal"):
                                if response.get("errno"):
                                    raise OSError(response["errno"], response["error"])
                                raise IntegrityError(response["error"])
                            # Preserve interrupted exports; publish a separate terminal gap part.
                            attempt = attempt.parent / (attempt.name + "-failed")
                            attempt.mkdir()
                            shutil.copytree(_safe(work, render["directory"]), attempt / "assets-rendered", copy_function=clone_copy2)
                            manifest = export_part(_failure(chunk["ids"], response["error"]), attempt, source=source, rendered=rows)
                        else:
                            manifest = response["manifest"]
                        manifest_ids = {r["unit_id"] for r in manifest["units"]}
                        enumerated_ids = {r["unit_id"] for r in entry["inventory"]["units"]}
                        if source["kind"] == "docx" and manifest_ids - enumerated_ids:
                            entry["inventory"]["expected_count"] = None
                            entry["inventory"]["count_warnings"].append("Recovery exposed additional package members; full package coverage is unknown")
                        if entry["inventory"]["expected_count"] is None:
                            known_ids = {r["unit_id"] for r in entry["inventory"]["units"]}
                            entry["inventory"]["units"].extend({"unit_id": r["unit_id"]} for r in manifest["units"] if r["unit_id"] not in known_ids)
                        relative = f"parts/g{generation:04d}-{number:06d}-{chunk['start']:06d}-{chunk['end']:06d}"
                        _durable(attempt)
                        chunk["intent"] = {"prepared": attempt.relative_to(work).as_posix(), "files": _records(attempt),
                                           "prefix": entry["item_relative"] + "/" + relative, "manifest": manifest, "relative_path": relative}
                        chunk["state"] = "prepared"
                        _save(output, receipt)
                        _checkpoint_hook("prepared", receipt)
                    intent = chunk["intent"]
                    _verify_input(source)
                    _verify(output, [entry["original_file"]])
                    _publish(output, work, receipt, intent)
                    part = {"relative_path": intent["relative_path"], "manifest": intent["manifest"]}
                    if part not in entry["parts"]:
                        entry["parts"].append(part)
                    chunk["state"] = "accepted"
                    _save(output, receipt)
                    _checkpoint_hook("accepted", receipt)
                _verify_input(source)
                _verify(output, [entry["original_file"]])
                if "item_intent" not in entry:
                    staging = _safe(work, f"views-{uuid.uuid4().hex}")
                    item = _safe(staging, entry["item_relative"])
                    item.mkdir(parents=True)
                    original = _safe(staging, "Originals/" + source["relative_path"])
                    original.parent.mkdir(parents=True)
                    clone_copy2(_safe(output, "Originals/" + source["relative_path"]), original)
                    for part in entry["parts"]:
                        shutil.copytree(_safe(output, entry["item_relative"] + "/" + part["relative_path"]), _safe(item, part["relative_path"]), copy_function=clone_copy2)
                    item_map = write_item(staging, entry["item_relative"], source, entry["inventory"], entry["parts"], reuse=source.get("reuse"))
                    leaf = [r for r in item_map["files"] if "/" not in r["path"]]
                    leaf_stage = _safe(work, "item-publication-" + uuid.uuid4().hex)
                    leaf_stage.mkdir()
                    for row in leaf:
                        clone_copy2(item / row["path"], leaf_stage / row["path"])
                    _durable(leaf_stage)
                    entry["item_intent"] = {"prepared": leaf_stage.relative_to(work).as_posix(), "files": leaf, "prefix": entry["item_relative"], "map": item_map}
                    _save(output, receipt)
                _publish(output, work, receipt, entry["item_intent"])
                entry["map"] = entry["item_intent"]["map"]
                entry["files"] = entry["map"]["files"]
                entry["state"] = "complete" if entry["map"]["conversion_complete"] else "partial"
                _save(output, receipt)
            receipt["state"] = "complete" if all(e["state"] == "complete" for e in receipt["sources"]) else "partial"
            receipt["coverage_accounted"] = all(e.get("map", {}).get("coverage_accounted", False) for e in receipt["sources"])
            receipt["conversion_complete"] = receipt["state"] == "complete"
            receipt["publication_complete"] = all(e["state"] in ("complete", "partial", "unavailable") for e in receipt["sources"])
            signature = _fingerprint([(e["state"], e.get("item_relative"), e.get("files", [])) for e in receipt["sources"]])
            if receipt.get("view_signature") != signature:
                intent = receipt.get("views_intent")
                if intent is None or intent.get("signature") != signature:
                    stage = _safe(work, "bundle-views-" + uuid.uuid4().hex)
                    stage.mkdir()
                    # D reads source maps while constructing views; expose them here.
                    for entry in receipt["sources"]:
                        if entry.get("map"):
                            target = _safe(stage, entry["item_relative"] + "/source-map.json")
                            target.parent.mkdir(parents=True)
                            clone_copy2(_safe(output, entry["item_relative"] + "/source-map.json"), target)
                    views = write_bundle_views(stage, receipt)
                    leaf = _safe(work, "root-publication-" + uuid.uuid4().hex)
                    leaf.mkdir()
                    for row in views["files"]:
                        clone_copy2(stage / row["path"], leaf / row["path"])
                    _durable(leaf)
                    receipt.update(state="pending", publication_complete=False)
                    intent = receipt["views_intent"] = {"prepared": leaf.relative_to(work).as_posix(), "files": views["files"], "previous": receipt["files"], "signature": signature}
                    _save(output, receipt)
                _publish(output, work, receipt, intent)
                receipt["files"] = intent["files"]
                receipt["view_signature"] = signature
            receipt["state"] = "complete" if all(e["state"] == "complete" for e in receipt["sources"]) else "partial"
            receipt["publication_complete"] = all(e["state"] in ("complete", "partial", "unavailable") for e in receipt["sources"])
            receipt["finished"] = now()
            _save(output, receipt)
            report = verify_bundle(output)
            if not report["valid"]:
                raise IntegrityError("Published bundle failed verification: " + "; ".join(report["errors"]))
            return {**receipt, "output": str(output), "verification": report, "exit_code": report["exit_code"]}
        finally:
            worker.close()


def run_archive_plan(plan_or_path, *, options=None, fetch_inputs=False, retry_failed=False, use_pinned_notes=False):
    """Run ready frozen jobs in priority order, deriving progress from receipts."""
    plan = load_archive_plan(plan_or_path) if isinstance(plan_or_path, (str, Path)) else copy.deepcopy(plan_or_path)
    validate_archive_plan(plan)
    progress, completed = [], set()
    pending = list(plan["jobs"])
    while pending:
        job = next((candidate for candidate in pending if candidate["scope_state"] == "ready" and candidate["action"] != "defer" and all(p in completed for p in candidate.get("prerequisites", []))), None)
        if job is None:
            progress.extend({"id": candidate["id"], "state": "deferred", "unmet_prerequisites": [p for p in candidate.get("prerequisites", []) if p not in completed]} for candidate in pending)
            break
        pending.remove(job)
        prerequisites = job.get("prerequisites", [])
        unmet = [p for p in prerequisites if p not in completed]
        if job["scope_state"] != "ready" or job["action"] == "defer" or unmet:
            progress.append({"id": job["id"], "state": "deferred", "unmet_prerequisites": unmet})
            continue
        folder = {"kind": "docling-folder", "schema": 1, "source_root": job["source_root"], "output": job["output"],
                  "sources": job["sources"], "issues": job.get("issues", []), "exclusions": job.get("exclusions", [])}
        # Reuse an already adequate portable bundle only after current verification.
        bundles = {(s.get("reuse") or {}).get("portable_bundle") for s in job["sources"]}
        if job["action"] == "reuse" and len(bundles) == 1 and None not in bundles:
            bundle = bundles.pop()
            report = verify_bundle(bundle)
            if not report["valid"]:
                raise IntegrityError("Previously accepted portable bundle failed verification")
            if report["exit_code"] == 0:
                existing = json.loads((Path(bundle) / "conversion.json").read_text())
                selected = {s["relative_path"]: s for s in job["sources"]}
                actual = {e["source"]["relative_path"]: e["source"] for e in existing["sources"]}
                if set(selected) <= set(actual) and all(actual[name].get("sha256") == row.get("sha256") for name, row in selected.items()):
                    for row in job["sources"]:
                        prepare_source(job["source_root"], row, fetch_inputs=fetch_inputs)
                    progress.append({"id": job["id"], "state": "reused", "output": str(bundle), "exit_code": 0, "verification": report})
                    completed.add(job["id"])
                    continue
        result = convert_folder(folder, options=options, fetch_inputs=fetch_inputs, retry_failed=retry_failed,
                                use_pinned_notes=use_pinned_notes)
        # Never trust an in-memory worker summary as cross-job completion evidence.
        report = verify_bundle(job["output"])
        if not report["valid"]:
            raise IntegrityError("Archive job publication is inconsistent")
        progress.append({"id": job["id"], "state": result["state"], "output": job["output"], "exit_code": report["exit_code"], "verification": report})
        if report["publication_complete"]:
            completed.add(job["id"])
    return {"kind": "docling-archive-run", "schema": 1, "jobs": progress,
            "exit_code": 0 if all(p.get("exit_code") == 0 for p in progress) and not plan.get("unresolved") else 2,
            "progress_source": "verified conversion.json receipts; frozen plan never re-enumerated"}


def convert_folder(plan, *, options=None, fetch_inputs=False, retry_failed=False, use_pinned_notes=False):
    root,output=_validate_plan(plan)
    work=output.with_name(output.name+'.conversion-work')
    inputs=[]
    for source in plan['sources']:
        inputs.append(root/source['relative_path'])
        reuse=source.get('reuse') or {}
        inputs.extend(reuse[key] for key in ['text_path','receipt_path'] if key in reuse)
        inputs.extend(row['path'] for row in reuse.get('linked_text_pins',[]))
        if reuse.get('notes_source'):inputs.append(reuse['notes_source']['path'])
    with availability_scope(policy_for(output,work,inputs,fetch_inputs)):
        return _convert_folder_impl(plan,options=options,fetch_inputs=fetch_inputs,retry_failed=retry_failed,use_pinned_notes=use_pinned_notes)
