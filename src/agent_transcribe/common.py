"""Shared mechanics only: exact inputs, checked outputs, checkpoints and locks."""
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import signal
import shutil
from pathlib import Path
import subprocess
import tempfile
import time
from datetime import datetime, timezone

ICMAT = (os.environ.get("AGENT_TRANSCRIBE_ICMAT") or shutil.which("icmat")
         or str(Path.home() / "bin" / "icmat"))


class IntegrityError(ValueError):
    """Preserve a changed/untracked artifact instead of continuing a batch."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(args, timeout=300, *, check=True, env=None):
    args = [str(a) for a in args]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, env=dict(os.environ, **(env or {})))
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        # Descendants may still hold the pipes after their parent exits.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    if check and process.returncode:
        raise RuntimeError(f"{Path(args[0]).name} ({process.returncode}): " + stderr.decode(errors="replace")[-2000:])
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def checksum(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic(path, data, *, replace=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data if isinstance(data, bytes) else data.encode())
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # Atomic creation; an existing name is never replaced.
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_copy(path, source):
    """Publish a file exclusively, without loading a large PDF into memory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output, Path(source).open("rb") as original:
            shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def fetch(paths):
    result = run([ICMAT, "fetch", "--json", "--", *paths], timeout=1200, check=False)
    try:
        receipt = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("icmat returned no JSON receipt: " + result.stderr.decode(errors="replace")[-1000:]) from exc
    counts = receipt.get("counts", {})
    ok = (result.returncode == 0 and receipt.get("success")
          and receipt.get("enumerationComplete") and not receipt.get("failures")
          and counts.get("filesVerified") == counts.get("regularFiles") == len(paths)
          and counts.get("datalessFiles") == 0)
    return receipt, bool(ok)


def prepare_sources(paths, *, allow_missing=False):
    paths = [Path(p).expanduser() for p in paths]
    if any(p.is_symlink() or (p.exists() and not p.is_file()) or
           (not allow_missing and not p.is_file()) for p in paths):
        raise ValueError("Inputs must be existing regular files, not directories or symlinks")
    resolved = [p.resolve() for p in paths]
    if not resolved or len(resolved) != len(set(resolved)):
        raise ValueError("Supply a nonempty list of distinct source files in their intended order")
    return resolved


