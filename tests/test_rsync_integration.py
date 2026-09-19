"""Exercise real rsync over local pipes; no network or real SSH connection."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sambasync.common import RsyncSettings, Settings, Share
from sambasync.sync import sync_share


@unittest.skipUnless(shutil.which('rsync'), 'rsync executable is not installed')
class LocalRsyncIntegrationTests(unittest.TestCase):
    def test_mirror_and_dry_run_with_spaces_and_ssh_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'data'
            source = root / 'virtual disks'
            source.mkdir(parents=True)
            (source / 'keep me.txt').write_text('current data\n')
            backup = base / 'backup'
            destination = backup / 'Virtual Disks'
            destination.mkdir(parents=True)
            (destination / 'stale.txt').write_text('old data\n')
            daemon_config = base / 'rsyncd.conf'
            credentials = 'uid = 0\ngid = 0\n' if os.geteuid() == 0 else ''
            daemon_config.write_text(
                f'use chroot = no\n{credentials}'
                f'[NetBackup]\npath = {backup}\nread only = no\n'
            )
            bin_dir = base / 'bin'
            bin_dir.mkdir()
            fake_ssh = bin_dir / 'ssh'
            executable = shutil.which('rsync')
            fake_ssh.write_text(
                f'#!{sys.executable}\nimport os, sys\n'
                'assert sys.argv[sys.argv.index("-l") + 1] == "backup-user", sys.argv\n'
                f'os.execv({executable!r}, [{executable!r}, "--server", "--daemon", '
                f'{"--config=" + str(daemon_config)!r}, "."])\n'
            )
            fake_ssh.chmod(0o700)
            # Stand in for sshpass and SSH login; the rsync processes and
            # transfer remain real, while no network connection is made.
            fake_sshpass = bin_dir / 'sshpass'
            fake_sshpass.write_text(
                f'#!{sys.executable}\nimport os, sys\n'
                'assert sys.argv[1] == "-d"\n'
                'fd = int(sys.argv[2])\n'
                'assert os.read(fd, 100) == b"ssh password\\n"\n'
                'os.close(fd)\n'
                'os.execvp(sys.argv[3], sys.argv[3:])\n'
            )
            fake_sshpass.chmod(0o700)
            settings = Settings(
                root, base / 'smb.conf', 'local-user',
                RsyncSettings('backup.example.test', 2233, 'backup-user',
                              'ssh password', 'NetBackup'),
            )
            share = Share('Virtual Disks', str(source), 'local-user')
            real_run = subprocess.run

            def captured_run(command, **kwargs):
                result = real_run(command, **kwargs, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            with patch.dict(os.environ, {'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}), \
                    patch('sambasync.sync.subprocess.run', side_effect=captured_run):
                self.assertEqual(sync_share(settings, share, dry_run=True), 0)
                self.assertTrue((destination / 'stale.txt').is_file())
                self.assertFalse((destination / 'keep me.txt').exists())
                self.assertEqual(sync_share(settings, share), 0)
                self.assertFalse((destination / 'stale.txt').exists())
                self.assertEqual((destination / 'keep me.txt').read_text(), 'current data\n')


if __name__ == '__main__':
    unittest.main()
