"""Read share definitions and make byte-preserving, validated Samba changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .common import NasError, Share


def _key(value: str) -> str:
    # Samba ignores whitespace and case in parameter names. Using the same
    # conservative comparison for share names also prevents ambiguous aliases.
    return "".join(value.split()).casefold()


@dataclass
class _Section:
    name: str
    start: int
    end: int
    values: dict[str, str]
    continued: set[str]


class SambaDocument:
    """Keep the original bytes; never serialize unrelated configuration."""

    def __init__(self, data: bytes):
        self.data = data
        self.sections: list[_Section] = []
        self._external: list[str] = []
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NasError("Samba configuration must be UTF-8 to manage its shares.") from exc
        if b"\x00" in data:
            raise NasError("Samba configuration contains a NUL byte.")
        current: _Section | None = None
        offset = 0
        pending = ""
        pending_start = 0
        continued = False
        for raw in data.splitlines(keepends=True):
            start, offset = offset, offset + len(raw)
            line = raw.decode("utf-8").rstrip("\r\n")
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            if not pending:
                pending_start = start
            if line.rstrip().endswith("\\"):
                pending += line.rstrip()[:-1]
                continued = True
                continue
            logical = pending + line
            pending = ""
            clean = logical.strip()
            if clean.startswith("["):
                match = re.fullmatch(r"\[([^\[\]]+)\]\s*(?:[#;].*)?", clean)
                if match is None or continued:
                    raise NasError("Unsupported or continued Samba section header; simplify it before managing shares.")
                name = match.group(1).strip()
                if not name:
                    raise NasError("Samba configuration contains an empty section name.")
                if current is not None:
                    current.end = pending_start
                current = _Section(name, pending_start, len(data), {}, set())
                self.sections.append(current)
            elif "=" in logical:
                name, value = logical.split("=", 1)
                name, value = _key(name), value.strip()
                if name in {"include", "configfile"} or (name == "configbackend" and value.casefold() != "file") or (name == "registryshares" and value.casefold() not in {"no", "false", "0"}):
                    self._external.append(name)
                if current is not None:
                    if name in {"path", "directory"}:
                        previous = current.values.get("path", current.values.get("directory"))
                        if previous is not None and previous != value:
                            raise NasError(f"Conflicting paths in [{current.name}]; simplify its path definition.")
                    # Keep the final value when a setting is repeated.
                    current.values.pop(name, None)
                    current.values[name] = value
                    if continued:
                        current.continued.add(name)
            else:
                raise NasError(f"Unsupported Samba configuration line: {clean[:80]}")
            continued = False
        if pending:
            raise NasError("Samba configuration ends with an unfinished continuation.")
        names: set[str] = set()
        for section in self.sections:
            name = _key(section.name)
            if name == "global":
                continue
            if name in names:
                raise NasError(f"Duplicate Samba share name: {section.name}")
            names.add(name)

    def _check_external(self) -> None:
        if self._external:
            raise NasError("This Samba configuration uses includes or an external configuration backend; manage a standalone smb.conf with explicit share paths.")

    @staticmethod
    def _boolean(value: str, section: str) -> bool:
        if value.casefold() in {"yes", "true", "1"}:
            return True
        if value.casefold() in {"no", "false", "0"}:
            return False
        raise NasError(f"Invalid Samba boolean in [{section}]: {value}")

    def _share(self, section: _Section) -> Share:
        values = section.values
        if section.continued & {"path", "directory", "validusers", "browseable", "browsable", "copy"}:
            raise NasError(f"[{section.name}] uses continued share settings; put those settings on single lines before managing it.")
        if "copy" in values:
            raise NasError(f"[{section.name}] inherits settings with 'copy'; use an explicit share definition before managing it.")
        browseable = True
        valid_users = ""
        inherited = []
        for global_section in self.sections:
            if _key(global_section.name) == "global":
                if global_section.continued & {"validusers", "browseable", "browsable"}:
                    raise NasError("Global share defaults use continued settings; put them on single lines before managing shares.")
                inherited.extend(global_section.values.items())
        for key, value in [*inherited, *values.items()]:
            if key in {"browseable", "browsable"}:
                browseable = self._boolean(value, section.name)
            elif key == "validusers":
                valid_users = value
        return Share(
            name=section.name,
            path=values.get("path", values.get("directory", "")),
            valid_users=valid_users,
            browseable=browseable,
        )

    def shares(self) -> list[Share]:
        self._check_external()
        return [self._share(section) for section in self.sections if _key(section.name) != "global"]

    def _find(self, name: str) -> _Section:
        self._check_external()
        if _key(name) == "global":
            raise NasError("The global Samba section cannot be managed as a share.")
        for section in self.sections:
            if _key(section.name) == _key(name):
                return section
        raise NasError(f"Share not found: {name}")

    def get(self, name: str) -> Share:
        return self._share(self._find(name))

    def add(self, share: Share) -> bytes:
        self._check_external()
        name = share.name
        if not name or name != name.strip() or any(ord(c) < 32 or c in "[]/\\%" for c in name):
            raise NasError("Share name must be nonempty and cannot contain brackets, slashes, percent signs, or control characters.")
        if _key(name) in {"global", "homes", "printers"}:
            raise NasError(f"Reserved Samba section name: {name}")
        if any(_key(section.name) == _key(name) for section in self.sections):
            raise NasError(f"Share already exists: {name}")
        for label, value in (("path", share.path), ("valid users", share.valid_users)):
            if not value or value != value.strip() or any(ord(c) < 32 for c in value) or value.endswith("\\"):
                raise NasError(f"Share {label} must be a nonempty single-line value without leading or trailing whitespace.")
        if not Path(share.path).is_absolute() or "%" in share.path:
            raise NasError("Share path must be an absolute path without Samba substitutions (%).")
        newline = "\r\n" if b"\r\n" in self.data else "\n"
        lines = [f"[{name}]"]
        lines.extend([
            f"   path = {share.path}",
            f"   browseable = {'yes' if share.browseable else 'no'}",
            "   writeable = yes",
            "   guest ok = no",
            f"   valid users = {share.valid_users}",
        ])
        separator = b""
        if self.data:
            separator = newline.encode() if self.data.endswith(b"\n") else (newline * 2).encode()
        return self.data + separator + (newline.join(lines) + newline).encode("utf-8")

    def remove(self, name: str) -> bytes:
        section = self._find(name)
        if _key(section.name) in {"homes", "printers"}:
            raise NasError(f"Reserved Samba section cannot be removed: {section.name}")
        # A section owns its lines up to the next header. No other section,
        # including a later [global] section, is rewritten.
        return self.data[:section.start] + self.data[section.end:]


def _read_config(path: Path) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise NasError(f"Samba configuration is not a regular file: {path}")
        return handle.read(), metadata


def _same_version(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mtime_ns, left.st_ctime_ns, left.st_size) == (right.st_dev, right.st_ino, right.st_mtime_ns, right.st_ctime_ns, right.st_size)


def _write_temporary(path: Path, data: bytes, metadata: os.stat_result, prefix: str) -> Path:
    descriptor, filename = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            current = os.fstat(handle.fileno())
            if (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
                os.fchown(handle.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(handle.fileno(), stat.S_IMODE(metadata.st_mode))
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _run(command: tuple[str, ...] | list[str], description: str) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False)
    except OSError as exc:
        raise NasError(f"Could not run {description}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise NasError(f"{description} failed (exit {result.returncode})" + (f": {detail}" if detail else "."))


def change_config(
    path: Path,
    original: bytes,
    updated: bytes,
) -> Path:
    """Validate, back up, atomically replace, reload; restore on reload failure.

    The caller must hold its application lock throughout the read/change cycle.
    File identity and contents are also checked to detect external editors.
    """
    path = Path(path)
    candidate: Path | None = None
    backup: Path | None = None
    installed = False
    metadata: os.stat_result | None = None
    try:
        if path.is_symlink():
            raise NasError(f"Refusing to replace a symlink Samba configuration: {path}")
        current, metadata = _read_config(path)
        if current != original:
            raise NasError("Samba configuration changed concurrently; retry the operation.")
        candidate = _write_temporary(path, updated, metadata, f".{path.name}.smbsync-")
        _run(["testparm", "-s", str(candidate)], "Samba validation")
        current, latest = _read_config(path)
        if current != original or not _same_version(metadata, latest):
            raise NasError("Samba configuration changed during validation; retry the operation.")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = _write_temporary(path, original, metadata, f"{path.name}.backup-{timestamp}-")
        current, latest = _read_config(path)
        if current != original or not _same_version(metadata, latest):
            raise NasError(f"Samba configuration changed before replacement; retry. Backup: {backup}")
        os.replace(candidate, path)
        candidate = None
        installed = True
        _sync_directory(path.parent)
        _run(["smbcontrol", "all", "reload-config"], "Samba reload")
        return backup
    except (OSError, NasError, KeyboardInterrupt) as exc:
        if installed and metadata is not None:
            recovery = ""
            try:
                current, _ = _read_config(path)
                if current != updated:
                    raise NasError("the installed file changed externally; automatic rollback would overwrite that change")
                candidate = _write_temporary(path, original, metadata, f".{path.name}.rollback-")
                os.replace(candidate, path)
                candidate = None
                _sync_directory(path.parent)
                recovery = "Original configuration restored."
                try:
                    _run(["smbcontrol", "all", "reload-config"], "Samba reload after rollback")
                except (OSError, NasError, KeyboardInterrupt) as reload_exc:
                    recovery += f" Reload of the restored configuration failed: {reload_exc}"
            except (OSError, NasError, KeyboardInterrupt) as rollback_exc:
                recovery = f"Automatic rollback failed: {rollback_exc}. Restore the backup manually."
            raise NasError(f"{exc or 'Operation interrupted.'} {recovery} Backup: {backup}") from exc
        if isinstance(exc, (NasError, KeyboardInterrupt)):
            raise
        raise NasError(f"Could not update Samba configuration: {exc}" + (f". Backup: {backup}" if backup else "")) from exc
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)
