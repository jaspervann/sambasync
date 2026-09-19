import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sambasync.common import NasError, Share
from sambasync.samba import SambaDocument, change_config


GLOBAL = b"# existing preamble\r\n[global]\r\n  workgroup = WORKGROUP\r\n  map to guest = never\r\n\r\n"
JASPER = b"[Jasper]\r\n  path = /mnt/data/jasper\r\n  valid users = jasper\r\n  read only = no\r\n\r\n"
DISKS = b"[Virtual Disks]\r\n  path = /mnt/data/vdisk\r\n  valid users = jasper\r\n\r\n"
LATER_GLOBAL = b"[global]\r\n  max log size = 1000\r\n"


class SambaDocumentTests(unittest.TestCase):
    def test_lists_existing_shares_with_spaces(self):
        shares = SambaDocument(GLOBAL + JASPER + DISKS).shares()
        self.assertEqual([share.name for share in shares], ["Jasper", "Virtual Disks"])
        self.assertEqual(shares[0].path, "/mnt/data/jasper")
        self.assertEqual(shares[0].valid_users, "jasper")

    def test_global_share_defaults_and_local_overrides(self):
        data = (b'[global]\n read only = no\n valid users = alice\n browseable = no\n'
                b'[Inherited]\n path = /mnt/data/inherited\n'
                b'[Override]\n path = /mnt/data/override\n read only = yes\n valid users = jasper\n browseable = yes\n')
        inherited, override = SambaDocument(data).shares()
        self.assertFalse(inherited.browseable)
        self.assertEqual(inherited.valid_users, 'alice')
        self.assertTrue(override.browseable)
        self.assertEqual(override.valid_users, 'jasper')

    def test_existing_read_only_settings_are_left_untouched(self):
        original = JASPER + b' read only = yes\n'
        document = SambaDocument(original)
        self.assertEqual(document.get('Jasper').path, '/mnt/data/jasper')
        self.assertTrue(document.add(Share('Photos', '/mnt/data/photos', 'jasper')).startswith(original))

    def test_add_preserves_all_existing_bytes_and_crlf(self):
        original = GLOBAL + JASPER + LATER_GLOBAL
        result = SambaDocument(original).add(Share("Virtual Disks", "/mnt/data/vdisk", "jasper"))
        self.assertTrue(result.startswith(original))
        self.assertNotIn(b"\n", result.replace(b"\r\n", b""))
        share = SambaDocument(result).get("virtual disks")
        self.assertEqual(share.path, "/mnt/data/vdisk")
        self.assertEqual(share.valid_users, "jasper")

    def test_remove_preserves_other_sections_and_all_global_bytes(self):
        original = GLOBAL + JASPER + DISKS + LATER_GLOBAL
        self.assertEqual(SambaDocument(original).remove("jasper"), GLOBAL + DISKS + LATER_GLOBAL)
        self.assertEqual(SambaDocument(original).remove("Virtual Disks"), GLOBAL + JASPER + LATER_GLOBAL)

    def test_reserved_sections_are_preserved(self):
        reserved = b"[homes]\n read only = yes\n[printers]\n path = /var/spool/samba\n printable = yes\n"
        result = SambaDocument(GLOBAL + reserved + JASPER).remove("Jasper")
        self.assertEqual(result, GLOBAL + reserved)
        with self.assertRaises(NasError):
            SambaDocument(result).remove("homes")

    def test_rejects_case_insensitive_duplicate(self):
        with self.assertRaisesRegex(NasError, "already exists"):
            SambaDocument(GLOBAL + JASPER).add(Share("JASPER", "/mnt/data/new", "jasper"))
        with self.assertRaisesRegex(NasError, "Duplicate"):
            SambaDocument(JASPER + b"[jasper]\n path = /mnt/data/other\n")

    def test_global_is_never_a_share(self):
        document = SambaDocument(GLOBAL + JASPER + LATER_GLOBAL)
        with self.assertRaises(NasError):
            document.remove("GLOBAL")
        with self.assertRaises(NasError):
            document.add(Share("global", "/mnt/data/global", "jasper"))

    def test_add_does_not_write_remote_metadata(self):
        result = SambaDocument(GLOBAL).add(Share('Photos', '/mnt/data/photos', 'jasper'))
        self.assertNotIn(b'nas-share remote', result)

    def test_legacy_remote_comments_are_preserved_and_ignored(self):
        for value in (b'not-json', b'42', b'"Other Folder"'):
            with self.subTest(value=value):
                original = JASPER + b' # nas-share remote = ' + value + b'\n'
                document = SambaDocument(original)
                self.assertEqual(document.get('Jasper').path, '/mnt/data/jasper')
                result = document.add(Share('Photos', '/mnt/data/photos', 'jasper'))
                self.assertTrue(result.startswith(original))

    def test_case_and_whitespace_in_parameter_names(self):
        share = SambaDocument(b"[Photos]\n P a T H = /mnt/data/photos\n VALID USERS = jasper\n Writeable = TRUE\n Browsable = 0\n").get("photos")
        self.assertEqual(share.path, "/mnt/data/photos")
        self.assertFalse(share.browseable)

    def test_refuses_external_configuration(self):
        for setting in (b"include = /etc/samba/extra.conf", b"config file = /etc/samba/other.conf", b"config backend = registry", b"registry shares = yes"):
            with self.subTest(setting=setting), self.assertRaisesRegex(NasError, "external"):
                SambaDocument(b"[global]\n" + setting + b"\n" + JASPER).shares()

    def test_continued_path_cannot_be_mistaken_for_share_header(self):
        original = b"[Jasper]\n path = /mnt/data/\\\n[global]\n valid users = jasper\n"
        document = SambaDocument(original)
        self.assertEqual(len(document.sections), 1)
        with self.assertRaisesRegex(NasError, "continued"):
            document.get("Jasper")

    def test_continued_unrelated_global_setting_is_preserved(self):
        global_data = b"[global]\n log file = /var/\\\n # preserved comment\n log/samba\n"
        result = SambaDocument(global_data + JASPER).remove("Jasper")
        self.assertEqual(result, global_data)

    def test_ambiguous_paths_and_inherited_shares_are_rejected(self):
        with self.assertRaisesRegex(NasError, "Conflicting paths"):
            SambaDocument(JASPER + b"directory = /mnt/data/other\n")
        with self.assertRaisesRegex(NasError, "inherits"):
            SambaDocument(JASPER + b"copy = other\n").shares()

    def test_config_injection_in_add_is_rejected(self):
        for share in (
            Share("Bad]\n[global", "/mnt/data/bad", "jasper"),
            Share("Bad", "/mnt/data/bad\n guest ok = yes", "jasper"),
            Share("Bad", "/mnt/data/bad", "jasper\n guest ok = yes"),
            Share("Bad", "/mnt/data/%U", "jasper"),
        ):
            with self.subTest(share=share), self.assertRaises(NasError):
                SambaDocument(GLOBAL).add(share)


class ConfigChangeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "smb.conf"
        self.original = GLOBAL + JASPER
        self.updated = GLOBAL + JASPER + DISKS
        self.path.write_bytes(self.original)
        self.path.chmod(0o640)

    def change(self):
        return change_config(self.path, self.original, self.updated)

    @patch("sambasync.samba.subprocess.run")
    def test_success_validates_candidate_then_backs_up_and_reloads(self, run):
        def command_result(command, **kwargs):
            if command[0] == "testparm":
                self.assertEqual(command[:2], ["testparm", "-s"])
                self.assertEqual(self.path.read_bytes(), self.original)
                self.assertEqual(Path(command[-1]).read_bytes(), self.updated)
            else:
                self.assertEqual(command, ["smbcontrol", "all", "reload-config"])
                self.assertEqual(self.path.read_bytes(), self.updated)
            return subprocess.CompletedProcess(command, 0, "", "")
        run.side_effect = command_result
        original_stat = self.path.stat()
        backup = self.change()
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(self.path.read_bytes(), self.updated)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual((self.path.stat().st_uid, self.path.stat().st_gid), (original_stat.st_uid, original_stat.st_gid))
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o640)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(list(self.path.parent.glob(".smb.conf.*")), [])

    @patch("sambasync.samba.subprocess.run")
    def test_validation_failure_does_not_replace_original(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "invalid configuration")
        with self.assertRaisesRegex(NasError, "validation failed"):
            self.change()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(run.call_count, 1)

    @patch("sambasync.samba.subprocess.run")
    def test_reload_failure_rolls_back_and_reloads_original(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "reload failed"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with self.assertRaisesRegex(NasError, "Original configuration restored"):
            self.change()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(run.call_count, 3)
        backups = list(self.path.parent.glob("smb.conf.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    @patch("sambasync.samba.subprocess.run")
    def test_failure_to_reload_restored_config_is_reported(self, run):
        run.side_effect = [subprocess.CompletedProcess([], code, "", "failed") for code in (0, 1, 1)]
        with self.assertRaisesRegex(NasError, "Reload of the restored configuration failed"):
            self.change()
        self.assertEqual(self.path.read_bytes(), self.original)

    @patch("sambasync.samba.subprocess.run")
    def test_concurrent_edit_during_validation_is_not_overwritten(self, run):
        external = b"# externally edited\n" + self.original
        def validate(command, **kwargs):
            self.path.write_bytes(external)
            return subprocess.CompletedProcess(command, 0, "", "")
        run.side_effect = validate
        with self.assertRaisesRegex(NasError, "changed during validation"):
            self.change()
        self.assertEqual(self.path.read_bytes(), external)
        self.assertEqual(run.call_count, 1)

    @patch("sambasync.samba.subprocess.run")
    def test_concurrent_edit_during_reload_is_not_overwritten_by_rollback(self, run):
        external = b"# external replacement\n" + self.updated
        def result(command, **kwargs):
            if command[0] == "testparm":
                return subprocess.CompletedProcess(command, 0, "", "")
            self.path.write_bytes(external)
            return subprocess.CompletedProcess(command, 1, "", "reload failed")
        run.side_effect = result
        with self.assertRaisesRegex(NasError, "automatic rollback would overwrite"):
            self.change()
        self.assertEqual(self.path.read_bytes(), external)

    @patch("sambasync.samba.subprocess.run")
    def test_missing_executable_is_a_clean_error(self, run):
        run.side_effect = FileNotFoundError("testparm")
        with self.assertRaisesRegex(NasError, "Could not run Samba validation"):
            self.change()
        self.assertEqual(self.path.read_bytes(), self.original)

    @patch("sambasync.samba.subprocess.run")
    def test_symlink_is_rejected(self, run):
        real = self.path.with_name("real.conf")
        self.path.rename(real)
        self.path.symlink_to(real)
        with self.assertRaisesRegex(NasError, "symlink"):
            self.change()
        self.assertFalse(run.called)
        self.assertEqual(real.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
