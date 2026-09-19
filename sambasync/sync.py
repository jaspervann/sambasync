"""Safe, single-share rsync transfers using the configured SSH transport."""

from __future__ import annotations

from contextlib import nullcontext
import ipaddress
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .common import NasError, Settings, Share


def _has_controls(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def validate_local_path(root: Path, value: str) -> Path:
    """Accept an absolute or root-relative path, without following symlinks.

    Missing directories are allowed so this also validates a share before add.
    Samba substitutions cannot identify a single directory to back up.
    """
    if not root.is_absolute() or ".." in root.parts:
        raise NasError("The share root must be an absolute path without traversal.")
    if _has_controls(str(root)) or "%" in str(root) or "\\" in str(root):
        raise NasError("The share root must be a literal path without control characters or Samba substitutions.")
    if not value or not value.strip() or _has_controls(value) or "%" in value or "\\" in value:
        raise NasError("Local path must be a literal directory path without control characters or Samba substitutions.")
    if any(part in (".", "..") for part in value.split("/")):
        raise NasError("Local path must not contain '.' or '..' components.")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if path == root or not path.is_relative_to(root):
        raise NasError(f"Local path must be strictly inside {root}.")
    try:
        for component in (*reversed(path.parents), path):
            if component.is_symlink():
                raise NasError(f"Local path contains a symbolic link: {component}")
    except OSError as exc:
        raise NasError(f"Cannot inspect local path: {path}") from exc
    return path


def _remote_path(value: str, label: str) -> str:
    value = value.rstrip("/")
    if (
        not value
        or value.startswith("/")
        or value != value.strip()
        or _has_controls(value)
        or any(char in value for char in "\\:*?[]%")
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise NasError(f"{label} must be a nonempty relative path without traversal, wildcards or control characters.")
    return value


def remote_destination(settings: Settings, share: Share) -> str:
    """Return USER@HOST::MODULE/PATH/, or USER@HOST::SHARE/ for an empty root."""
    rsync = settings.rsync
    host = rsync.host
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError as exc:
            raise NasError("Invalid rsync host.") from exc
    elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", host):
        raise NasError("Rsync host must be a hostname, IPv4 address or bracketed IPv6 address.")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\$?", rsync.user):
        raise NasError("Invalid rsync user.")
    remote = _remote_path(share.name, "Share name")
    if rsync.root:
        root = _remote_path(rsync.root, "Rsync root")
        remote = f"{root}/{remote}"
    return f"{rsync.user}@{host}::{remote}/"


def check_source(settings: Settings, share: Share) -> Path:
    """Refuse deletion-capable syncs from a missing or empty source."""
    path = validate_local_path(settings.root, share.path)
    try:
        if not path.is_dir():
            raise NasError(f"Share directory does not exist: {path}. Sync aborted.")
        with os.scandir(path) as entries:
            if next(entries, None) is None:
                raise NasError(f"Share directory is empty: {path}. Sync aborted.")
    except OSError as exc:
        raise NasError(f"Cannot read share directory: {path}. Sync aborted.") from exc
    return path


def _check_password(password: str, label: str) -> None:
    if any(char in password for char in "\r\n\0"):
        raise NasError(f"{label} cannot contain newline or NUL characters.")


def sync_share(settings: Settings, share: Share, dry_run: bool = False) -> int:
    """Run rsync interactively and return its status without exposing passwords."""
    destination = remote_destination(settings, share)
    rsync = settings.rsync
    if isinstance(rsync.port, bool) or not isinstance(rsync.port, int) or not 1 <= rsync.port <= 65535:
        raise NasError("SSH port must be an integer between 1 and 65535.")
    _check_password(rsync.password, "SSH password")
    ssh = ["ssh", "-p", str(rsync.port), "-l", rsync.user]
    source = check_source(settings, share)
    command = ["rsync", "-avh", "--delete", "--info=progress2", "--protect-args"]
    if dry_run:
        command.append("--dry-run")
    # Rsync parses -e itself, not with a POSIX shell: a literal quote is
    # represented by doubling it inside a quoted argument.
    remote_shell = " ".join("'" + arg.replace("'", "''") + "'" for arg in ssh)
    command.extend(["-e", remote_shell])
    command.extend(["--", f"{source}/", destination])
    try:
        with (tempfile.TemporaryFile(mode="w+b") if rsync.password else nullcontext()) as secret:
            pass_fds: tuple[int, ...] = ()
            if secret is not None:
                secret.write((rsync.password + "\n").encode("utf-8"))
                secret.seek(0)
                password_fd = secret.fileno()
                command = ["sshpass", "-d", str(password_fd), *command]
                pass_fds = (password_fd,)
            result = subprocess.run(command, check=False, pass_fds=pass_fds).returncode
            return result if result >= 0 else 128 - result
    except FileNotFoundError as exc:
        raise NasError("Cannot start sync; install rsync, ssh and (for password login) sshpass.") from exc
    except OSError as exc:
        raise NasError("Cannot start sync or prepare the SSH password descriptor.") from exc
