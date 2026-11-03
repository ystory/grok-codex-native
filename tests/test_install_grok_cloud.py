import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install-grok-cloud.py"
spec = importlib.util.spec_from_file_location("install_grok_cloud", SCRIPT)
installer = importlib.util.module_from_spec(spec)
with patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]):
    spec.loader.exec_module(installer)
    import prepare as preparation


class PreserveControlTests(unittest.TestCase):
    def invoke(self, directory, existing_state, commands, output, *, existing_gid=None, existing_uid=1000, explicit_data=True, metadata=None, import_metadata=None, readonly=False, invalid=None):
        directory = Path(directory).resolve()
        project = Path(directory) / ("workspace/project" if import_metadata else "project")
        (project / "scripts").mkdir(parents=True)
        (project / "deploy").mkdir()
        (project / "deploy/config.toml").write_text('forced_login_method = "chatgpt"\n')
        (project / "deploy/environments.toml").write_text("include_local = false\n")
        data = Path(directory) / ("private-data" if explicit_data or existing_gid is not None else "workspace/grok-codex-data")
        if invalid == "inside_checkout":
            data = project / "private-data"
        elif invalid == "invalid_path":
            data = Path(directory) / "bad data"
        elif invalid == "unsafe_environment":
            outside = Path(directory) / "outside.env"
            outside.write_text(f"GROK_CODEX_DATA={data}\nGROK_CODEX_PROJECT=grok-codex\nGROK_CODEX_UID=1000\nGROK_CODEX_GID=1000\n")
            (project / "deploy/.env").symlink_to(outside)
        elif invalid == "bad_config":
            (project / "deploy/config.toml").write_text('forced_login_method = "api"\n')
        if metadata is not None or readonly:
            data.mkdir(parents=True)
        if metadata is not None:
            (data / "deployment.json").write_text(json.dumps(metadata))
        if readonly:
            data.chmod(0o500)
        source = Path(directory) / "import-source"
        if import_metadata:
            source.mkdir()
            (source / "deployment.json").write_text(json.dumps(import_metadata))
        if existing_gid is not None:
            (project / "deploy/.env").write_text(
                f"GROK_CODEX_DATA={Path(directory) / 'private-data'}\nGROK_CODEX_PROJECT=grok-codex\n"
                f"GROK_CODEX_UID={existing_uid}\nGROK_CODEX_GID={existing_gid}\n"
            )

        def execute(command, **kwargs):
            effective = shlex.split(command[3]) if command[:3] == ["sg", "docker", "-c"] else command
            commands.append(effective)
            if any(str(value).endswith("/prepare.sh") for value in effective):
                environment = project / "deploy/.env"
                if not environment.exists():
                    environment.parent.mkdir(exist_ok=True)
                    prepared_data = effective[effective.index(str((project / "scripts/prepare.sh").resolve())) + 1]
                    prepared_project = effective[effective.index("--project") + 1] if "--project" in effective else "grok-codex"
                    prepared_gid = effective[effective.index("-g") + 1].removeprefix("#")
                    environment.write_text(
                        f"GROK_CODEX_DATA={prepared_data}\nGROK_CODEX_PROJECT={prepared_project}\n"
                        f"GROK_CODEX_UID=1000\nGROK_CODEX_GID={prepared_gid}\n"
                    )
                response = ""
            elif effective[-4:] == ["ps", "--all", "--quiet", "control"]:
                response = "existing-control-id\n" if existing_state else ""
            elif effective[:2] == ["docker", "inspect"]:
                response = json.dumps(existing_state)
            elif "/opt/project/deploy/healthcheck.py" in effective:
                response = json.dumps({"accountType": None}) + "\n"
            else:
                response = ""
            return subprocess.CompletedProcess(command, 0, stdout=response)

        with patch.object(installer, "__file__", str(project / "scripts/install-grok-cloud.py")), \
                patch.object(installer._docker, "PROJECT", project), \
                patch.object(installer, "WORKSPACE", (Path(directory) / "workspace").resolve()), \
                patch.object(installer, "DOCKER_SOCKET", Path(directory) / "absent-docker.sock"), \
                patch.object(sys, "argv", [str(SCRIPT)] + (["--data", str(data)] if explicit_data else []) + (["--import-data", str(source)] if import_metadata else [])), \
                patch.object(sys, "platform", "linux"), \
                patch.object(installer.os, "geteuid", return_value=1000), \
                patch.object(installer.os, "getuid", return_value=1000), \
                patch.object(installer.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="box", pw_gid=1000)), \
                patch.object(installer.subprocess, "run", side_effect=execute), \
                contextlib.redirect_stdout(output):
            installer.main()

    def test_absent_control_builds_and_starts_a_first_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, output = [], io.StringIO()
            self.invoke(directory, None, commands, output, explicit_data=False)
            self.assertTrue(any(command[-3:] == ["--profile", "build-worker", "build"] for command in commands))
            self.assertTrue(any(command[-4:] == ["up", "-d", "--wait", "control"] for command in commands))
            self.assertIn('"status": "runtime_prepared"', output.getvalue())
            self.assertIn('"authentication": "authentication_still_required"', output.getvalue())
            prepare = next(command for command in commands if any(str(value).endswith("/prepare.sh") for value in command))
            self.assertEqual(prepare[:7], ["sudo", "-n", "-u", "box", "-g", "#1000", "--"])
            self.assertIn(str((Path(directory) / "workspace/grok-codex-data").resolve()), prepare)
            network = next(command for command in commands if any(str(value).endswith("/worker-network.py") for value in command))
            self.assertEqual(network, ["sudo", "-n", "python3", str((Path(directory) / "project/scripts/worker-network.py").resolve()), "apply", "--project", "grok-codex", "--all"])
            listing = next(command for command in commands if command[-4:] == ["ps", "--all", "--quiet", "control"])
            self.assertLess(commands.index(prepare), commands.index(network))
            self.assertLess(commands.index(network), commands.index(listing))

    def test_existing_deployment_keeps_its_recorded_gid(self):
        state = {"Status": "running", "Running": True}
        with tempfile.TemporaryDirectory() as directory:
            commands, output = [], io.StringIO()
            self.invoke(directory, state, commands, output, existing_gid=993, explicit_data=False,
                        metadata={"schemaVersion": 1, "project": "grok-codex", "uid": 1000, "gid": 993})
            prepare = next(command for command in commands if any(str(value).endswith("/prepare.sh") for value in command))
            self.assertEqual(prepare[:7], ["sudo", "-n", "-u", "box", "-g", "#993", "--"])
            self.assertIn(str((Path(directory) / "private-data").resolve()), prepare)
            self.assertFalse(any("build" in command or "up" in command for command in commands))

    def test_import_metadata_selects_the_saved_project_and_gid(self):
        saved = {"schemaVersion": 1, "project": "saved-grok", "uid": 1000, "gid": 993}
        with tempfile.TemporaryDirectory() as directory:
            commands, output = [], io.StringIO()
            self.invoke(directory, None, commands, output, explicit_data=False, import_metadata=saved)
            prepare = next(command for command in commands if any(str(value).endswith("/prepare.sh") for value in command))
            self.assertEqual(prepare[:7], ["sudo", "-n", "-u", "box", "-g", "#993", "--"])
            self.assertEqual(prepare[-2:], ["--project", "saved-grok"])
            network = next(command for command in commands if any(str(value).endswith("/worker-network.py") for value in command))
            self.assertEqual(network[-3:], ["--project", "saved-grok", "--all"])
            self.assertEqual(json.loads((Path(directory) / "workspace/grok-codex-data/deployment.json").read_text()), saved)
            self.assertTrue((Path(directory) / "import-source/deployment.json").exists())

    def test_metadata_conflicts_and_readonly_data_fail_before_system_changes(self):
        cases = (
            ({"schemaVersion": 1, "project": "grok-codex", "uid": 1001, "gid": 993}, None, False),
            ({"schemaVersion": 1, "project": "grok-codex", "uid": 1000, "gid": 993}, 1000, False),
            (None, None, True),
        )
        for metadata, gid, readonly in cases:
            with self.subTest(metadata=metadata, readonly=readonly), tempfile.TemporaryDirectory() as directory:
                commands, output = [], io.StringIO()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.invoke(directory, None, commands, output, metadata=metadata, existing_gid=gid, readonly=readonly)
                self.assertEqual(commands, [])

    def test_different_existing_uid_is_refused_before_system_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            commands, output = [], io.StringIO()
            with self.assertRaisesRegex(RuntimeError, "UID"):
                self.invoke(directory, None, commands, output, existing_gid=993, existing_uid=1001)
            self.assertEqual(commands, [])

    def test_prepare_constraints_are_checked_before_bootstrap_commands(self):
        for invalid in ("inside_checkout", "invalid_path", "unsafe_environment", "bad_config"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                commands, output = [], io.StringIO()
                with self.assertRaises((RuntimeError, ValueError)):
                    self.invoke(directory, None, commands, output, invalid=invalid)
                self.assertEqual(commands, [])

    def test_running_control_is_health_checked_without_building_or_recreating(self):
        state = {"Status": "running", "Running": True, "Paused": False, "Restarting": False, "Dead": False}
        with tempfile.TemporaryDirectory() as directory:
            commands, output = [], io.StringIO()
            self.invoke(directory, state, commands, output)
            self.assertFalse(any("build" in command or "up" in command for command in commands))
            self.assertTrue(any("/opt/project/deploy/healthcheck.py" in command for command in commands))
            self.assertIn('"status": "runtime_preserved"', output.getvalue())

    def test_nonrunning_control_is_preserved_and_requires_explicit_operation(self):
        for status in ("exited", "paused", "restarting"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                commands, output = [], io.StringIO()
                state = {"Status": status, "Running": status != "exited", "Paused": status == "paused", "Restarting": status == "restarting"}
                with self.assertRaisesRegex(RuntimeError, "preserved"):
                    self.invoke(directory, state, commands, output)
                self.assertFalse(any("build" in command or "up" in command for command in commands))
                self.assertFalse(any("/opt/project/deploy/healthcheck.py" in command for command in commands))


class ImportPrivateDataTests(unittest.TestCase):
    def test_cold_import_preserves_original_files_and_links_but_recreates_runtime_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, source = root / "workspace", root / "old-home-data"
            project, target = workspace / "project", workspace / "grok-codex-data"
            project.mkdir(parents=True)
            (source / "codex").mkdir(parents=True)
            (source / "codex/auth.json").write_text("PRIVATE_TEST_SENTINEL")
            (source / "codex/auth.json").chmod(0o600)
            (source / "workspaces").mkdir()
            (source / "workspaces/external-link").symlink_to(root / "outside")
            (root / "outside").write_text("DO_NOT_COPY_EXTERNAL_CONTENT")
            (source / "control-run").mkdir()
            os.mkfifo(source / "control-run/stale-socket")
            with patch.object(installer, "WORKSPACE", workspace), patch.object(installer, "DOCKER_SOCKET", root / "absent-docker.sock"):
                installer.import_private_data(source, target, project)
            self.assertEqual((source / "codex/auth.json").read_text(), "PRIVATE_TEST_SENTINEL")
            self.assertEqual((target / "codex/auth.json").read_text(), "PRIVATE_TEST_SENTINEL")
            self.assertEqual((target / "codex/auth.json").stat().st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertTrue((target / "workspaces/external-link").is_symlink())
            self.assertEqual(os.readlink(target / "workspaces/external-link"), str(root / "outside"))
            self.assertEqual(list((target / "control-run").iterdir()), [])
            self.assertFalse((target / "outside").exists())

    def test_unsafe_or_nonfresh_import_is_refused_without_overwriting_source_or_target(self):
        for reason in ("docker_socket", "target_exists", "source_link", "target_parent_link", "outside_workspace", "inside_checkout", "source_inside_checkout", "existing_environment", "special_file", "managed_codex_link", "dangling_config_link"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                workspace, source = root / "workspace", root / "old-home-data"
                project, target, socket = workspace / "project", workspace / "grok-codex-data", root / "docker.sock"
                project.mkdir(parents=True)
                if reason == "source_inside_checkout":
                    source = project / "old-data"
                source.mkdir()
                (source / "history").write_text("ORIGINAL")
                original_source = source
                if reason == "docker_socket":
                    socket.touch()
                elif reason == "target_exists":
                    target.mkdir()
                    (target / "existing").write_text("KEEP")
                elif reason == "source_link":
                    source = root / "source-link"
                    source.symlink_to(original_source, target_is_directory=True)
                elif reason == "target_parent_link":
                    (workspace / "linked-parent").symlink_to(root, target_is_directory=True)
                    target = workspace / "linked-parent/new-data"
                elif reason == "outside_workspace":
                    target = root / "outside-data"
                elif reason == "inside_checkout":
                    target = project / "private-data"
                elif reason == "existing_environment":
                    (project / "deploy").mkdir()
                    (project / "deploy/.env").write_text("EXISTING")
                elif reason == "special_file":
                    os.mkfifo(source / "unmanaged-pipe")
                elif reason == "managed_codex_link":
                    original = root / "original-codex"
                    original.mkdir(mode=0o750)
                    original.chmod(0o750)
                    (original / "marker").write_text("ORIGINAL_CODEX")
                    (source / "codex").symlink_to(original, target_is_directory=True)
                elif reason == "dangling_config_link":
                    (source / "codex").mkdir()
                    (source / "codex/config.toml").symlink_to(root / "absent-config.toml")
                with patch.object(installer, "WORKSPACE", workspace), patch.object(installer, "DOCKER_SOCKET", socket):
                    with self.assertRaises((RuntimeError, ValueError)):
                        installer.import_private_data(source, target, project)
                self.assertEqual((original_source / "history").read_text(), "ORIGINAL")
                if reason == "target_exists":
                    self.assertEqual((target / "existing").read_text(), "KEEP")
                else:
                    self.assertFalse(target.exists())
                if reason == "managed_codex_link":
                    self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o750)
                    self.assertEqual((original / "marker").read_text(), "ORIGINAL_CODEX")
                elif reason == "dangling_config_link":
                    self.assertFalse((root / "absent-config.toml").exists())


class DeploymentIdentityTests(unittest.TestCase):
    def test_prepare_refuses_managed_directory_or_config_links_before_touching_originals(self):
        for kind in ("codex_directory", "dangling_config"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                project, data, original = root / "project", root / "private-data", root / "original-codex"
                (project / "scripts").mkdir(parents=True)
                (project / "deploy").mkdir()
                (project / "deploy/config.toml").write_text('forced_login_method = "chatgpt"\n')
                (project / "deploy/environments.toml").write_text("include_local = false\n")
                data.mkdir()
                original.mkdir(mode=0o750)
                original.chmod(0o750)
                (original / "config.toml").write_text('forced_login_method = "chatgpt"\n')
                (original / "environments.toml").write_text("include_local = false\n")
                if kind == "codex_directory":
                    (data / "codex").symlink_to(original, target_is_directory=True)
                else:
                    (data / "codex").mkdir()
                    (data / "codex/config.toml").symlink_to(root / "absent-config.toml")
                with patch.object(preparation, "__file__", str(project / "scripts/prepare.py")), \
                        patch.object(sys, "argv", ["prepare.py", str(data)]), patch.object(sys, "platform", "linux"), \
                        patch.object(preparation.os, "getuid", return_value=1000), patch.object(preparation.os, "getgid", return_value=1000), \
                        patch.object(preparation.os, "geteuid", return_value=1000), \
                        patch.object(preparation.shutil, "which", return_value="docker"), \
                        patch.object(preparation.subprocess, "check_output", return_value="linux\n") as query, \
                        patch.object(preparation.subprocess, "run") as execute, \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        preparation.main()
                query.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o750)
                self.assertEqual((original / "config.toml").read_text(), 'forced_login_method = "chatgpt"\n')
                self.assertFalse((root / "absent-config.toml").exists())
                self.assertFalse((data / "deployment.json").exists())

    def test_prepare_writes_private_identity_and_refuses_later_identity_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project, data = root / "project", root / "private-data"
            (project / "scripts").mkdir(parents=True)
            (project / "deploy").mkdir()
            (project / "deploy/config.toml").write_text('forced_login_method = "chatgpt"\n')
            (project / "deploy/environments.toml").write_text("include_local = false\n")
            with patch.object(preparation, "__file__", str(project / "scripts/prepare.py")), \
                    patch.object(sys, "argv", ["prepare.py", str(data)]), \
                    patch.object(sys, "platform", "linux"), \
                    patch.object(preparation.os, "getuid", return_value=1000), \
                    patch.object(preparation.os, "getgid", return_value=1000), \
                    patch.object(preparation.os, "geteuid", return_value=1000), \
                    patch.object(preparation.shutil, "which", return_value="docker"), \
                    patch.object(preparation.subprocess, "check_output", return_value="linux\n") as query, \
                    patch.object(preparation.subprocess, "run") as execute, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                preparation.main()
                metadata = data / "deployment.json"
                self.assertEqual(json.loads(metadata.read_text()), {"schemaVersion": 1, "project": "grok-codex", "uid": 1000, "gid": 1000})
                self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
                (data / "manifests").mkdir(exist_ok=True)
                (data / "manifests").chmod(0o755)
                metadata.chmod(0o644)
                auth = data / "codex/auth.json"
                auth.write_text("PRIVATE_TEST_SENTINEL")
                auth.chmod(0o640)
                preparation.main()
                self.assertEqual(stat.S_IMODE((data / "manifests").stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(metadata.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(auth.stat().st_mode), 0o640)
                self.assertEqual(auth.read_text(), "PRIVATE_TEST_SENTINEL")
                original = metadata.read_bytes()
                query.reset_mock()
                execute.reset_mock()
                with patch.object(preparation.os, "getgid", return_value=993):
                    with self.assertRaises(SystemExit):
                        preparation.main()
                query.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(metadata.read_bytes(), original)

    def test_invalid_or_symlinked_metadata_is_refused(self):
        for invalid in (
            {"schemaVersion": 1, "project": "grok-codex", "uid": 1000, "gid": 0},
            {"schemaVersion": 1, "project": "grok-codex", "uid": True, "gid": 993},
            {"schemaVersion": True, "project": "grok-codex", "uid": 1000, "gid": 993},
            {"schemaVersion": 1, "project": "../other", "uid": 1000, "gid": 993},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    preparation.validate_deployment_identity(invalid)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "outside.json").write_text("DO_NOT_READ")
            (data / "deployment.json").symlink_to(data / "outside.json")
            with self.assertRaisesRegex(ValueError, "symlink"):
                preparation.read_deployment_identity(data)


class BuildNetworkTests(unittest.TestCase):
    def test_daemon_without_default_bridge_uses_host_network_for_both_builds(self):
        project = SCRIPT.parents[1]
        environment = dict(os.environ, GROK_CODEX_DATA="/tmp/grok-codex-install-config-test")
        config = json.loads(subprocess.check_output([
            "docker", "compose", "-f", str(project / "deploy/compose.yaml"),
            "--profile", "build-worker", "config", "--format", "json",
        ], env=environment, text=True))
        for service in ("control", "worker-template"):
            with self.subTest(service=service):
                self.assertEqual(config["services"][service]["build"].get("network"), "host")


if __name__ == "__main__":
    unittest.main()
