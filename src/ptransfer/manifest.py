"""The transfer bundle: one folder that holds everything from one phone.

Layout::

    MyPhone.ptbundle/
      manifest.json         what is in here, from which phone, when
      checksums.sha256      integrity index, one line per file
      media/                mirror of the phone's internal storage
      apps/<package>/       APKs (base + splits)
      data/                 contacts.vcf, sms.json, calls.json, settings.json
      logs/transfer.log

A folder rather than a single archive on purpose: a 200 GB zip that fails at
99% is worthless, and a folder can be resumed, inspected and copied to the new
phone piecemeal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from . import BUNDLE_SCHEMA_VERSION

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
CHECKSUM_NAME = "checksums.sha256"


@dataclass
class SourceDevice:
    serial: str = ""
    brand: str = ""
    manufacturer: str = ""
    model: str = ""
    device: str = ""
    android_release: str = ""
    sdk: int = 0
    build_id: str = ""


@dataclass
class SectionResult:
    name: str
    status: str = "pending"  # pending | ok | partial | failed | skipped
    files: int = 0
    bytes: int = 0
    message: str = ""
    items: int = 0  # logical items (contacts, messages...) where meaningful

    @property
    def succeeded(self) -> bool:
        return self.status in ("ok", "partial")


@dataclass
class Manifest:
    schema: int = BUNDLE_SCHEMA_VERSION
    app_version: str = ""
    created_at: str = ""
    finished_at: str = ""
    source: SourceDevice = field(default_factory=SourceDevice)
    sections: dict[str, SectionResult] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        data = asdict(self)
        data["sections"] = {k: asdict(v) for k, v in self.sections.items()}
        return json.dumps(data, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        data = json.loads(text)
        m = cls(
            schema=data.get("schema", 1),
            app_version=data.get("app_version", ""),
            created_at=data.get("created_at", ""),
            finished_at=data.get("finished_at", ""),
            warnings=list(data.get("warnings", [])),
        )
        m.source = SourceDevice(**{k: v for k, v in (data.get("source") or {}).items() if k in SourceDevice.__annotations__})
        for name, sec in (data.get("sections") or {}).items():
            m.sections[name] = SectionResult(**{k: v for k, v in sec.items() if k in SectionResult.__annotations__})
        return m

    @property
    def total_bytes(self) -> int:
        return sum(s.bytes for s in self.sections.values())

    @property
    def total_files(self) -> int:
        return sum(s.files for s in self.sections.values())


class Bundle:
    """Read/write access to a bundle directory."""

    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root)
        self.manifest = Manifest()

    # --- creation -----------------------------------------------------
    @classmethod
    def create(cls, root: str | os.PathLike, source: SourceDevice, app_version: str = "") -> "Bundle":
        b = cls(root)
        b.root.mkdir(parents=True, exist_ok=True)
        for sub in ("media", "apps", "data", "logs"):
            (b.root / sub).mkdir(exist_ok=True)
        b.manifest = Manifest(
            app_version=app_version,
            created_at=_now(),
            source=source,
        )
        b.save()
        return b

    @classmethod
    def open(cls, root: str | os.PathLike) -> "Bundle":
        b = cls(root)
        path = b.root / MANIFEST_NAME
        if not path.exists():
            raise FileNotFoundError(f"{path} not found - that folder is not a transfer bundle.")
        b.manifest = Manifest.from_json(path.read_text(encoding="utf-8"))
        return b

    # --- paths --------------------------------------------------------
    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def apps_dir(self) -> Path:
        return self.root / "apps"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    # --- mutation -----------------------------------------------------
    def section(self, name: str) -> SectionResult:
        return self.manifest.sections.setdefault(name, SectionResult(name=name))

    def record(self, result: SectionResult) -> None:
        self.manifest.sections[result.name] = result
        self.save()

    def warn(self, message: str) -> None:
        if message not in self.manifest.warnings:
            self.manifest.warnings.append(message)
        log.warning(message)

    def finish(self) -> None:
        self.manifest.finished_at = _now()
        self.save()

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (MANIFEST_NAME + ".tmp")
        tmp.write_text(self.manifest.to_json(), encoding="utf-8")
        tmp.replace(self.root / MANIFEST_NAME)

    # --- integrity ----------------------------------------------------
    def write_checksums(self, progress=None) -> int:
        """Hash every file in the bundle. Returns the number hashed."""
        lines: list[str] = []
        files = [p for p in sorted(self.root.rglob("*")) if p.is_file() and p.name not in (MANIFEST_NAME, CHECKSUM_NAME)]
        for i, p in enumerate(files, 1):
            digest = sha256_file(p)
            rel = p.relative_to(self.root).as_posix()
            lines.append(f"{digest}  {rel}")
            if progress:
                progress(i / max(1, len(files)), rel)
        (self.root / CHECKSUM_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return len(files)

    def verify(self, progress=None) -> list[str]:
        """Re-hash and report mismatches. Empty list means the bundle is intact."""
        path = self.root / CHECKSUM_NAME
        if not path.exists():
            return ["No checksums.sha256 in this bundle - cannot verify."]
        problems: list[str] = []
        entries = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for i, line in enumerate(entries, 1):
            expected, _, rel = line.partition("  ")
            target = self.root / rel
            if not target.exists():
                problems.append(f"missing: {rel}")
            elif sha256_file(target) != expected:
                problems.append(f"corrupt: {rel}")
            if progress:
                progress(i / max(1, len(entries)), rel)
        return problems

    def size_on_disk(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def safe_bundle_name(device_label: str) -> str:
    # Dots are kept so "Nokia 8.3" stays recognisable; the timestamp suffix
    # means the result can never come out as "." or "..".
    keep = [c if (c.isalnum() or c in " -_.") else "-" for c in device_label]
    name = "".join(keep).strip().replace(" ", "-")
    while "--" in name:
        name = name.replace("--", "-")
    return (name or "phone") + "-" + time.strftime("%Y%m%d-%H%M")


def iter_bundle_media(bundle: Bundle) -> Iterable[Path]:
    if bundle.media_dir.exists():
        yield from (p for p in bundle.media_dir.rglob("*") if p.is_file())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
