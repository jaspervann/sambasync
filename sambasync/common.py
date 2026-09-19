from dataclasses import dataclass, field
from pathlib import Path


class NasError(Exception):
    """An actionable error suitable for display without a traceback."""


@dataclass(frozen=True)
class Share:
    name: str
    path: str
    valid_users: str = ""
    browseable: bool = True


@dataclass(frozen=True)
class RsyncSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    root: str


@dataclass(frozen=True)
class Settings:
    root: Path
    samba_config: Path
    samba_user: str
    rsync: RsyncSettings
