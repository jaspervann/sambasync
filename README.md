# SambaSync (`smbsync`)

A Unix CLI for managing local Samba shares and mirroring them to a backup server with rsync.

## Requirements

Requires Python 3.11 or newer. There is no Python package installation or third-party Python dependency.

Install any missing system tools:

```bash
sudo apt install python3 samba rsync openssh-client
```

If you configure an SSH password, also install `sshpass`:

```bash
sudo apt install sshpass
```

Samba must already be configured, and the configured Samba user must exist in Unix and Samba. The remote SSH account is expected to have write access to the rsync backup destination.

## Run

Ensure proper permissions:

```bash
chmod +x ./smbsync
chmod 600 ./settings.toml
```

Run the script directly from this folder after editing the configuration in `settings.toml`:

```bash
./smbsync list
```

`sudo` is not necessarily required by the CLI. `list` needs read access to `settings.toml` and `smb.conf`. `sync` also needs access to the share files and permission to create a lock beside `smb.conf`. `add` and `delete` need permission to change and reload Samba configuration; adding a folder or deleting local data also needs the corresponding filesystem permissions. The examples below use `sudo` because typical Samba installations restrict these files and directories.

Existing shares are read from `smb.conf`. Adding or deleting a share preserves existing global sections, unrelated definitions, comments and line endings.

## Configuration

Create or edit `settings.toml` in this folder. The CLI always reads that file in the same directory as the real `smbsync` script, even when invoked from another working directory or through a symlink.

| Setting | Meaning |
| --- | --- |
| `storage.root` | Root folder of all shares on local storage. Must be an absolute path; defaults to `/mnt/data`. It may be a regular folder or a mount point. |
| `samba.config` | Existing Samba configuration file. Defaults to `/etc/samba/smb.conf`. |
| `samba.user` | Required Unix/Samba username granted access to new shares. Controls ownership of new folders. |
| `rsync.host` | Required backup hostname or IP address, without a URL or path; enclose IPv6 addresses in brackets. |
| `rsync.port` | SSH port, not the rsync daemon port. Defaults to `22`. |
| `rsync.user` | Required SSH login username. |
| `rsync.password` | SSH password. Empty or omitted uses normal SSH authentication, such as keys or an interactive prompt. |
| `rsync.root` | Root rsync module for all shares on the backup destination, optionally followed by existing subfolders, like `NetBackup/Photos`. Empty or omitted uses each share name as its own module. Defaults to `""`. |

The connection uses an **rsync module over SSH**. Each share module must already exist on the remote server.

A configured password is supplied to SSH through `sshpass` using a private file descriptor, never a command-line argument. A file containing a password must have no group/other permissions and be owned by the invoking user or root. When running with `sudo`, that means root ownership; mode `600` is suitable. `settings.toml` is ignored by version control.

SSH uses the invoking account's keys and known hosts, normally root's when using `sudo`. The CLI does not disable host-key verification.

## Commands

| Command | Purpose |
| --- | --- |
| `./smbsync list` | Show all shares and their local/remote paths. |
| `./smbsync add [name] [--path folder]` | Add a share, prompting for omitted values. |
| `./smbsync delete name [--delete-data]` | Remove a share after confirmation; optionally delete local files. |
| `./smbsync sync name [--dry-run]` | Mirror one share, or preview the transfer. |
| `./smbsync sync --all [--dry-run]` | Mirror every share sequentially, or preview every transfer. |

Use `./smbsync --help` or `./smbsync <command> --help` for help.

Quote names and paths containing spaces.

## List shares

```bash
./smbsync list
```

Shows each share's name, local path, remote destination, and allowed Samba users. Existing definitions are discovered automatically; there is no import step or separate share database.

With the example configuration above, `./smbsync list` prints:

```text
Documents
  Local:  /mnt/data/docs
  Remote: johnny@x4g7d3.quickconnect.synology.com::Documents/
  Users:  john

Virtual Disks
  Local:  /mnt/data/vdisks
  Remote: johnny@x4g7d3.quickconnect.synology.com::Virtual Disks/
  Users:  john
```

Listing does not check whether the local folder exists or connect to the backup server.

## Add a share

```bash
sudo ./smbsync add
sudo ./smbsync add 'Family Photos' --path photos
```

