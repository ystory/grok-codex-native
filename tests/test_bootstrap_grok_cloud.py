import importlib.util
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/bootstrap-grok-cloud.py"
spec = importlib.util.spec_from_file_location("bootstrap_grok_cloud", SCRIPT)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapConfigurationTests(unittest.TestCase):
    def test_fresh_daemon_uses_private_roots_without_claiming_the_platform_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = bootstrap.Journal(Path(directory) / "state")
            exists = Path.exists
            with patch.object(bootstrap, "daemon_running", side_effect=[False, True]), \
                    patch.object(Path, "exists", autospec=True, side_effect=lambda path: False if path == Path("/run/systemd/system") else exists(path)), \
                    patch.object(bootstrap, "process_start", return_value="12345"), \
                    patch.object(bootstrap.subprocess, "Popen", return_value=SimpleNamespace(pid=6789)) as launch:
                bootstrap.start_daemon(journal)
            arguments = launch.call_args.args[0]
            self.assertIn("--bridge=none", arguments)
            self.assertIn("--data-root=/var/lib/grok-codex-docker", arguments)
            self.assertIn("--exec-root=/run/grok-codex-docker", arguments)
            self.assertIn("--host=unix:///var/run/docker.sock", arguments)
            self.assertEqual(journal.data["daemon"]["command"], arguments)

    def test_second_user_is_refused_and_rollback_removes_the_first_added_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            members = set()

            def execute(command, **kwargs):
                if command[0] == "usermod":
                    members.add(command[-1])
                elif command[0] == "gpasswd":
                    members.remove(command[2])
                else:
                    self.fail(f"Unexpected system command: {command}")

            with patch.object(bootstrap.pwd, "getpwnam", return_value=SimpleNamespace(pw_gid=1000)), \
                    patch.object(bootstrap.grp, "getgrnam", side_effect=lambda name: SimpleNamespace(gr_gid=998, gr_mem=sorted(members))), \
                    patch.object(bootstrap, "run", side_effect=execute), \
                    patch.object(bootstrap, "daemon_running", return_value=True), \
                    patch.object(bootstrap, "verify"):
                bootstrap.install(bootstrap.Journal(state), "amd64", "alice")
                with self.assertRaisesRegex(RuntimeError, "already selects.*alice"):
                    bootstrap.install(bootstrap.Journal(state), "amd64", "bob")
                self.assertEqual(members, {"alice"})
                bootstrap.rollback(bootstrap.Journal(state))
                self.assertEqual(members, set())

    def test_nonexistent_user_is_refused_before_system_commands_or_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = bootstrap.Journal(Path(directory) / "state")
            with patch.object(bootstrap.pwd, "getpwnam", side_effect=KeyError("missing")), \
                    patch.object(bootstrap, "run") as command:
                with self.assertRaisesRegex(RuntimeError, "existing user"):
                    bootstrap.install(journal, "amd64", "missing")
            command.assert_not_called()
            self.assertEqual(journal.data, {"configs": {}})
            self.assertFalse(journal.path.exists())

    def test_removed_package_configuration_is_not_an_installed_dependency(self):
        output = "ii \tdocker-ce\t5:29.8.2\nrc \tcontainerd.io\t2.2.0\n"
        self.assertEqual(bootstrap.installed_packages(output), {"docker-ce": "5:29.8.2"})

    def test_mirrors_change_only_exact_debian_uri_tokens(self):
        source = (
            "# http://deb.debian.org/debian is the original mirror\n"
            "Types: deb\nURIs: http://deb.debian.org/debian\n"
            "Suites: trixie trixie-updates\nSigned-By: /usr/share/keyrings/debian.gpg\n\n"
            "URIs: https://deb.debian.org/debian-security https://example.org/debian\n"
            "Suites: trixie-security\n"
        )
        changed = bootstrap.debian_mirrors(source)
        self.assertIn("# http://deb.debian.org/debian is the original mirror", changed)
        self.assertIn("URIs: https://ftp.debian.org/debian\n", changed)
        self.assertIn("URIs: https://security.debian.org/debian-security https://example.org/debian", changed)
        self.assertIn("Signed-By: /usr/share/keyrings/debian.gpg", changed)
        self.assertEqual(bootstrap.debian_mirrors(changed), changed)

    def test_repeated_install_and_rollback_preserve_original_bytes_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "debian.sources"
            source.write_bytes(b"original\n")
            source.chmod(0o640)
            state = root / "state"
            bootstrap.Journal(state).write_config(source, b"managed\n")
            bootstrap.Journal(state).write_config(source, b"managed\n")
            bootstrap.Journal(state).restore_configs()
            self.assertEqual(source.read_bytes(), b"original\n")
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o640)
            bootstrap.Journal(state).restore_configs()
            self.assertEqual(source.read_bytes(), b"original\n")

    def test_rollback_archives_new_configuration_and_removes_active_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "docker.sources"
            journal = bootstrap.Journal(root / "state")
            journal.write_config(source, b"official signed repository\n")
            journal.restore_configs()
            self.assertFalse(source.exists())
            archived = list((root / "state" / "retired-configs").iterdir())
            self.assertEqual([path.read_bytes() for path in archived], [b"official signed repository\n"])

    def test_external_edit_refuses_rollback_before_restoring_any_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.write_bytes(b"first original")
            second.write_bytes(b"second original")
            journal = bootstrap.Journal(root / "state")
            journal.write_config(first, b"first managed")
            journal.write_config(second, b"second managed")
            second.write_bytes(b"administrator edit")
            with self.assertRaisesRegex(RuntimeError, "changed outside"):
                journal.restore_configs()
            self.assertEqual(first.read_bytes(), b"first managed")
            self.assertEqual(second.read_bytes(), b"administrator edit")

    def test_external_edit_and_symlink_refuse_reinstallation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            journal = bootstrap.Journal(root / "state")
            journal.write_config(source, b"managed")
            source.write_bytes(b"administrator edit")
            with self.assertRaisesRegex(RuntimeError, "changed outside"):
                journal.write_config(source, b"managed")
            link = root / "link"
            link.symlink_to(source)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                journal.write_config(link, b"replacement")
            self.assertEqual(source.read_bytes(), b"administrator edit")

    def test_pending_configuration_record_can_be_safely_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"original")
            journal = bootstrap.Journal(root / "state")
            journal.write_config(source, b"managed")
            source.write_bytes(b"original")
            bootstrap.Journal(root / "state").write_config(source, b"managed")
            self.assertEqual(source.read_bytes(), b"managed")
            receipt = json.loads((root / "state" / "receipt.json").read_text())
            self.assertEqual(len(receipt["configs"]), 1)


if __name__ == "__main__":
    unittest.main()
