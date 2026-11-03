"""Bounded, offline Docling adapter and inference-independent source images.

Imports of Docling are deliberately lazy: plain text and inventory do not need
model initialization. Original unit numbers always survive range selection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import codecs
import contextlib
import errno
import hashlib
import importlib.metadata
import math
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile

PINNED_VERSIONS = {"docling": "2.142.1", "docling-core": "2.103.1", "docling-parse": "7.22.4"}
MAX_RANGE = 16


@contextlib.contextmanager
def generated_image_budget(rendered, reserved_bytes):
    """Bound internally generated native images by independently decoded pixels.

    Docling's default encoded-image limit is too small for some camera captures.
    Only the isolated worker uses this temporary budget; local/remote URI policy
    and Pillow pixel limits are unchanged. Existing storage checks precede it.
    """
    pixels = [row["width"] * row["height"] * 4 + row["height"] + 65536
              for row in rendered if row.get("decode_status") == "ok"
              and row.get("width", 0) > 0 and row.get("height", 0) > 0]
    if not pixels:
        yield {"effective_bytes": None, "basis": "no independently decoded raster"}
        return
    if not isinstance(reserved_bytes, int) or reserved_bytes < max(pixels):
        from .common import IntegrityError
        raise IntegrityError("Native image budget exceeds reserved raster envelope")
    from docling_core.utils.settings import settings
    previous = settings.max_image_decoded_size
    limit = max(previous, reserved_bytes)
    try:
        settings.max_image_decoded_size = limit
        yield {"default_bytes": previous, "effective_bytes": limit,
               "reserved_bytes": reserved_bytes,
               "basis": "DPI-aware per-unit storage envelope, checked against independent render pixels"}
    finally:
        settings.max_image_decoded_size = previous


@dataclass
class BackendOptions:
    artifacts_path: str | None = None
    threads: int = 4
    document_timeout: float = 300
    images_scale: float = 3
    rotate_clockwise: int = 0
    raster_images_scale: float | None = None

    def __post_init__(self):
        if self.threads < 1 or self.document_timeout <= 0 or self.images_scale <= 0:
            raise ValueError("threads, timeout and image scale must be positive")
        if self.raster_images_scale is not None and (isinstance(self.raster_images_scale, bool) or not math.isfinite(self.raster_images_scale) or self.raster_images_scale <= 0):
            raise ValueError("raster image scale must be finite and positive")
        if self.rotate_clockwise not in (0, 90, 180, 270):
            raise ValueError("rotate_clockwise must be 0, 90, 180 or 270")

    def frozen(self):
        """Keep omitted opt-in settings byte-compatible with earlier receipts."""
        settings = asdict(self)
        if self.raster_images_scale is None:
            settings.pop("raster_images_scale")
        return settings


def _fatal(exc):
    from .common import IntegrityError
    if isinstance(exc, IntegrityError):
        raise exc
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        raise exc


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def preflight(options, *, require_models=True):
    """Check the explicit local runtime without importing inference or fetching.

    ``require_models=False`` checks only the package runtime (for DOCX); TXT
    bypasses preflight entirely. Missing resources are returned as errors.
    """
    options = options if isinstance(options, BackendOptions) else BackendOptions(**options)
    errors, versions, models = [], {}, []
    for package, expected in PINNED_VERSIONS.items():
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
        if versions[package] != expected:
            errors.append(f"{package} must be {expected}; found {versions[package]}")
    try:
        versions["Pillow"] = importlib.metadata.version("Pillow")
        if int(versions["Pillow"].split(".")[0]) >= 13:
            errors.append("conversion environment requires Pillow <13")
    except importlib.metadata.PackageNotFoundError:
        errors.append("Pillow is unavailable")
    tess = None
    if require_models:
        if not options.artifacts_path:
            errors.append("explicit local artifacts_path is required; automatic downloads are disabled")
        else:
            root = Path(options.artifacts_path).resolve()
            layout = root / "docling-project--docling-layout-heron"
            table = root / "docling-project--docling-models/model_artifacts/tableformer/accurate"
            required = [layout / "config.json", layout / "preprocessor_config.json", table / "tm_config.json"]
            for directory, pattern in ((layout, "*.safetensors"), (table, "tableformer_*.safetensors")):
                weights = sorted(directory.glob(pattern))
                if not weights:
                    errors.append(f"missing local model weights: {directory / pattern}")
                required.extend(weights)
            for path in required:
                if not path.is_file() or path.stat().st_size == 0:
                    errors.append(f"missing or empty local model artifact: {path}")
                else:
                    models.append({"path": str(path.relative_to(root)), "size": path.stat().st_size, "sha256": _sha(path)})
        tess = shutil.which("tesseract")
        if tess is None:
            errors.append("Tesseract CLI is unavailable")
        else:
            try:
                proc = subprocess.run([tess, "--list-langs"], capture_output=True, text=True, timeout=30, check=True)
                language_output = proc.stdout + "\n" + proc.stderr
                languages = set(language_output.splitlines())
                directory_match = re.search(r'in "([^"]+)"', language_output)
                data_root = Path(directory_match.group(1)) if directory_match else None
                for language in ("eng", "osd"):
                    if language not in languages:
                        errors.append(f"Tesseract language {language} is unavailable")
                    elif data_root is None or not (data_root / f"{language}.traineddata").is_file():
                        errors.append(f"Tesseract language {language} has no inspectable local traineddata file")
                    else:
                        asset = data_root / f"{language}.traineddata"
                        models.append({"path": str(asset), "size": asset.stat().st_size, "sha256": _sha(asset)})
                proc = subprocess.run([tess, "--version"], capture_output=True, text=True, timeout=30, check=True)
                versions["tesseract"] = proc.stdout.splitlines()[0]
            except (OSError, subprocess.SubprocessError) as exc:
                _fatal(exc)
                errors.append(f"Tesseract preflight: {exc}")
    return {"ready": not errors, "errors": errors, "versions": versions, "models": models,
            "options": options.frozen(), "device": "cpu", "ocr": "tesseract-cli", "languages": ["eng"],
            "tesseract_cmd": tess, "network": "offline model flags; remote services/plugins disabled; OS network denial required for hardened execution", "queue_max_size": 1}


def _tiff_inventory(path):
    """Walk IFD links independently of pixel decoding, retaining known prefix."""
    units, warnings = [], []
    try:
        with Path(path).open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            header = f.read(16)
            if header[:2] not in (b"II", b"MM"):
                raise ValueError("invalid TIFF byte order")
            endian = "<" if header[:2] == b"II" else ">"
            magic = struct.unpack(endian + "H", header[2:4])[0]
            if magic == 42:
                offset, count_fmt, pointer_fmt, entry_size = struct.unpack(endian + "I", header[4:8])[0], "H", "I", 12
            elif magic == 43 and struct.unpack(endian + "HH", header[4:8]) == (8, 0):
                offset, count_fmt, pointer_fmt, entry_size = struct.unpack(endian + "Q", header[8:16])[0], "Q", "Q", 20
            else:
                raise ValueError("invalid TIFF directory header")
            seen = set()
            while offset:
                if offset in seen:
                    raise ValueError(f"cyclic TIFF directory chain at {offset}")
                seen.add(offset)
                nbytes, pbytes = struct.calcsize(count_fmt), struct.calcsize(pointer_fmt)
                if offset < 8 or offset + nbytes > size:
                    raise ValueError(f"unreadable TIFF directory at {offset}")
                f.seek(offset)
                entries = struct.unpack(endian + count_fmt, f.read(nbytes))[0]
                next_at = offset + nbytes + entries * entry_size
                if next_at + pbytes > size:
                    raise ValueError(f"truncated TIFF directory at {offset}")
                units.append({"unit_id": len(units) + 1, "ifd_offset": offset})
                f.seek(next_at)
                offset = struct.unpack(endian + pointer_fmt, f.read(pbytes))[0]
        if not units:
            raise ValueError("TIFF contains no readable frame directory")
        return len(units), units, warnings
    except (OSError, ValueError, struct.error) as exc:
        _fatal(exc)
        warnings.append(f"TIFF directory enumeration incomplete: {exc}")
        return None, units, warnings


def _read_text(path):
    raw = Path(path).read_bytes()
    encoding = "utf-8"
    # Test longest BOM first: UTF-32 LE also starts with the UTF-16 LE BOM.
    for bom, enc in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                     (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
                     (codecs.BOM_UTF8, "utf-8-sig")):
        if raw.startswith(bom):
            encoding = enc
            break
    return raw.decode(encoding, errors="strict"), encoding


def inspect_source(path, kind):
    path = Path(path)
    count, units, warnings = None, [], []
    unit_kind, method = "page", "unsupported"
    try:
        if kind == "tiff":
            unit_kind, method = "frame", "TIFF IFD chain (independent of pixel decode)"
            count, units, warnings = _tiff_inventory(path)
        elif kind == "pdf":
            method = "pdfium physical page count"
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(path) as document:
                count = len(document)
            units = [{"unit_id": n} for n in range(1, count + 1)]
        elif kind == "image":
            from PIL import Image
            method = "Pillow image frame enumeration"
            with Image.open(path) as image:
                count = getattr(image, "n_frames", 1)
            units = [{"unit_id": n} for n in range(1, count + 1)]
        elif kind == "txt":
            unit_kind, method = "line", "strict BOM/UTF-8 text line enumeration"
            text, encoding = _read_text(path)
            units = [{"unit_id": f"line:{n}"} for n, _ in enumerate(text.splitlines(keepends=True), 1)]
            count = len(units)
            return {"expected_count": count, "unit_kind": unit_kind, "units": units, "count_method": method,
                    "count_warnings": warnings, "encoding": encoding}
        elif kind == "docx":
            unit_kind, method = "package", "DOCX package (no physical pagination)"
            with zipfile.ZipFile(path) as package:
                if "word/document.xml" not in package.namelist():
                    raise ValueError("DOCX main document part is missing")
            count, units = 1, [{"unit_id": "package:word/document.xml"}]
    except Exception as exc:
        _fatal(exc)
        warnings.append(f"Cannot enumerate {kind}: {type(exc).__name__}: {exc}")
    return {"expected_count": count, "unit_kind": unit_kind, "units": units,
            "count_method": method, "count_warnings": warnings}


def _normalize_frame(path, unit_id, rotation):
    from PIL import Image, ImageOps
    with Image.open(path) as source:
        source.seek(unit_id - 1)
        orientation = source.getexif().get(274, 1)
        if isinstance(orientation, bool) or orientation not in range(1, 9):
            raise ValueError(f"invalid EXIF orientation: {orientation!r}")
        raw_size = source.size
        raw_dpi = source.info.get("dpi", (72, 72))
        warnings = []
        try:
            dpi = tuple(float(x) for x in raw_dpi)
            if len(dpi) != 2 or any(not math.isfinite(x) or x <= 0 for x in dpi):
                raise ValueError("invalid DPI")
            if dpi == (1, 1):
                dpi = (72., 72.)
        except (TypeError, ValueError):
            dpi = (72., 72.)
            warnings.append(f"invalid DPI {raw_dpi!r}; declared 72 DPI fallback")
        # Pillow's TIFF loader honors its own orientation during load and clears
        # the tag. exif_transpose therefore applies exactly the remaining tag.
        frame = ImageOps.exif_transpose(source)
        frame.load()
        if orientation in (5, 6, 7, 8):
            dpi = dpi[::-1]
        if frame.mode in ("RGBA", "LA") or "transparency" in frame.info:
            rgba = frame.convert("RGBA")
            canvas = Image.new("RGBA", rgba.size, "white")
            canvas.alpha_composite(rgba)
            frame = canvas.convert("RGB")
        else:
            frame = frame.convert("RGB")
        if rotation:
            frame = frame.rotate(-rotation, expand=True)
        if rotation in (90, 270):
            dpi = dpi[::-1]
        # Drop metadata after normalization; subsequent consumers must not apply
        # original orientation to the already upright pixels again.
        frame.info.clear()
        transform = {"exif_orientation": orientation, "exif_normalized": True,
                     "rotate_clockwise": rotation, "order": ["exif", "clockwise_rotation"],
                     "source_width": raw_size[0], "source_height": raw_size[1]}
        return frame, dpi, transform, warnings


def render_units(path, kind, unit_ids, destination, *, rotate_clockwise=0, images_scale=3):
    if rotate_clockwise not in (0, 90, 180, 270) or images_scale <= 0:
        raise ValueError("invalid rendering rotation or scale")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("render destination cannot be a symlink")
    destination = destination.resolve()
    rows = []
    for unit_id in unit_ids:
        row = {"unit_id": unit_id, "decode_status": "not_applicable" if kind in ("txt", "docx") else "failed",
               "asset": None, "width": None, "height": None, "dpi": None, "transform": {}, "errors": []}
        if kind in ("txt", "docx"):
            rows.append(row)
            continue
        if not isinstance(unit_id, int) or isinstance(unit_id, bool) or unit_id < 1:
            raise ValueError("image/page/frame unit IDs must be positive integers")
        try:
            if kind in ("image", "tiff"):
                image, dpi, transform, warnings = _normalize_frame(path, unit_id, rotate_clockwise)
            elif kind == "pdf":
                import pypdfium2 as pdfium
                # Reopen independently: one damaged page must not poison later
                # page handles. PDFium serializes native rendering in caller.
                with pdfium.PdfDocument(Path(path)) as document:
                    page = document[unit_id - 1]
                    try:
                        bitmap = page.render(scale=images_scale, rotation=rotate_clockwise)
                        try:
                            image = bitmap.to_pil().copy().convert("RGB")
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                dpi, warnings = (72 * images_scale, 72 * images_scale), []
                transform = {"rotate_clockwise": rotate_clockwise, "images_scale": images_scale,
                             "exif_normalized": False, "order": ["PDF render", "clockwise_rotation"]}
            else:
                raise ValueError(f"unsupported render kind: {kind}")
        except Exception as exc:
            _fatal(exc)
            row["errors"].append(f"{type(exc).__name__}: {exc}")
            rows.append(row)
            continue
        # Publication failures must escape; they are not content decode gaps.
        target = destination / f"unit-{unit_id:06d}.png"
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"refusing to replace rendered asset: {target}")
        image.save(target, format="PNG", dpi=dpi)
        row.update(decode_status="ok", asset=target.name, width=image.width, height=image.height,
                   dpi=list(dpi), transform=transform, recognition_warnings=warnings)
        rows.append(row)
    return rows


def _visible_layers():
    from docling_core.types.doc import ContentLayer
    # Background/invisible text must not be presented as visible source words.
    return {layer for layer in ContentLayer if layer.value not in ("background", "invisible")}


def native_text_empty(document, page_no=None):
    """Test native reading presence with generated picture markers disabled.

    Literal text, captions, tables and picture descendants remain visible.
    This never establishes source blankness or changes the exported Markdown.
    """
    return not document.export_to_markdown(page_no=page_no, included_content_layers=_visible_layers(),
        traverse_pictures=True, escape_underscores=False, image_placeholder="").strip()


def _document_text(document, page_no=None):
    return document.export_to_markdown(page_no=page_no, included_content_layers=_visible_layers(),
                                       traverse_pictures=True, escape_underscores=False)


class DoclingBackend:
    def __init__(self, options=None):
        self.options = options if isinstance(options, BackendOptions) else BackendOptions(**(options or {}))
        self._converters = {}
        self._preflights = {}

    def _converter(self, kind, mode):
        key = ("docx" if kind == "docx" else "paginated", mode)
        if key in self._converters:
            return self._converters[key]
        require_models = kind != "docx"
        if require_models not in self._preflights:
            self._preflights[require_models] = preflight(self.options, require_models=require_models)
        report = self._preflights[require_models]
        if not report["ready"]:
            raise RuntimeError("conversion preflight failed: " + "; ".join(report["errors"]))
        # These guard upstream model libraries too; no ambient cache miss may
        # reach a network download even if an upstream implementation changes.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        from docling.document_converter import DocumentConverter, PdfFormatOption, ImageFormatOption, WordFormatOption
        from docling.datamodel.base_models import InputFormat
        if kind == "docx":
            from docling.datamodel.pipeline_options import ConvertPipelineOptions
            pipeline = ConvertPipelineOptions(document_timeout=self.options.document_timeout,
                                              enable_remote_services=False, allow_external_plugins=False)
            converter = DocumentConverter(allowed_formats=[InputFormat.DOCX],
                                          format_options={InputFormat.DOCX: WordFormatOption(pipeline_options=pipeline)})
        else:
            from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
            from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractCliOcrOptions, OcrMode
            from docling.backend.image_backend import ImageDocumentBackend, _ImagePageBackend
            rotation = self.options.rotate_clockwise

            class RotatedImageBackend(ImageDocumentBackend):
                def load_page(self, page_no):
                    with self._lock:
                        self._source.seek(page_no)
                        orientation = self._source.getexif().get(274, 1)
                        if isinstance(orientation, bool) or orientation not in range(1, 9):
                            raise ValueError(f"invalid EXIF orientation on frame {page_no + 1}: {orientation!r}")
                    page = super().load_page(page_no)
                    if not rotation:
                        return page
                    pixels = page._image.rotate(-rotation, expand=True)
                    dpi = page._dpi[::-1] if rotation in (90, 270) else page._dpi
                    return _ImagePageBackend(pixels, page_no, dpi)

            pipeline = PdfPipelineOptions(
                artifacts_path=Path(self.options.artifacts_path),
                accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=self.options.threads),
                document_timeout=self.options.document_timeout, images_scale=self.options.images_scale,
                generate_page_images=True, generate_picture_images=True, do_ocr=True, do_table_structure=True,
                enable_remote_services=False, allow_external_plugins=False, queue_max_size=1,
                ocr_batch_size=1, layout_batch_size=1, table_batch_size=1,
                ocr_options=TesseractCliOcrOptions(lang=["eng"], tesseract_cmd=report["tesseract_cmd"],
                                                  mode=OcrMode.FULL_PAGE if mode == "full_page" else OcrMode.DEFAULT))
            # Separate pipeline settings keep PDF rendering unchanged and let
            # Docling crop pictures using the same scale as its page image.
            raster_pipeline = pipeline.model_copy(update={"images_scale": self.options.raster_images_scale}) if self.options.raster_images_scale is not None else pipeline
            pdf_kwargs = {"pipeline_options": pipeline}
            if mode == "pdfium":
                from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
                pdf_kwargs["backend"] = PyPdfiumDocumentBackend
            converter = DocumentConverter(allowed_formats=[InputFormat.PDF, InputFormat.IMAGE], format_options={
                InputFormat.PDF: PdfFormatOption(**pdf_kwargs),
                InputFormat.IMAGE: ImageFormatOption(pipeline_options=raster_pipeline)})
            # Pinned 2.142.1 constructor rewrites IMAGE subclasses. Install the
            # range-preserving adapter after that compatibility normalization.
            converter.format_to_options[InputFormat.IMAGE] = ImageFormatOption(
                pipeline_options=raster_pipeline, backend=RotatedImageBackend)
        self._converters[key] = converter
        return converter

    def convert_range(self, path, kind, start, end, *, mode="default", scratch=None):
        path = Path(path)
        if mode not in ("default", "full_page", "pdfium"):
            raise ValueError(f"unknown conversion mode: {mode}")
        if isinstance(start, bool) or isinstance(end, bool) or start < 1 or end < start or end - start + 1 > MAX_RANGE:
            raise ValueError("conversion requires a positive inclusive range of at most 16 units")
        if kind in ("txt", "docx") and (start, end) != (1, 1):
            raise ValueError("TXT/DOCX use only the (1,1) structural sentinel range")
        result = {"document": None, "units": [], "status": "failed", "errors": [], "start": start, "end": end,
                  "provenance": {"backend": "docling", "mode": mode, "source": str(path),
                                 "options": self.options.frozen(), "versions": PINNED_VERSIONS.copy()}}
        if kind == "txt":
            try:
                text, encoding = _read_text(path)
                result["units"] = [{"unit_id": f"line:{n}", "decode_status": "not_applicable", "conversion_status": "ok",
                                    "text": line, "recognition_warnings": [], "doc_page_no": None, "transform": {},
                                    "attempts": [{"stage": "strict_decode", "encoding": encoding}]}
                                   for n, line in enumerate(text.splitlines(keepends=True), 1)]
                result["status"] = "ok"
                result["provenance"].update(backend="strict-text", encoding=encoding, native_document=False)
            except (OSError, UnicodeError) as exc:
                _fatal(exc)
                result["errors"].append(f"{type(exc).__name__}: {exc}")
            return result
        original_ids = list(range(start, end + 1))
        try:
            if kind == "pdf" and self.options.rotate_clockwise:
                return self._rotated_pdf(path, start, end, mode, scratch)
            converter = self._converter(kind, mode)
            # A Path prevents Docling from treating any string as an HTTP URL.
            progress, diagnostics = [], []
            class DiagnosticHandler(logging.Handler):
                def emit(self, record):
                    diagnostics.append(record.getMessage())
            handler = DiagnosticHandler(level=logging.WARNING)
            logger = logging.getLogger("docling")
            logger.addHandler(handler)
            def on_progress(event):
                progress.append(event.model_dump(mode="json") if hasattr(event, "model_dump") else str(event))
            try:
                converted = converter.convert(path, raises_on_error=False, page_range=(start, end), progress_callback=on_progress)
            finally:
                logger.removeHandler(handler)
            result["provenance"]["progress"] = progress
            result["recognition_warnings"] = list(dict.fromkeys(diagnostics))
            result = self._adapt(converted, kind, original_ids, result)
            for unit in result["units"]:
                unit["recognition_warnings"].extend("source-scoped Docling diagnostic: " + message for message in result["recognition_warnings"])
            return result
        except Exception as exc:
            _fatal(exc)
            result["errors"].append(f"{type(exc).__name__}: {exc}")
            ids = ["package:word/document.xml"] if kind == "docx" else original_ids
            result["units"] = [self._failed_unit(n, result["errors"][-1], mode, kind) for n in ids]
            return result

    def _failed_unit(self, unit_id, error, mode, kind):
        return {"unit_id": unit_id, "decode_status": "not_applicable" if kind == "docx" else "unknown",
                "conversion_status": "failed", "text": "", "recognition_warnings": [], "error": error,
                "doc_page_no": None if kind == "docx" else unit_id,
                "transform": {"rotate_clockwise": self.options.rotate_clockwise,
                              "exif_normalized": None,
                              "order": ["exif", "clockwise_rotation"] if kind in ("image", "tiff") else []},
                "attempts": [{"stage": "docling", "mode": mode, "status": "failed"}]}

    def _adapt(self, converted, kind, original_ids, result):
        document = converted.document
        result["document"] = document
        errors = [error.model_dump(mode="json") if hasattr(error, "model_dump") else {"error_message": str(error)}
                  for error in getattr(converted, "errors", [])]
        result["errors"] = errors
        status = str(getattr(converted.status, "value", converted.status))
        result["provenance"]["docling_status"] = status
        if kind == "docx":
            row = self._failed_unit("package:word/document.xml", "DOCX conversion failed", result["provenance"]["mode"], kind)
            if status in ("success", "partial_success") and document is not None:
                row.update(conversion_status="ok" if status == "success" and not errors else "partial",
                           text=_document_text(document), error=None)
                row["attempts"][0]["status"] = row["conversion_status"]
                row["structural_locators"] = [item.self_ref for item, _ in document.iterate_items(
                    traverse_pictures=True, included_content_layers=_visible_layers())]
            result["units"] = [row]
        else:
            successful = {page.page_no for page in getattr(converted, "pages", [])}
            page_errors = {error.get("page_no") for error in errors if error.get("page_no") is not None}
            global_errors = [error for error in errors if error.get("page_no") is None]
            for number in original_ids:
                row = self._failed_unit(number, "Docling did not complete this original unit", result["provenance"]["mode"], kind)
                if number in successful and number not in page_errors and document is not None:
                    row.update(decode_status="ok", conversion_status="partial" if global_errors else "ok",
                               text=_document_text(document, number), error=None)
                    row["attempts"][0]["status"] = row["conversion_status"]
                    if kind in ("image", "tiff"):
                        row["transform"]["exif_normalized"] = True
                    row["native_text_empty"] = native_text_empty(document, number)
                    if row["native_text_empty"]:
                        row["recognition_warnings"].append("no text detected; inspect full-unit image")
                    if global_errors:
                        row["recognition_warnings"].append("document-scoped conversion errors; successful page retained")
                elif number in page_errors:
                    row["error"] = "; ".join(error.get("error_message", "page error") for error in errors if error.get("page_no") == number)
                result["units"].append(row)
        statuses = {row["conversion_status"] for row in result["units"]}
        result["status"] = "ok" if statuses == {"ok"} else "partial" if statuses.intersection({"ok", "partial"}) else "failed"
        return result

    def _rotated_pdf(self, path, start, end, mode, scratch):
        """Raster rotation retains exact original mapping; coordinates are derivative."""
        from PIL import Image
        context = tempfile.TemporaryDirectory(prefix="docling-rotation-", dir=scratch)
        with context as tmp:
            tmp = Path(tmp)
            rows = render_units(path, "pdf", range(start, end + 1), tmp / "rendered",
                                rotate_clockwise=self.options.rotate_clockwise, images_scale=self.options.images_scale)
            good = [row for row in rows if row["decode_status"] == "ok"]
            if not good:
                result = {"document": None, "units": [self._failed_unit(r["unit_id"], "; ".join(r["errors"]), mode, "pdf") for r in rows],
                          "start": start, "end": end, "errors": ["PDF raster rotation failed"], "status": "failed", "provenance": {"mode": mode}}
                return result
            images = []
            for row in good:
                with Image.open(tmp / "rendered" / row["asset"]) as image:
                    images.append(image.copy())
            derivative = tmp / "normalized.tiff"
            images[0].save(derivative, save_all=True, append_images=images[1:], dpi=(72 * self.options.images_scale,) * 2)
            # Rendered pixels already include explicit rotation, so use a warm
            # zero-rotation adapter for this finite derivative.
            options = self.options.frozen()
            options["rotate_clockwise"] = 0
            if not hasattr(self, "_raster_backend"):
                self._raster_backend = DoclingBackend(BackendOptions(**options))
            result = self._raster_backend.convert_range(derivative, "tiff", 1, len(good), mode=mode, scratch=scratch)
            mapping = {i: row["unit_id"] for i, row in enumerate(good, 1)}
            document = result.get("document")
            if document is not None:
                document.pages = {mapping[n]: page.model_copy(update={"page_no": mapping[n]}) for n, page in document.pages.items() if n in mapping}
                from docling_core.types.doc import ContentLayer
                for item, _ in document.iterate_items(traverse_pictures=True, included_content_layers=set(ContentLayer)):
                    for provenance in getattr(item, "prov", []):
                        provenance.page_no = mapping[provenance.page_no]
            for error in result.get("errors", []):
                if isinstance(error, dict) and error.get("page_no") in mapping:
                    error["page_no"] = mapping[error["page_no"]]
            for unit in result["units"]:
                unit["unit_id"] = mapping[unit["unit_id"]]
                unit["doc_page_no"] = unit["unit_id"]
                unit["transform"] = next(r["transform"] for r in good if r["unit_id"] == unit["unit_id"])
            result["units"].extend(self._failed_unit(r["unit_id"], "; ".join(r["errors"]), mode, "pdf") for r in rows if r["decode_status"] != "ok")
            result["units"].sort(key=lambda unit: unit["unit_id"])
            result.update(start=start, end=end)
            result["provenance"].update(source=str(path), raster_derivative=True,
                                         original_mapping=mapping, coordinate_space="rotated raster derivative")
            if len(good) != len(rows) and result["status"] == "ok":
                result["status"] = "partial"
            return result