The interactive command asks for the share name and local folder. The suggested folder name is derived from the share name; for example, `Family Photos` suggests `family-photos`. With `storage.root` = `/mnt/data`, `--path photos` points to `/mnt/data/photos`. You may also supply an absolute path like `--path /mnt/data/photos`. Either way, the folder must be inside `storage.root`.

The second example shares `/mnt/data/photos` and backs it up to the `Family Photos` module by default. With `root = "NetBackup"`, it uses `NetBackup/Family Photos/` instead.

New shares are browseable, writeable, have guest access disabled and are restricted to `samba.user`. New folders are owned by that user and its primary group, with mode `2770`. The parent folder must already exist. Existing folders keep their ownership, permissions, and contents.

The CLI refuses duplicate share names, overlapping paths, symlink paths, and paths outside the shares root. Adding a share does not perform a sync.

## Delete a share

```bash
sudo ./smbsync delete 'Virtual Disks'
```

Type the exact share name to confirm. The CLI then asks whether to permanently delete the local folder and its contents; the default is **no**. Choosing yes requires typing the share name again. A mismatched confirmation or cancellation during these prompts leaves everything unchanged.

To request local-file deletion up front:

```bash
sudo ./smbsync delete 'Virtual Disks' --delete-data
```

This still requires typing the exact share name. The CLI removes and reloads the Samba definition before deleting local files. File deletion is refused if the folder overlaps another share or contains a mount point. If deletion fails after Samba was updated, the error reports that the share was removed and remaining files need inspection.

Remote backups are always kept when deleting a share. Without local-file deletion, the local folder is also kept.

## Sync shares

```bash
sudo ./smbsync sync Documents --dry-run
sudo ./smbsync sync Documents
sudo ./smbsync sync 'Virtual Disks'
sudo ./smbsync sync --all --dry-run
sudo ./smbsync sync --all
```

Sync runs `rsync -avh --delete --info=progress2 --protect-args` over SSH. Trailing slashes copy the source folder's contents into its remote share folder.

**Files removed locally are deleted from the remote mirror.** `--dry-run` previews transfers and deletions without changing remote files, but still connects and authenticates to the server.

Sync refuses a missing or empty source folder, symlink paths, paths outside the root, or overlapping share/backup paths. Hidden files count as contents. These checks happen before the transfer; sync does not create a filesystem snapshot. If `storage.root` is a mount point, ensure it is mounted before syncing; a nonempty underlying folder can otherwise be mistaken for the intended source.

Rsync progress is streamed to the terminal. With `--all`, all shares run one at a time in order of `smb.conf`. A failed share does not prevent later shares from running. The CLI returns the first failure status, or zero if every share succeeds. Sync runs only when invoked; no schedule is installed. To schedule syncs, you may add it to your crontab by executing the following from this folder:

```bash
(crontab -l 2>/dev/null; echo "0 4 * * * $(readlink -f ./smbsync) sync --all") | crontab -
```

This preserves your existing cron jobs, adds a daily 04:00 job for `smbsync sync --all`, and saves the updated crontab. `readlink -f ./smbsync` converts the local script path to its absolute path. The job runs as the crontab owner, who needs the same file permissions and SSH access as an interactive sync.

## Samba changes

Add and delete automatically validate a candidate configuration with `testparm -s`, save a backup in the same directory as `smb.conf`, replace the configuration atomically, and run `smbcontrol all reload-config`. File mode and ownership are preserved. Backups contain the Samba configuration, not share data.

If reload fails, the CLI attempts to restore and reload the original configuration. It reports any recovery failure and the backup location. An external change detected during the operation is not overwritten.

Add, delete, and sync share a lock in the same directory as `smb.conf`. A second operation exits with an error while another is running. Listing does not take that lock.

Use a standalone UTF-8 `smb.conf` with explicit share paths. Includes, external configuration backends, copied share definitions, and continued share settings are refused. Symlinked configuration files cannot be replaced. Special sections such as `[homes]` and `[printers]` cannot be deleted as ordinary shares.

To edit an existing share, modify `smb.conf`, validate it with `testparm -s`, and reload Samba. The CLI reads the changes on its next invocation. Renaming a share changes its backup destination; existing remote folders or modules are not renamed.
