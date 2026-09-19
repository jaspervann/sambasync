"""End-to-end CLI tests using temporary shares and no live NAS services."""

from __future__ import annotations

from contextlib import chdir, redirect_stderr, redirect_stdout
import getpass
import io
import json
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from sambasync import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "data"
        self.root.mkdir()
        self.jasper = self.root / "jasper"
        self.vdisk = self.root / "vdisk"
        self.jasper.mkdir()
        self.vdisk.mkdir()
        (self.jasper / "keep.txt").write_text("irreplaceable data\n")
        (self.vdisk / "disk.img").write_text("disk data\n")
        self.smb = self.base / "smb.conf"
        self.global_bytes = (
            b"# Existing administrator settings\r\n"
            b"[global]\r\n"
            b"   workgroup = WORKGROUP\r\n"
            b"   server role = standalone server\r\n"
            b"   security = user\r\n"
            b"   map to guest = never\r\n"
            b"   server min protocol = SMB2\r\n\r\n"
        )
        share_text = (
            "[Jasper]\r\n"
            f"   path = {self.jasper}\r\n"
            "   browseable = yes\r\n"
            "   read only = no\r\n"
            "   guest ok = no\r\n"
            "   valid users = jasper\r\n\r\n"
            "[Virtual Disks]\r\n"
            f"   path = {self.vdisk}\r\n"
            "   browseable = yes\r\n"
            "   read only = no\r\n"
            "   guest ok = no\r\n"
            "   valid users = jasper\r\n"
        )
        self.smb.write_bytes(self.global_bytes + share_text.encode())
        self.initial_config = self.smb.read_bytes()
        self.config = self.base / "settings.toml"
        self.launcher = self.base / "smbsync"
        self.user = getpass.getuser()
        self.config.write_text(
            "[storage]\n"
            f"root = {json.dumps(str(self.root))}\n"
            "[samba]\n"
            f"config = {json.dumps(str(self.smb))}\n"
            f"user = {json.dumps(self.user)}\n"
            "[rsync]\n"
            "host = 'backup.example.test'\n"
            "port = 2233\n"
            "user = 'jasper'\n"
            "password = 'not-for-cli-output'\n"
            "root = 'NetBackup'\n"
        )
        self.config.chmod(0o600)
        samba_run = mock.patch("sambasync.samba.subprocess.run",
                               return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.samba_run = samba_run.start()
        self.addCleanup(samba_run.stop)

    def invoke(self, *args, answers=()):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            mock.patch("builtins.input", side_effect=answers),
            mock.patch.object(cli.sys, "argv", [str(self.launcher)]),
        ):
            status = cli.main(list(args))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_help_lists_commands_without_redundant_metavar_row(self):
        help_text = cli.parser().format_help()
        self.assertIn('usage: smbsync [-h] COMMAND ...', help_text)
        self.assertIn('\ncommands:\n  list ', help_text)
        self.assertNotIn('{list,add,delete,sync}', help_text)
        self.assertNotIn('\n  COMMAND\n', help_text)

    def test_config_is_beside_launcher_not_in_working_directory(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'settings.toml').write_text('invalid TOML')
        with chdir(elsewhere):
            status, stdout, stderr = self.invoke('list')
        self.assertEqual(status, 0, stderr)
        self.assertIn(str(self.jasper), stdout)

    def test_symlink_launcher_uses_config_beside_real_script(self):
        self.launcher.touch()
        links = self.base / 'bin'
        links.mkdir()
        link = links / 'smbsync'
        link.symlink_to(self.launcher)
        self.launcher = link
        status, stdout, stderr = self.invoke('list')
        self.assertEqual(status, 0, stderr)
        self.assertIn(str(self.jasper), stdout)

    def test_list_discovers_existing_samba_shares_and_reports_paths(self):
        status, stdout, stderr = self.invoke("list")
        self.assertEqual(status, 0, stderr)
        self.assertTrue(stdout.startswith('\nJasper\n'))
        self.assertIn('  Users:  jasper\n\nVirtual Disks\n', stdout)
        self.assertTrue(stdout.endswith('  Users:  jasper\n\n'))
        for expected in (
            "Jasper",
            "Virtual Disks",
            str(self.jasper),
            str(self.vdisk),
            "backup.example.test",
            "NetBackup",
        ):
            self.assertIn(expected, stdout)
        self.assertNotIn("not-for-cli-output", stdout + stderr)
        self.assertNotIn('ready', stdout)
        self.assertNotIn('Access:', stdout)
        self.assertEqual(self.smb.read_bytes(), self.initial_config)

    def test_list_rejects_json_option(self):
        with self.assertRaises(SystemExit) as caught:
            self.invoke('list', '--json')
        self.assertEqual(caught.exception.code, 2)

    def test_wrong_delete_confirmation_preserves_configuration_and_files(self):
        status, stdout, stderr = self.invoke("delete", "Jasper", answers=["jasper"])
        self.assertNotEqual(status, 0, stdout + stderr)
        self.assertEqual(self.smb.read_bytes(), self.initial_config)
        self.assertEqual((self.jasper / "keep.txt").read_text(), "irreplaceable data\n")

    def test_unknown_share_does_not_change_configuration(self):
        status, stdout, stderr = self.invoke("delete", "Missing")
        self.assertNotEqual(status, 0, stdout + stderr)
        self.assertEqual(self.smb.read_bytes(), self.initial_config)
        self.assertTrue(self.jasper.is_dir())

    def test_delete_keeps_local_files_by_default_and_preserves_globals(self):
        status, stdout, stderr = self.invoke("delete", "Jasper", answers=["Jasper", ""])
        self.assertEqual(status, 0, stdout + stderr)
        remaining = self.smb.read_bytes()
        self.assertTrue(remaining.startswith(self.global_bytes))
        self.assertNotIn(b"[Jasper]", remaining)
        self.assertIn(b"[Virtual Disks]", remaining)
        self.assertEqual((self.jasper / "keep.txt").read_text(), "irreplaceable data\n")
        self.assertTrue((self.vdisk / "disk.img").is_file())

    def test_delete_data_flag_removes_only_confirmed_local_share(self):
        status, stdout, stderr = self.invoke(
            "delete", "Jasper", "--delete-data", answers=["Jasper"]
        )
        self.assertEqual(status, 0, stdout + stderr)
        self.assertFalse(self.jasper.exists())
        self.assertTrue((self.vdisk / "disk.img").is_file())
        self.assertTrue(self.smb.read_bytes().startswith(self.global_bytes))
        self.assertNotIn(b"[Jasper]", self.smb.read_bytes())

    def test_delete_data_flag_cannot_bypass_name_confirmation(self):
        status, stdout, stderr = self.invoke(
            "delete", "Jasper", "--delete-data", answers=["yes"]
        )
        self.assertNotEqual(status, 0, stdout + stderr)
        self.assertTrue((self.jasper / "keep.txt").is_file())
        self.assertEqual(self.smb.read_bytes(), self.initial_config)

    def test_interactive_data_deletion_requires_second_name_confirmation(self):
        status, stdout, stderr = self.invoke(
            "delete", "Jasper", answers=["Jasper", "y", "wrong"]
        )
        self.assertNotEqual(status, 0, stdout + stderr)
        self.assertTrue((self.jasper / "keep.txt").is_file())
        self.assertEqual(self.smb.read_bytes(), self.initial_config)

    def test_interactive_data_deletion_removes_confirmed_share(self):
        status, stdout, stderr = self.invoke(
            "delete", "Jasper", answers=["Jasper", "y", "Jasper"]
        )
        self.assertEqual(status, 0, stdout + stderr)
        self.assertFalse(self.jasper.exists())
        self.assertTrue((self.vdisk / "disk.img").is_file())

    def test_sync_propagates_rsync_exit_status(self):
        with mock.patch.object(cli, "sync_share", return_value=23) as sync:
            status, stdout, stderr = self.invoke("sync", "Virtual Disks")
        self.assertEqual(status, 23, stdout + stderr)
        sync.assert_called_once()
        self.assertEqual(sync.call_args.args[1].name, "Virtual Disks")
        self.assertFalse(sync.call_args.kwargs.get("dry_run", False))
        self.assertEqual(self.smb.read_bytes(), self.initial_config)

    def test_sync_dry_run_is_forwarded(self):
        with mock.patch.object(cli, "sync_share", return_value=0) as sync:
            status, stdout, stderr = self.invoke("sync", "Jasper", "--dry-run")
        self.assertEqual(status, 0, stdout + stderr)
        sync.assert_called_once()
        self.assertTrue(sync.call_args.kwargs["dry_run"])

    def test_sync_all_runs_shares_in_order_with_dry_run(self):
        with mock.patch.object(cli, "sync_share", return_value=0) as sync:
            status, stdout, stderr = self.invoke("sync", "--all", "--dry-run")
        self.assertEqual(status, 0, stdout + stderr)
        self.assertEqual([call.args[1].name for call in sync.call_args_list],
                         ["Jasper", "Virtual Disks"])
        self.assertTrue(all(call.kwargs["dry_run"] for call in sync.call_args_list))
        self.assertLess(stdout.index("Previewing 'Jasper'"), stdout.index("Previewing 'Virtual Disks'"))

    def test_sync_all_continues_after_rsync_failure(self):
        with mock.patch.object(cli, "sync_share", side_effect=[23, 0]) as sync:
            status, stdout, stderr = self.invoke("sync", "--all")
        self.assertEqual(status, 23)
        self.assertEqual(sync.call_count, 2)
        self.assertIn("'Jasper': rsync failed with exit status 23", stderr)
        self.assertIn("Syncing 'Virtual Disks'", stdout)

    def test_sync_all_continues_after_invalid_share(self):
        self.smb.write_bytes(self.initial_config.replace(
            str(self.jasper).encode(), str(self.base / "outside").encode()
        ))
        with mock.patch.object(cli, "sync_share", return_value=0) as sync:
            status, stdout, stderr = self.invoke("sync", "--all")
        self.assertEqual(status, 1)
        self.assertEqual([call.args[1].name for call in sync.call_args_list], ["Virtual Disks"])
        self.assertIn("'Jasper': Local path must be strictly inside", stderr)

    def test_sync_requires_exactly_one_selection(self):
        for arguments in ((), ("Jasper", "--all")):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as caught:
                self.invoke("sync", *arguments)
            self.assertEqual(caught.exception.code, 2)

    def test_sync_unknown_share_does_not_invoke_rsync(self):
        with mock.patch.object(cli, "sync_share") as sync:
            status, stdout, stderr = self.invoke("sync", "Missing")
        self.assertNotEqual(status, 0, stdout + stderr)
        sync.assert_not_called()

    def test_validator_failure_keeps_original_configuration_and_files(self):
        self.samba_run.return_value = subprocess.CompletedProcess([], 1, "", "invalid configuration")
        status, stdout, stderr = self.invoke(
            "delete", "Jasper", "--delete-data", answers=["Jasper"]
        )
        self.assertNotEqual(status, 0, stdout + stderr)
        self.assertEqual(self.smb.read_bytes(), self.initial_config)
        self.assertTrue((self.jasper / "keep.txt").is_file())

    def test_add_creates_share_with_matching_remote_name(self):
        status, stdout, stderr = self.invoke(
            "add", "Family Photos",
            "--path", "family photos",
        )
        self.assertEqual(status, 0, stdout + stderr)
        folder = self.root / "family photos"
        self.assertTrue(folder.is_dir())
        info = folder.stat()
        owner = pwd.getpwnam(self.user)
        self.assertEqual(info.st_uid, owner.pw_uid)
        self.assertEqual(info.st_gid, owner.pw_gid)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o2770)
        self.assertTrue(self.smb.read_bytes().startswith(self.global_bytes))
        self.assertIn(b"[Jasper]", self.smb.read_bytes())
        self.assertIn(b"[Virtual Disks]", self.smb.read_bytes())
        added = cli.SambaDocument(self.smb.read_bytes()).get('Family Photos')
        self.assertEqual(added.path, str(folder))
        self.assertEqual(added.valid_users, self.user)
        self.assertNotIn(b'nas-share remote', self.smb.read_bytes())
        status, stdout, stderr = self.invoke('list')
        self.assertEqual(status, 0, stderr)
        self.assertIn('jasper@backup.example.test::NetBackup/Family Photos/', stdout)

    def test_add_existing_folder_preserves_ownership_permissions_and_files(self):
        folder = self.root / "photos"
        folder.mkdir(mode=0o751)
        folder.chmod(0o751)
        photo = folder / "photo.jpg"
        photo.write_bytes(b"existing photo")
        before = folder.stat()
        status, stdout, stderr = self.invoke(
            "add", "Photos", "--path", "photos"
        )
        self.assertEqual(status, 0, stdout + stderr)
        after = folder.stat()
        self.assertEqual(after.st_uid, before.st_uid)
        self.assertEqual(after.st_gid, before.st_gid)
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
        self.assertEqual(photo.read_bytes(), b"existing photo")

    def test_add_rejects_removed_options(self):
        for option in ("--users", "--owner", "--group", "--writable", "--read-only", "--remote-path"):
            values = ["jasper"] if option in {"--users", "--owner", "--group", "--remote-path"} else []
            with self.subTest(option=option), self.assertRaises(SystemExit) as caught:
                self.invoke("add", "Photos", option, *values)
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(self.smb.read_bytes(), self.initial_config)
            self.assertFalse((self.root / "photos").exists())

    def test_add_rejects_path_outside_share_root(self):
        status, stdout, stderr = self.invoke(
            "add", "Outside",
            "--path", str(self.base / "outside"),
        )
        self.assertNotEqual(status, 0, stdout + stderr)
        self.assertEqual(self.smb.read_bytes(), self.initial_config)
        self.assertFalse((self.base / "outside").exists())

    def test_add_collects_missing_fields_interactively(self):
        status, stdout, stderr = self.invoke('add', answers=['Photos', 'photos'])
        self.assertEqual(status, 0, stdout + stderr)
        self.assertTrue((self.root / 'photos').is_dir())
        added = cli.SambaDocument(self.smb.read_bytes()).get('Photos')
        self.assertEqual(added.valid_users, self.user)
        self.assertIn('jasper@backup.example.test::NetBackup/Photos/', stdout)

    def test_delete_data_refuses_shared_or_nested_local_directory(self):
        nested = self.jasper / "nested"
        nested.mkdir()
        (nested / "keep-too.txt").write_text("another share's data\n")
        for other_path in (self.jasper, nested):
            with self.subTest(other_path=other_path):
                overlapping = self.initial_config.replace(
                    str(self.vdisk).encode(), str(other_path).encode()
                )
                self.smb.write_bytes(overlapping)
                status, stdout, stderr = self.invoke(
                    "delete", "Jasper", "--delete-data", answers=["Jasper"]
                )
                self.assertNotEqual(status, 0, stdout + stderr)
                self.assertEqual(self.smb.read_bytes(), overlapping)
                self.assertTrue((self.jasper / "keep.txt").is_file())
                self.assertTrue((nested / "keep-too.txt").is_file())

    def test_legacy_remote_comment_does_not_override_share_name(self):
        original = self.initial_config.replace(
            b'[Virtual Disks]\r\n',
            b'[Virtual Disks]\r\n   # nas-share remote = "Jasper"\r\n',
        )
        self.smb.write_bytes(original)
        with mock.patch.object(cli, 'sync_share', return_value=0) as sync:
            status, stdout, stderr = self.invoke('sync', 'Virtual Disks')
        self.assertEqual(status, 0, stderr)
        sync.assert_called_once()
        self.assertIn('jasper@backup.example.test::NetBackup/Virtual Disks/', stdout)
        self.assertEqual(self.smb.read_bytes(), original)

    def test_add_refuses_existing_share_name(self):
        status, stdout, stderr = self.invoke('add', 'Jasper', '--path', 'other')
        self.assertNotEqual(status, 0, stdout + stderr)
        self.assertFalse((self.root / 'other').exists())
        self.assertEqual(self.smb.read_bytes(), self.initial_config)


if __name__ == "__main__":
    unittest.main()