@contextlib.contextmanager
def job(output, kind, sources, settings, materialize, *, best_effort=False, source_root=None):
    """State is a mechanical receipt, never a document catalog or metadata authority."""
    if not materialize and any("com~apple~CloudDocs" in source.parts for source in sources):
        raise ValueError("Use --fetch for iCloud sources so the installed icmat verifies the exact inputs")
    output = Path(output).expanduser().absolute()
    if output.exists() and output.is_symlink():
        raise ValueError("Derivative directory must not be a symlink")
    output = output.resolve()  # Resolve parent aliases before writing portable relative locators.
    for source in sources:
        if source.resolve().is_relative_to(output.resolve()):
            raise ValueError("The derivative directory must not contain an original source")
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".processing.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("This output folder already has an active writer") from exc
        ledger = output / ".processing.json"
        source_root = (Path(source_root).resolve() if source_root is not None else
                       Path(os.path.commonpath([str(p.parent) for p in sources])))
        keys = [p.relative_to(source_root).as_posix() for p in sources]
        specification = {"kind": kind, "sources": keys, "settings": settings,
                         "source_path_base": "source_root"}
        if ledger.exists():
            state = json.loads(ledger.read_text())
            if state.get("schema") != 2 or state["specification"] != specification:
                raise ValueError("Inputs/settings differ from this job. Use a new output folder.")
            atomic(output / ".processing.previous.json", ledger.read_bytes())
        else:
            if any(p.name not in {"README.md", ".processing.lock"} for p in output.iterdir()):
                raise ValueError("New jobs require a fresh output folder (an existing README is allowed)")
            state = {"schema": 2, "specification": specification, "started_at": now(),
                     "sources": {}, "steps": {}}
        state.setdefault("source_root", {"original_absolute": str(source_root)})
        state["source_root"]["relative_to_job"] = os.path.relpath(source_root, output)
        current = Job(output, state, source_root=source_root)
        current.save()
        intake_started = time.monotonic()
        if materialize and not best_effort:
            receipt, ok = fetch(sources)
            atomic(output / "icmat-receipt.json", json_bytes(receipt))
            if not ok:
                raise RuntimeError("icmat did not verify every input; receipt saved, processing stopped")
        state["intake_failures"] = {}
        for source in sources:
            key = current.source_key(source)
            try:
                if materialize and best_effort:
                    receipt, ok = fetch([source])
                    intake_name = ("by-source/" + key + "/receipt.json" if len(Path(key).parts) > 1
                                   else source.name + ".json")
                    target = output / ".intake" / intake_name
                    # A bookkeeping write failure is fatal, rather than a source gap.
                    try:
                        if target.exists():
                            atomic(target.with_name(target.name + ".previous"), target.read_bytes())
                        atomic(target, json_bytes(receipt))
                    except OSError as exc:
                        raise IntegrityError("Cannot preserve the intake receipt: " + str(target)) from exc
                    if not ok:
                        raise RuntimeError("icmat did not verify this input; exact-file receipt saved")
                before = source.stat()
                if getattr(before, "st_flags", 0) & 0x40000000:
                    raise RuntimeError("Dataless source: rerun with --fetch: " + str(source))
                value = checksum(source)
                after = source.stat()
            except (OSError, RuntimeError) as exc:
                if not best_effort or isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    raise
                state["intake_failures"][key] = {"error": str(exc), "at": now()}
                current.save()
                continue
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Source changed during verification: " + str(source))
            previous = state["sources"].get(key)
            if previous and previous["sha256"] != value:
                raise ValueError("Source bytes changed. Preserve this job and use a new output folder.")
            state["sources"][key] = {"bytes": after.st_size, "sha256": value,
                                                  "verified_at": now()}
            current.save()
        state.setdefault("timings", {})["last_intake_seconds"] = round(time.monotonic() - intake_started, 6)
        try:
            yield current
            verification_started = time.monotonic()
            current.verify_inputs_unchanged()
            state["timings"]["last_input_verification_seconds"] = round(time.monotonic() - verification_started, 6)
        finally:
            current.save()


