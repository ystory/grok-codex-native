import importlib.util
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("worker_launch", SCRIPTS / "_worker.py")
worker = importlib.util.module_from_spec(spec)
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    spec.loader.exec_module(worker)
spec = importlib.util.spec_from_file_location("restore_worker", SCRIPTS / "restore-worker.py")
restore = importlib.util.module_from_spec(spec)
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]), patch.dict(sys.modules, {"_worker": worker}):
    spec.loader.exec_module(restore)
spec = importlib.util.spec_from_file_location("new_worker", SCRIPTS / "new-worker.py")
creator = importlib.util.module_from_spec(spec)
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]), patch.dict(sys.modules, {"_worker": worker}):
    spec.loader.exec_module(creator)
from _docker import Deployment


IMAGE_A, IMAGE_B = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def image(identity=IMAGE_A):
    return {"Id": identity, "Config": {"Entrypoint": ["docker-entrypoint.sh"],
                                        "Env": ["HOME=/home/node", "CODEX_HOME=/home/node/.codex"], "Volumes": None,
                                        "User": "node", "WorkingDir": "/work/source"}}


def arguments(**changes):
    values = {"name": "test", "image": None, "cpus": 2.0, "memory": "4g", "pids_limit": 512,
              "browser_port": 3000, "env_file": None, "inputs_dir": None, "secrets_dir": None,
              "repo": "https://private-pat@example.org/secret/repository.git", "ref": "main"}
    return SimpleNamespace(**(values | changes))


