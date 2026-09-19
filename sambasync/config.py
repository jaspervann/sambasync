"""Read the administrator's TOML settings without changing smb.conf."""

import os
from pathlib import Path
import re
import stat
import tomllib

from .common import NasError, RsyncSettings, Settings


def _text(table: dict, key: str, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise NasError(f"Configuration '{key}' must be a single-line string.")
    return value


def _absolute(value: str, key: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise NasError(f"Configuration '{key}' must be an absolute path without '..'.")
    return path


def load_config(path: Path) -> Settings:
    try:
        with path.open('rb') as stream:
            permissions = os.fstat(stream.fileno())
            data = tomllib.load(stream)
    except tomllib.TOMLDecodeError:
        # Parser exceptions can contain the input, including a password.
        raise NasError(f"Invalid TOML in {path}.") from None
    except OSError as exc:
        raise NasError(f"Cannot read configuration {path}: {exc.strerror}") from None
    allowed = {'storage', 'samba', 'rsync'}
    if data.keys() - allowed:
        raise NasError('Unknown top-level configuration setting; see README.md.')
    storage = data.get('storage', {})
    samba = data.get('samba', {})
    rsync = data.get('rsync', {})
    if not all(isinstance(table, dict) for table in (storage, samba, rsync)):
        raise NasError('[storage], [samba] and [rsync] must be TOML tables.')
    if storage.keys() - {'root'}:
        raise NasError('Unknown [storage] configuration setting; see README.md.')
    if samba.keys() - {'config', 'user'}:
        raise NasError('Unknown [samba] configuration setting; see README.md.')
    if rsync.keys() - {'host', 'port', 'user', 'password', 'root'}:
        raise NasError('Unknown [rsync] configuration setting; see README.md.')

    root = _absolute(_text(storage, 'root', '/mnt/data'), 'storage.root')
    if root == Path('/'):
        raise NasError("The shares root must not be '/'.")
    samba_path = _absolute(_text(samba, 'config', '/etc/samba/smb.conf'), 'config')
    samba_user = _text(samba, 'user')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*\$?', samba_user):
        raise NasError("'samba.user' must be a single Linux username.")

    host = _text(rsync, 'host')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\]', host):
        raise NasError("'host' must be a hostname, IPv4 address, or bracketed IPv6 address.")
    user = _text(rsync, 'user')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*\$?', user):
        raise NasError("'user' must be an SSH username.")
    port = rsync.get('port', 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise NasError("'port' must be an SSH port number from 1 to 65535.")
    password = _text(rsync, 'password', '')
    if password and (
        permissions.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        or permissions.st_uid not in {0, os.geteuid()}
    ):
        raise NasError(f"Configuration with passwords must be owned by you or root and private: chmod 600 {path}")
    remote_root = _text(rsync, 'root', '')
    if (remote_root and (remote_root.startswith('/')
            or any(part in {'', '.', '..'} for part in remote_root.split('/'))
            or any(c in remote_root for c in '\\:%'))):
        raise NasError("'rsync.root' must be empty or an rsync module and optional subfolders, such as NetBackup.")

    return Settings(
        root=root,
        samba_config=samba_path,
        samba_user=samba_user,
        rsync=RsyncSettings(
            host=host, port=port, user=user, password=password, root=remote_root,
        ),
    )
