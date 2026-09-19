from dataclasses import replace
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sambasync.common import NasError, RsyncSettings, Settings, Share
from sambasync.sync import check_source, remote_destination, sync_share, validate_local_path


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.root.mkdir()
        self.directory = self.root / "Virtual Disks"
        self.directory.mkdir()
        (self.directory / "disk.img").write_text("data", encoding="utf-8")
        self.share = Share("Virtual Disks", str(self.directory), "jasper")
        self.settings = Settings(
            self.root, Path(self.temp.name) / "smb.conf", "jasper",
            RsyncSettings("johannessy.jappa.nl", 2233, "jasper", "", "NetBackup"),
        )

    def change_rsync(self, **kwargs):
        self.settings = replace(self.settings, rsync=replace(self.settings.rsync, **kwargs))

    def test_relative_and_absolute_paths_with_spaces(self):
        self.assertEqual(validate_local_path(self.root, "Virtual Disks"), self.directory)
        self.assertEqual(validate_local_path(self.root, str(self.directory)), self.directory)
        self.assertEqual(validate_local_path(self.root, "new folder/nested"), self.root / "new folder/nested")

    def test_unsafe_local_paths_rejected(self):
        for value in ("", " ", ".", "..", "../outside", "a/../b", "/etc", str(self.root), "%U", "folder\nname", "a\\b"):
            with self.subTest(value=value), self.assertRaises(NasError):
                validate_local_path(self.root, value)

    def test_symlink_components_rejected_even_when_target_is_inside_root(self):
        link = self.root / "link"
        link.symlink_to(self.directory, target_is_directory=True)
        for value in (str(link), str(link / "nested")):
            with self.subTest(value=value), self.assertRaisesRegex(NasError, "symbolic link"):
                validate_local_path(self.root, value)
        link.unlink()
        link.symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaises(NasError):
            validate_local_path(self.root, str(link))

    def test_remote_path_always_uses_share_name(self):
        self.assertEqual(remote_destination(self.settings, self.share), "jasper@johannessy.jappa.nl::NetBackup/Virtual Disks/")
        self.change_rsync(root="NetBackup/NAS/")
        self.assertEqual(remote_destination(self.settings, self.share), "jasper@johannessy.jappa.nl::NetBackup/NAS/Virtual Disks/")

    @patch("sambasync.sync.subprocess.run")
    def test_empty_root_uses_share_name_as_module(self, run):
        self.change_rsync(root="")
        run.return_value = subprocess.CompletedProcess([], 0)
        self.assertEqual(sync_share(self.settings, self.share), 0)
        self.assertEqual(run.call_args.args[0][-1], "jasper@johannessy.jappa.nl::Virtual Disks/")

    def test_unsafe_remote_paths_rejected(self):
        for value in ("", "/", "/root", "..", "../other", "a/../b", "a//b", ".", "*", "a?b", "[a]", "name\n", "mod:path", "a\\b"):
            with self.subTest(value=value), self.assertRaises(NasError):
                remote_destination(self.settings, replace(self.share, name=value))

    def test_remote_host_and_user_cannot_inject_options(self):
        for field, value in (("host", "-oProxyCommand=bad"), ("host", "host name"), ("host", "host;cmd"), ("user", "name@other")):
            settings = replace(self.settings, rsync=replace(self.settings.rsync, **{field: value}))
            with self.subTest(field=field, value=value), self.assertRaises(NasError):
                remote_destination(settings, self.share)
        self.change_rsync(host="[2001:db8::1]")
        self.assertIn("@[2001:db8::1]::", remote_destination(self.settings, self.share))

    def test_missing_empty_and_file_sources_rejected(self):
        empty = self.root / "empty"
        empty.mkdir()
        for path in (self.root / "missing", empty, self.directory / "disk.img"):
            with self.subTest(path=path), self.assertRaises(NasError):
                check_source(self.settings, replace(self.share, path=str(path)))

    def test_hidden_file_counts_as_nonempty(self):
        (self.directory / "disk.img").rename(self.directory / ".hidden")
        self.assertEqual(check_source(self.settings, self.share), self.directory)

    @patch("sambasync.sync.subprocess.run")
    def test_argv_preserves_spaces_and_returns_rsync_status(self, run):
        run.return_value = subprocess.CompletedProcess([], 23)
        self.assertEqual(sync_share(self.settings, self.share, dry_run=True), 23)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["rsync", "-avh", "--delete", "--info=progress2", "--protect-args"])
        self.assertIn("--dry-run", command)
        self.assertEqual(shlex.split(command[command.index("-e") + 1]), ["ssh", "-p", "2233", "-l", "jasper"])
        self.assertEqual(command[-3:], ["--", f"{self.directory}/", "jasper@johannessy.jappa.nl::NetBackup/Virtual Disks/"])
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("sambasync.sync.subprocess.run")
    def test_password_is_passed_only_to_ssh_and_descriptor_is_closed(self, run):
        self.change_rsync(password="ssh secret", user="ssh-login")
        descriptors = []

        def inspect_call(command, **kwargs):
            self.assertEqual(command[:2], ["sshpass", "-d"])
            self.assertNotIn("ssh secret", repr(command))
            self.assertNotIn("env", kwargs)
            self.assertNotIn("--password-file", command)
            fd = int(command[2])
            descriptors.append(fd)
            self.assertEqual(kwargs["pass_fds"], (fd,))
            self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
            self.assertEqual(os.read(fd, 100), b"ssh secret\n")
            self.assertEqual(shlex.split(command[command.index("-e") + 1]),
                             ["ssh", "-p", "2233", "-l", "ssh-login"])
            return subprocess.CompletedProcess(command, 0)

        run.side_effect = inspect_call
        self.assertEqual(sync_share(self.settings, self.share), 0)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    @patch("sambasync.sync.subprocess.run")
    def test_password_descriptor_closed_after_failed_spawn(self, run):
        self.change_rsync(password="secret")
        descriptors = []

        def fail_spawn(command, **kwargs):
            descriptors.append(int(command[2]))
            raise FileNotFoundError("rsync")

        run.side_effect = fail_spawn
        with self.assertRaises(NasError) as error:
            sync_share(self.settings, self.share)
        self.assertNotIn("secret", str(error.exception))
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    @patch("sambasync.sync.subprocess.run")
    def test_empty_password_uses_normal_ssh_authentication(self, run):
        run.return_value = subprocess.CompletedProcess([], 0)
        sync_share(self.settings, self.share)
        command = run.call_args.args[0]
        self.assertEqual(command[0], 'rsync')
        self.assertNotIn('--password-file', command)
        self.assertNotIn('sshpass', command)
        self.assertEqual(run.call_args.kwargs['pass_fds'], ())

    @patch("sambasync.sync.subprocess.run")
    def test_invalid_secrets_and_port_fail_before_external_calls(self, run):
        for overrides in ({"password": "secret\nsecond"}, {"password": "bad\0secret"}, {"port": 0}, {"port": True}, {"user": "bad@user"}):
            settings = replace(self.settings, rsync=replace(self.settings.rsync, **overrides))
            with self.subTest(overrides=overrides), self.assertRaises(NasError):
                sync_share(settings, self.share)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
