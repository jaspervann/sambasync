"""Interactive command line for Samba share management."""

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import sys

from .common import NasError, Settings, Share
from .config import load_config
from .samba import SambaDocument, change_config
from .sync import remote_destination, sync_share, validate_local_path


class HelpFormatter(argparse.HelpFormatter):
    """List subcommands directly, without argparse's redundant metavar row."""

    def _format_action(self, action):
        if isinstance(action, argparse._SubParsersAction):
            return ''.join(
                self._format_action(subaction)
                for subaction in action._choices_actions
            )
        return super()._format_action(action)


def prompt(label: str, default: str = '') -> str:
    suffix = f' [{default}]' if default else ''
    return input(f'{label}{suffix}: ').strip() or default


def yes_no(label: str, default: bool = False) -> bool:
    while True:
        reply = input(f"{label} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        if not reply:
            return default
        if reply in {'y', 'yes'}:
            return True
        if reply in {'n', 'no'}:
            return False
        print('Please answer yes or no.')


@contextmanager
def config_lock(path: Path):
    """Serialize mutations and syncs through an adjacent, stable lock file."""
    lock_path = path.with_name(path.name + '.smbsync.lock')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise NasError(f'Unsafe lock file: {lock_path}')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NasError('Another smbsync operation is running. Try again after it finishes.') from None
        yield
    finally:
        os.close(fd)


def read_samba(settings: Settings) -> tuple[bytes, SambaDocument]:
    data = settings.samba_config.read_bytes()
    return data, SambaDocument(data)


def validate_name(name: str) -> str:
    if (not name or name != name.strip() or len(name) > 80
            or any(ord(c) < 32 or ord(c) == 127 or c in '[]/\\:%*?"<>|' for c in name)
            or name.casefold() in {'global', 'homes', 'printers', 'print$', 'ipc$', '.', '..'}):
        raise NasError('Use a share name of 1–80 characters without reserved names or path punctuation.')
    return name


def overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def ensure_unique(settings: Settings, share: Share, shares: list[Share], *, remote: bool = True):
    local_path = validate_local_path(settings.root, share.path)
    remote_path = Path(remote_destination(settings, share).split('::', 1)[1].casefold()) if remote else None
    for existing in shares:
        if existing.name.casefold() == share.name.casefold():
            continue
        # Compare even out-of-root shares so deleting a parent cannot destroy one.
        if existing.path and Path(existing.path).is_absolute():
            other = Path(existing.path).resolve()
            if overlaps(local_path, other):
                raise NasError(f"Local path overlaps share '{existing.name}': {other}")
        if remote:
            other_remote = Path(remote_destination(settings, existing).split('::', 1)[1].casefold())
            if overlaps(remote_path, other_remote):
                raise NasError(f"Backup destination overlaps share '{existing.name}'. Choose a separate remote folder.")


def list_shares(settings: Settings) -> int:
    _, document = read_samba(settings)
    rows = []
    for share in document.shares():
        try:
            validate_local_path(settings.root, share.path)
            destination = remote_destination(settings, share)
        except NasError:
            destination = None
        rows.append(dict(name=share.name, local_path=share.path, remote_path=destination,
                         users=share.valid_users))
    if not rows:
        print('No Samba shares found.')
    else:
        for row in rows:
            print()
            print(row['name'])
            print(f"  Local:  {row['local_path'] or '(no static path)'}")
            print(f"  Remote: {row['remote_path'] or '(unavailable)'}")
            print(f"  Users:  {row['users'] or '(inherited from Samba)'}")
        print()
    return 0


def add_share(settings: Settings, args: argparse.Namespace) -> int:
    original, document = read_samba(settings)
    name = validate_name(args.name if args.name is not None else prompt('Share name'))
    if any(s.name.casefold() == name.casefold() for s in document.shares()):
        raise NasError(f"Share '{name}' already exists.")
    suggested = re.sub(r'[^a-z0-9_.-]+', '-', name.lower()).strip('-') or 'share'
    path = validate_local_path(settings.root, args.path if args.path is not None else prompt('Local folder (relative to shares root)', suggested))
    share = Share(name=name, path=str(path), valid_users=settings.samba_user)
    ensure_unique(settings, share, document.shares())
    updated = document.add(share)
    if path.exists() and not path.is_dir():
        raise NasError(f'Local path exists but is not a folder: {path}')
    if not path.parent.is_dir():
        raise NasError(f'Parent folder must already exist: {path.parent}')

    owner = None
    if not path.exists():
        owner_name = settings.samba_user
        try:
            owner = pwd.getpwnam(owner_name)
        except KeyError:
            raise NasError(f"Configured Samba owner '{owner_name}' is not a Linux user.") from None

    created = False
    try:
        # Recheck after interactive prompts and immediately before changing disk.
        validate_local_path(settings.root, str(path))
        if owner is not None:
            path.mkdir(mode=0o770)
            created = True
            os.chown(path, owner.pw_uid, owner.pw_gid)
            path.chmod(0o2770)
        backup = change_config(settings.samba_config, original, updated)
    except BaseException:
        if created:
            try:
                path.rmdir()  # Never remove data written concurrently.
            except OSError:
                pass
        raise
    print(f"Added '{name}'.\n  Local:  {path}\n  Remote: {remote_destination(settings, share)}\n  Samba backup: {backup}")
    return 0


def check_data_deletion(root: Path, path: Path):
    validate_local_path(root, str(path))
    if not path.is_dir():
        raise NasError(f'Local folder does not exist: {path}')
    # mountinfo includes bind mounts, which os.path.ismount can miss.
    try:
        mounts = Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError:
        raise NasError('Cannot inspect Linux mount points; refusing local-file deletion.') from None
    for line in mounts:
        fields = line.split()
        if len(fields) < 5:
            raise NasError('Cannot parse Linux mount points; refusing local-file deletion.')
        mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
        if mount == path or path in mount.parents:
            raise NasError(f'Refusing to delete a folder containing a mount point: {mount}')


def remove_local_data(root: Path, path: Path):
    check_data_deletion(root, path)
    if not shutil.rmtree.avoids_symlink_attacks:
        raise NasError('This Python platform does not support safe folder deletion.')
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        shutil.rmtree(path.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def delete_share(settings: Settings, args: argparse.Namespace) -> int:
    original, document = read_samba(settings)
    share = document.get(args.name)
    path = validate_local_path(settings.root, share.path)
    print(f"Remove Samba share '{share.name}'\n  Local: {path}\n  Remote backup will be kept.")
    if args.delete_data:
        print('All files in the local folder will be permanently deleted.')
    if input('Type the exact share name to confirm: ') != share.name:
        raise NasError('Confirmation did not match. Nothing changed.')
    delete_data = args.delete_data
    if not delete_data and yes_no('Also permanently delete the local folder and all its files?'):
        print(f'Permanently delete {path} and all its files.')
        if input('Type the exact share name again to confirm file deletion: ') != share.name:
            raise NasError('Confirmation did not match. Nothing changed.')
        delete_data = True
    if delete_data:
        ensure_unique(settings, share, document.shares(), remote=False)
        check_data_deletion(settings.root, path)
    updated = document.remove(share.name)
    backup = change_config(settings.samba_config, original, updated)
    if delete_data:
        try:
            remove_local_data(settings.root, path)
        except (OSError, NasError) as exc:
            raise NasError(f"Share removed, but local-file deletion failed: {exc}. Inspect {path}. Samba backup: {backup}") from None
    print(f"Removed '{share.name}'. Local files {'deleted' if delete_data else 'kept'}; remote backup kept.\nSamba backup: {backup}")
    return 0


def run_sync(settings: Settings, args: argparse.Namespace) -> int:
    _, document = read_samba(settings)
    shares = document.shares()
    selected = shares if args.all else [document.get(args.name)]
    status = 0
    for share in selected:
        try:
            ensure_unique(settings, share, shares)
            print(f"{'Previewing' if args.dry_run else 'Syncing'} '{share.name}' → {remote_destination(settings, share)}", flush=True)
            result = sync_share(settings, share, dry_run=args.dry_run)
            if result:
                print(f"'{share.name}': rsync failed with exit status {result}.", file=sys.stderr)
                if not status:
                    status = result
        except NasError as exc:
            if not args.all:
                raise
            print(f"'{share.name}': {exc}", file=sys.stderr)
            if not status:
                status = 1
    return status


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog='smbsync',
        description='Manage Samba shares and rsync backups.',
        formatter_class=HelpFormatter,
    )
    commands = result.add_subparsers(
        dest='command', required=True, title='commands', metavar='COMMAND',
    )
    commands.add_parser('list', help='List existing Samba shares and backup destinations')
    adding = commands.add_parser('add', help='Create a Samba share; prompt for missing settings')
    adding.add_argument('name', nargs='?')
    adding.add_argument('--path', help='Local folder, relative to shares root or absolute')
    deleting = commands.add_parser('delete', help='Remove a share, optionally deleting local files')
    deleting.add_argument('name')
    deleting.add_argument('--delete-data', action='store_true', help='Permanently delete local files after typing the share name')
    syncing = commands.add_parser('sync', help='Mirror one share (or all shares) to its rsync backup destination')
    syncing.add_argument('name', nargs='?')
    syncing.add_argument('--all', action='store_true', help='Sync every share sequentially in smb.conf order')
    syncing.add_argument('--dry-run', action='store_true', help='Preview transfers and deletions without changing the backup')
    return result


def config_path() -> Path:
    """Locate settings beside the real launcher, independent of the cwd."""
    launcher = Path(sys.argv[0]).resolve()
    return launcher.parent / 'settings.toml'


def main(argv: list[str] | None = None) -> int:
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if args.command == 'sync':
        if args.all and args.name is not None:
            command_parser.error('sync accepts either a share name or --all, not both')
        if not args.all and args.name is None:
            command_parser.error('sync requires a share name or --all')
    try:
        settings = load_config(config_path())
        if args.command == 'list':
            return list_shares(settings)
        with config_lock(settings.samba_config):
            return {'add': add_share, 'delete': delete_share, 'sync': run_sync}[args.command](settings, args)
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled.', file=sys.stderr)
        return 130
    except NasError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except OSError as exc:
        print(f'Error: {exc.strerror}' + (f' ({exc.filename})' if exc.filename else ''), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