class Job:
    def __init__(self, folder, state, *, source_root):
        self.folder, self.state = folder, state
        self.source_root = source_root

    def source_key(self, source):
        return Path(source).relative_to(self.source_root).as_posix()

    def source_path(self, key):
        return self.source_root / key

    def save(self):
        atomic(self.folder / ".processing.json", json_bytes(self.state))

    def completed(self, name):
        return self.state["steps"].get(name, {}).get("status") == "done"

    def source_checksum(self, source):
        return self.state["sources"][self.source_key(source)]["sha256"]

    def output_path(self, relative):
        part = Path(relative)
        target = self.folder / part
        if part.is_absolute() or ".." in part.parts or not target.resolve().is_relative_to(self.folder.resolve()):
            raise IntegrityError("Output path escapes the derivative folder")
        return target

    def step(self, name, build):
        old = self.state["steps"].get(name)
        if old and old["status"] == "done":
            for relative, expected in old["outputs"].items():
                path = self.output_path(relative)
                if path.is_symlink() or not path.is_file() or checksum(path) != expected:
                    raise IntegrityError("A completed output is missing or modified: " + str(path))
            self.cleanup_stage(old)
            return old["details"]
        prepared = bool(old and old.get("prepared_outputs"))
        if prepared:
            entry = old
        else:
            previous_stages = list((old or {}).get("previous_stages", []))
            if old and old.get("stage"):
                previous_stages.append(old["stage"])
            stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.folder))
            entry = {"status": "running", "attempts": (old or {}).get("attempts", 0) + 1,
                     "started_at": now(), "outputs": dict((old or {}).get("outputs", {})),
                     "stage": stage.name, "previous_stages": previous_stages,
                     "failures": list((old or {}).get("failures", []))}
        self.state["steps"][name] = entry
        self.save()
        try:
            if not prepared:
                with tempfile.TemporaryDirectory(prefix="archive-job-") as directory:
                    files, details = build(Path(directory))
                    if not files:
                        raise ValueError("A scan step must produce at least one output")
                    staged = {}
                    for i, (relative, temporary) in enumerate(files.items()):
                        self.output_path(relative)
                        target = stage / f"output-{i:04d}"
                        atomic_copy(target, temporary)
                        staged[relative] = {"file": target.name, "sha256": checksum(target)}
                entry.update(status="prepared", prepared_outputs=staged, details=details)
                self.save()  # All exact bytes are durable before any final output is published.
            files = self.prepared_files(entry)
            for relative, temporary in files.items():
                target = self.output_path(relative)
                expected = entry["prepared_outputs"][relative]["sha256"]
                if target.exists() or target.is_symlink():
                    if (entry["outputs"].get(relative) != expected or target.is_symlink()
                            or not target.is_file() or checksum(target) != expected):
                        raise IntegrityError("Untracked or modified output; refusing to overwrite: " + str(target))
                else:
                    entry["outputs"][relative] = expected
                    self.save()
                    atomic_copy(target, temporary)
                    if checksum(target) != expected:
                        raise IntegrityError("Published output differs from prepared bytes: " + str(target))
            entry.update(status="done", finished_at=now())
            entry.pop("error", None)
            self.save()
            self.cleanup_stage(entry)
        except BaseException as exc:
            entry["failures"].append({"at": now(), "error": str(exc) or type(exc).__name__})
            if entry["status"] != "done":
                entry.update(status="failed", error=str(exc) or type(exc).__name__, finished_at=now())
            self.save()
            raise
        return entry["details"]

    def stage_path(self, entry):
        name = entry["stage"]
        path = self.folder / name
        if (Path(name).name != name or not name.startswith(".stage-") or path.is_symlink()
                or path.resolve().parent != self.folder.resolve()):
            raise IntegrityError("Invalid prepared-output stage; preserve this job for inspection")
        return path

    def prepared_files(self, entry, *, cleaning=False):
        stage = self.stage_path(entry)
        if cleaning and not stage.exists():
            return {}
        if not stage.is_dir():
            raise IntegrityError("Prepared outputs are missing; restore the stage, do not rerun OCR")
        files = {}
        names = set()
        for relative, record in entry["prepared_outputs"].items():
            self.output_path(relative)
            name = record["file"]
            if Path(name).name != name or name in names:
                raise IntegrityError("Invalid prepared-output mapping")
            names.add(name)
            path = stage / name
            if cleaning and not path.exists():
                continue
            if path.is_symlink() or not path.is_file() or checksum(path) != record["sha256"]:
                raise IntegrityError("A prepared output is missing or modified: " + str(path))
            files[relative] = path
        if any(p.name not in names for p in stage.iterdir()):
            raise IntegrityError("Untracked file in prepared stage; preserve it for inspection")
        return files

    def cleanup_stage(self, entry):
        if not entry.get("prepared_outputs"):
            raise IntegrityError("Completed step lacks its prepared-output checkpoint")
        files = self.prepared_files(entry, cleaning=True)
        for path in files.values():
            path.unlink()
        stage = self.stage_path(entry)
        if stage.exists():
            stage.rmdir()

    def write_readme(self, text):
        """Preserve human edits; adapters supply all descriptive content."""
        path = self.folder / "README.md"
        with contextlib.suppress(FileExistsError):
            atomic(path, text, replace=False)

    def verify_inputs_unchanged(self):
        for original, record in self.state["sources"].items():
            if original in self.state.get("intake_failures", {}):
                continue
            if checksum(self.source_path(original)) != record["sha256"]:
                raise RuntimeError("Original changed during this job: " + original)