class RestoreWorkerTests(unittest.TestCase):
    def fixture(self, directory, *, custom=False):
        data = Path(directory).resolve() / "data"
        data.mkdir(mode=0o700)
        config = Deployment(data, "grok-codex", os.getuid(), os.getgid())
        paths = worker.create_layout(config, "test")
        manifest = worker.make_manifest(config, arguments(image="custom/worker:approved" if custom else None), image())
        worker.write_manifest(config, "test", manifest)
        old_token = worker.write_token(paths)
        (paths["source"] / "keep.txt").write_text("uncommitted source\n")
        (paths["artifacts"] / "keep.txt").write_text("original proof\n")
        (paths["home"] / "keep.txt").write_text("worker config\n")
        (data / "codex").mkdir(mode=0o700)
        (data / "codex/history.json").write_text('{"native":"preserve"}\n')
        return config, paths, manifest, old_token

    def existing(self, config, paths, manifest, token, *, running=True):
        binds = [
            {"Type": "bind", "Source": str(paths["source"]), "Destination": "/work/source", "RW": True},
            {"Type": "bind", "Source": str(paths["artifacts"]), "Destination": "/work/artifacts", "RW": True},
            {"Type": "bind", "Source": str(paths["home"]), "Destination": "/home/node", "RW": True},
        ]
        return {"Id": "c" * 64, "Name": "/" + paths["identity"], "Image": IMAGE_A,
                "State": {"Status": "running" if running else "exited", "Running": running, "Paused": False, "Restarting": False, "Dead": False},
                "Config": {"Image": IMAGE_A, "Cmd": worker.executor_command(token),
                           "Entrypoint": image()["Config"]["Entrypoint"], "Env": image()["Config"]["Env"],
                           "User": "node", "WorkingDir": "/work/source",
                           "Labels": {"io.grok-codex.realm": config.project, "io.grok-codex.role": "worker",
                                      "io.grok-codex.environment": paths["identity"],
                                      "io.grok-codex.image-reference": manifest["image"]}},
                "HostConfig": {"NetworkMode": paths["network"], "Init": True, "NanoCpus": 2_000_000_000,
                               "Memory": 4 * 1024 ** 3, "PidsLimit": 512, "ShmSize": 512 * 1024 ** 2,
                               "Privileged": False, "CapAdd": None, "CapDrop": None, "PidMode": "", "IpcMode": "private",
                               "UTSMode": "", "SecurityOpt": None, "Devices": None, "DeviceRequests": None,
                               "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                               "PublishAllPorts": False, "Sysctls": {"net.ipv6.conf.all.disable_ipv6": "1",
                                                                      "net.ipv6.conf.default.disable_ipv6": "1"},
                               "PortBindings": {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32769"}],
                                                "3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32768"}]}},
                "Mounts": binds, "NetworkSettings": {"Networks": {paths["network"]: {"NetworkID": "d" * 64}}}}

    def network(self, config, paths):
        return {"Id": "d" * 64, "Name": paths["network"], "Driver": "bridge", "EnableIPv6": False,
                "Labels": {"io.grok-codex.realm": config.project, "io.grok-codex.role": "worker-network"}}

    def assert_worker_lock_held(self, config, name):
        path = worker.layout(config, name)["manifest"].with_suffix(".lock")
        result = subprocess.run([sys.executable, "-c",
            "import fcntl,sys; f=open(sys.argv[1],'a'); "
            "\ntry: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
            "\nexcept BlockingIOError: print('LOCKED')"
            "\nelse: raise SystemExit('UNLOCKED')", str(path)],
            text=True, capture_output=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "LOCKED"))

    def reset_permissions(self, config, paths):
        directories = [config.data / "workspaces", paths["source"], config.data / "artifacts", paths["artifacts"],
                       config.data / "runtime", paths["runtime"], paths["home"], paths["home"] / ".codex", paths["manifest"].parent]
        lock = paths["manifest"].with_suffix(".lock")
        lock.write_text("fixture lock")
        files = [paths["manifest"], paths["token"], lock]
        for path in directories:
            path.chmod(0o755)
        for path in files:
            path.chmod(0o640)
        return directories, files

    def test_manifest_is_strict_private_and_does_not_store_repository_or_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, manifest, _ = self.fixture(directory)
            stored = paths["manifest"].read_text()
            self.assertNotIn("private-pat", stored)
            self.assertNotIn("repository.git", stored)
            self.assertNotIn(str(config.data), stored)
            self.assertEqual(stat.S_IMODE(paths["manifest"].stat().st_mode), 0o600)
            env_file = Path(directory).resolve() / "worker.env"
            env_file.write_text("SERVICE_SECRET=fixture-secret-value\n")
            with_secret = worker.make_manifest(config, arguments(env_file=env_file), image())
            self.assertNotIn("fixture-secret-value", json.dumps(with_secret))
            self.assertEqual(with_secret["envFile"], str(env_file))
            for change in ({"command": ["sh", "-c", "unexpected"]}, {"uid": config.uid + 1},
                           {"gid": config.gid + 1}, {"worker": "another-worker"}, {"memory": "0"}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    worker.validate_manifest(manifest | change, config, "test")
            paths["manifest"].unlink()
            paths["manifest"].symlink_to(paths["source"] / "keep.txt")
            with self.assertRaises(ValueError):
                worker.read_manifest(config, "test")

    def test_restore_keeps_a_process_lock_from_inspection_through_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, _, _ = self.fixture(directory)
            phases = []
            write_token = worker.write_token

            def inspect(kind, name):
                self.assert_worker_lock_held(config, "test")
                phases.append("inspect-" + kind)
                return None

            def rotate(paths):
                self.assert_worker_lock_held(config, "test")
                phases.append("token")
                return write_token(paths)

            def execute(command, **kwargs):
                self.assert_worker_lock_held(config, "test")
                phases.append("create" if "create" in command else "register" if "register-worker.py" in " ".join(command) else "start")
                return "c" * 64 + "\n" if "create" in command else ""

            with patch.object(worker, "inspect_optional", side_effect=inspect), \
                    patch.object(worker, "image_info", return_value=image()), \
                    patch.object(worker, "write_token", side_effect=rotate), \
                    patch.object(worker, "execute", side_effect=execute):
                restore.restore(config, "test")
            self.assertIn("token", phases)
            self.assertIn("create", phases)
            self.assertEqual(phases[-1], "register")
            lock = paths["manifest"].with_suffix(".lock")
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_worker_lock_refuses_symlinks_and_nonprivate_files(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, _, _ = self.fixture(directory)
            lock = paths["manifest"].with_suffix(".lock")
            proof = paths["source"] / "keep.txt"
            lock.symlink_to(proof)
            with self.assertRaises((OSError, ValueError)):
                with worker.worker_lock(config, "test"):
                    self.fail("A symlink lock was accepted")
            self.assertEqual(proof.read_text(), "uncommitted source\n")
            lock.unlink()
            lock.write_text("preserve")
            lock.chmod(0o644)
            with self.assertRaises(ValueError):
                with worker.worker_lock(config, "test"):
                    self.fail("A nonprivate lock was accepted")
            self.assertEqual(lock.read_text(), "preserve")
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o644)

    def test_new_creation_holds_the_same_private_lock_for_token_and_docker_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            config, _, _, _ = self.fixture(directory)
            write_token = worker.write_token
            phases = []

            def rotate(paths):
                self.assert_worker_lock_held(config, "fresh")
                phases.append("token")
                return write_token(paths)

            def execute(command, **kwargs):
                self.assert_worker_lock_held(config, "fresh")
                phases.append("register" if "register-worker.py" in " ".join(command) else "docker")
                return "c" * 64 + "\n" if "create" in command else ""

            with patch.object(sys, "argv", [str(SCRIPTS / "new-worker.py"), "fresh"]), \
                    patch.object(creator, "deployment", return_value=config), \
                    patch.object(worker, "image_info", return_value=image()), \
                    patch.object(worker, "write_token", side_effect=rotate), \
                    patch.object(worker, "execute", side_effect=execute), \
                    contextlib.redirect_stdout(io.StringIO()):
                creator.main()
            self.assertIn("token", phases)
            self.assertEqual(phases[-1], "register")

    def test_user_working_directory_and_restart_policy_drift_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, manifest, token = self.fixture(directory)
            original = self.existing(config, paths, manifest, token)
            variants = []
            for key, value in (("User", "0:0"), ("WorkingDir", "/")):
                candidate = copy.deepcopy(original)
                candidate["Config"][key] = value
                variants.append(candidate)
            for policy in ({"Name": "always", "MaximumRetryCount": 0}, None,
                           {"Name": "no", "MaximumRetryCount": False}):
                candidate = copy.deepcopy(original)
                candidate["HostConfig"]["RestartPolicy"] = policy
                variants.append(candidate)
            for candidate in variants:
                with self.subTest(candidate=candidate["HostConfig"]["RestartPolicy"], user=candidate["Config"]["User"], cwd=candidate["Config"]["WorkingDir"]), \
                        patch.object(worker, "inspect_optional", side_effect=lambda kind, name: candidate if kind == "container" else self.network(config, paths)), \
                        patch.object(worker, "image_info", return_value=image()), \
                        patch.object(worker, "execute") as execute:
                    with self.assertRaises(ValueError):
                        restore.restore(config, "test")
                    execute.assert_not_called()
                    self.assertEqual(paths["token"].read_text().strip(), token)

    def test_fractional_cpu_limit_uses_dockers_exact_decimal_value(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, manifest, token = self.fixture(directory)
            existing = self.existing(config, paths, manifest, token)
            for cpus, nano in ((1.001, 1_001_000_000), (4.1, 4_100_000_000), (0.75, 750_000_000), (2, 2_000_000_000)):
                with self.subTest(cpus=cpus):
                    existing["HostConfig"]["NanoCpus"] = nano
                    worker.validate_container(existing, config, manifest | {"cpus": cpus}, paths, image(), token)
            existing["HostConfig"]["NanoCpus"] = 1_000_999_999
            with self.assertRaises(ValueError):
                worker.validate_container(existing, config, manifest | {"cpus": 1.001}, paths, image(), token)

    def test_explicit_restore_repairs_reset_metadata_without_changing_repo_file_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, _, _ = self.fixture(directory)
            directories, files = self.reset_permissions(config, paths)
            proof = paths["source"] / "keep.txt"
            proof.chmod(0o755)
            history = config.data / "codex/history.json"
            history.chmod(0o640)
            with patch.object(worker, "inspect_optional", return_value=None), \
                    patch.object(worker, "image_info", return_value=image()), \
                    patch.object(worker, "execute", side_effect=lambda command, **kwargs: "c" * 64 + "\n" if "create" in command else ""):
                restore.restore(config, "test")
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories))
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files))
            self.assertEqual((proof.read_text(), stat.S_IMODE(proof.stat().st_mode)), ("uncommitted source\n", 0o755))
            self.assertEqual((history.read_text(), stat.S_IMODE(history.stat().st_mode)), ('{"native":"preserve"}\n', 0o640))

    def test_unsafe_restore_path_or_descriptor_is_refused_before_any_permission_change(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, _, _ = self.fixture(directory)
            directories, files = self.reset_permissions(config, paths)
            codex = paths["home"] / ".codex"
            saved = paths["home"] / "preserved-codex"
            codex.rename(saved)
            outside = Path(directory).resolve() / "outside"
            outside.mkdir(mode=0o755)
            codex.symlink_to(outside, target_is_directory=True)
            with patch.object(worker, "inspect_optional") as inspect:
                with self.assertRaises(ValueError):
                    restore.restore(config, "test")
                inspect.assert_not_called()
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
            codex.unlink()
            saved.rename(codex)
            token_inode = paths["token"].stat().st_ino
            fstat = os.fstat

            def changed_owner(descriptor):
                metadata = fstat(descriptor)
                if metadata.st_ino == token_inode:
                    values = list(metadata)
                    values[4] = config.uid + 1
                    return os.stat_result(values)
                return metadata

            with patch.object(worker.os, "fstat", side_effect=changed_owner), \
                    patch.object(worker, "inspect_optional") as inspect:
                with self.assertRaises(ValueError):
                    restore.restore(config, "test")
                inspect.assert_not_called()
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o755 for path in directories))
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o640 for path in files))

    def test_missing_definitions_restore_from_migrated_data_without_touching_source_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, manifest, old_token = self.fixture(directory)
            moved = Path(directory).resolve() / "workspace-data"
            shutil.copytree(config.data, moved)
            migrated = Deployment(moved, config.project, config.uid, config.gid)
            new_paths = worker.layout(migrated, "test")
            commands = []

            def execute(command, **kwargs):
                commands.append(command)
                return "c" * 64 + "\n" if "create" in command else ""

            with patch.object(worker, "inspect_optional", return_value=None), \
                    patch.object(worker, "image_info", return_value=image(IMAGE_B)), \
                    patch.object(worker, "execute", side_effect=execute):
                result = restore.restore(migrated, "test")
            self.assertEqual(result["imageId"], IMAGE_B)
            self.assertNotEqual(new_paths["token"].read_text().strip(), old_token)
            self.assertEqual(stat.S_IMODE(new_paths["token"].stat().st_mode), 0o600)
            for name, expected in (("source", "uncommitted source\n"), ("artifacts", "original proof\n"), ("home", "worker config\n")):
                self.assertEqual((new_paths[name] / "keep.txt").read_text(), expected)
            self.assertEqual((moved / "codex/history.json").read_text(), '{"native":"preserve"}\n')
            creation = next(command for command in commands if "create" in command and "--mount" in command)
            self.assertTrue(any("src=" + str(new_paths["source"]) in value for value in creation))
            self.assertNotIn(str(config.data), " ".join(creation))
            self.assertIn(IMAGE_B, creation)
            self.assertFalse(any("clone" in command or "reset" in command for command in commands))
            start = next(i for i, command in enumerate(commands) if "worker-network.py" in " ".join(command) and "start" in command)
            register = next(i for i, command in enumerate(commands) if "register-worker.py" in " ".join(command))
            self.assertLess(start, register)
            self.assertEqual(worker.read_manifest(migrated, "test"), manifest)

    def test_existing_running_worker_is_verified_and_re_registered_without_rotating_token(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, manifest, token = self.fixture(directory)
            container = self.existing(config, paths, manifest, token)
            commands = []
            with patch.object(worker, "inspect_optional", side_effect=lambda kind, name: container if kind == "container" else self.network(config, paths)), \
                    patch.object(worker, "image_info", return_value=image()), \
                    patch.object(worker, "execute", side_effect=lambda command, **kwargs: commands.append(command) or ""):
                result = restore.restore(config, "test")
            self.assertEqual(result["state"], "running_preserved")
            self.assertEqual(paths["token"].read_text().strip(), token)
            self.assertFalse(any("create" in command or "start" in command for command in commands))
            self.assertTrue(any("apply" in command and "worker-network.py" in " ".join(command) for command in commands))
            container["State"] = {"Status": "exited", "Running": False}
            commands.clear()
            with patch.object(worker, "inspect_optional", side_effect=lambda kind, name: container if kind == "container" else self.network(config, paths)), \
                    patch.object(worker, "image_info", return_value=image()), \
                    patch.object(worker, "execute", side_effect=lambda command, **kwargs: commands.append(command) or ""):
                stopped = restore.restore(config, "test")
            self.assertEqual(stopped["state"], "stopped_started")
            self.assertTrue(any("start" in command and "worker-network.py" in " ".join(command) for command in commands))
            self.assertEqual(paths["token"].read_text().strip(), token)
            container["HostConfig"]["NetworkMode"] = "host"
            commands.clear()
            with patch.object(worker, "inspect_optional", side_effect=lambda kind, name: container if kind == "container" else self.network(config, paths)), \
                    patch.object(worker, "image_info", return_value=image()), \
                    patch.object(worker, "execute", side_effect=lambda command, **kwargs: commands.append(command) or ""):
                with self.assertRaises(ValueError):
                    restore.restore(config, "test")
            self.assertEqual(commands, [])
            self.assertEqual(paths["token"].read_text().strip(), token)

    def test_legacy_data_and_changed_custom_image_are_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            config, paths, manifest, token = self.fixture(directory, custom=True)
            commands = []
            with patch.object(worker, "inspect_optional", return_value=None), \
                    patch.object(worker, "image_info", return_value=image(IMAGE_B)), \
                    patch.object(worker, "execute", side_effect=lambda command, **kwargs: commands.append(command) or ""):
                with self.assertRaises(ValueError):
                    restore.restore(config, "test")
            self.assertEqual(commands, [])
            self.assertEqual(paths["token"].read_text().strip(), token)
            foreign = self.network(config, paths)
            foreign["Labels"] = {"io.grok-codex.realm": "another-project"}
            with patch.object(worker, "inspect_optional", side_effect=lambda kind, name: None if kind == "container" else foreign), \
                    patch.object(worker, "execute", side_effect=lambda command, **kwargs: commands.append(command) or ""):
                with self.assertRaises(ValueError):
                    restore.restore(config, "test")
            self.assertEqual(commands, [])
            self.assertEqual(paths["token"].read_text().strip(), token)
            saved_source = paths["source"].with_name("preserved-source")
            paths["source"].rename(saved_source)
            paths["source"].symlink_to(saved_source, target_is_directory=True)
            with patch.object(worker, "inspect_optional") as inspect:
                with self.assertRaises(ValueError):
                    restore.restore(config, "test")
                inspect.assert_not_called()
            self.assertEqual((saved_source / "keep.txt").read_text(), "uncommitted source\n")
            paths["manifest"].unlink()
            with patch.object(worker, "inspect_optional") as inspect:
                with self.assertRaisesRegex(ValueError, "manifest"):
                    restore.restore(config, "test")
                inspect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
