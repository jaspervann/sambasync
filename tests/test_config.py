from dataclasses import fields
from pathlib import Path
import tempfile
import unittest

from sambasync.common import NasError
from sambasync.config import load_config


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'settings.toml'
        self.text = (
            '[storage]\nroot = "/mnt/data"\n\n'
            '[samba]\nconfig = "/etc/samba/smb.conf"\nuser = "jasper"\n\n'
            '[rsync]\nhost = "johannessy.jappa.nl"\nport = 2233\n'
            'user = "jasper"\npassword = ""\nroot = "NetBackup"\n'
        )

    def load(self, text=None, mode=0o600):
        self.path.write_text(self.text if text is None else text)
        self.path.chmod(mode)
        return load_config(self.path)

    def test_config_matches_existing_server(self):
        settings = self.load()
        self.assertEqual(settings.root, Path('/mnt/data'))
        self.assertEqual(settings.samba_config, Path('/etc/samba/smb.conf'))
        self.assertEqual(settings.samba_user, 'jasper')
        self.assertEqual(settings.rsync.host, 'johannessy.jappa.nl')
        self.assertEqual(settings.rsync.port, 2233)
        self.assertEqual(settings.rsync.root, 'NetBackup')

    def test_empty_rsync_root_selects_share_modules(self):
        settings = self.load(self.text.replace('root = "NetBackup"', 'root = ""'))
        self.assertEqual(settings.rsync.root, '')

    def test_omitted_rsync_root_defaults_to_share_modules(self):
        settings = self.load(self.text.replace('root = "NetBackup"\n', ''))
        self.assertEqual(settings.rsync.root, '')

    def test_password_requires_private_config_and_is_not_in_repr(self):
        text = self.text.replace('password = ""', 'password = "private-secret"', 1)
        with self.assertRaisesRegex(NasError, 'private'):
            self.load(text, mode=0o644)
        settings = self.load(text)
        self.assertEqual(settings.rsync.password, 'private-secret')
        self.assertNotIn('private-secret', repr(settings))

    def test_invalid_toml_does_not_echo_secrets(self):
        with self.assertRaises(NasError) as caught:
            self.load('[rsync]\npassword = secret-that-is-not-quoted\n')
        self.assertNotIn('secret-that-is-not-quoted', str(caught.exception))

    def test_rsync_uses_one_user_and_password(self):
        settings = self.load(self.text.replace('password = ""', 'password = "ssh-secret"', 1))
        self.assertEqual(settings.rsync.user, 'jasper')
        self.assertEqual(settings.rsync.password, 'ssh-secret')
        self.assertEqual(
            {field.name for field in fields(settings.rsync)},
            {'host', 'port', 'user', 'password', 'root'},
        )
        self.assertNotIn('ssh-secret', repr(settings))

    def test_rejects_removed_rsync_settings(self):
        for key in ('ssh_user', 'ssh_password', 'password_file', 'identity_file', 'identify_file'):
            with self.subTest(key=key), self.assertRaisesRegex(NasError, r'Unknown \[rsync\]'):
                self.load(self.text + f'\n{key} = "removed-value"\n')

    def test_rejects_old_samba_config_key(self):
        with self.assertRaisesRegex(NasError, r'Unknown \[samba\]'):
            self.load(self.text.replace('config = ', 'config_file = ', 1))

    def test_rejects_invalid_or_misspelled_critical_settings(self):
        for old, new in (
            ('port = 2233', 'port = true'),
            ('port = 2233', 'port = 0'),
            ('port = 2233', 'prt = 2233'),
            ('root = "/mnt/data"', 'root = "/"'),
            ('root = "/mnt/data"', 'rot = "/mnt/data"'),
            ('[storage]\nroot = "/mnt/data"', 'storage = "invalid"'),
            ('[storage]\nroot = "/mnt/data"', 'root = "/mnt/data"'),
            ('root = "NetBackup"', 'root = "../NetBackup"'),
            ('host = "johannessy.jappa.nl"', 'host = "-oProxyCommand=command"'),
        ):
            with self.subTest(new=new), self.assertRaises(NasError):
                self.load(self.text.replace(old, new))


if __name__ == '__main__':
    unittest.main()
