import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build-release.py"


def test_release_contains_reviewed_files_and_rejects_unstaged_changes(tmp_path):
    repo = tmp_path / "repo"
    project = repo / "codex-native"
    (project / "scripts").mkdir(parents=True)
    (project / "deploy").mkdir()
    shutil.copyfile(SCRIPT, project / "scripts/build-release.py")
    (project / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (project / "deploy/.env").write_text("PRIVATE_TEST_SENTINEL\n")
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "add", "codex-native/scripts/build-release.py", "codex-native/pyproject.toml"], cwd=repo, check=True)
    command = [sys.executable, str(project / "scripts/build-release.py"), "--out"]
    subprocess.run(command + [str(tmp_path / "first")], check=True, capture_output=True)
    subprocess.run(command + [str(tmp_path / "second")], check=True, capture_output=True)
    first = tmp_path / "first/grok-codex-native-0.1.0.tar.gz"
    assert first.read_bytes() == (tmp_path / "second" / first.name).read_bytes()
    with tarfile.open(first) as archive:
        assert set(archive.getnames()) == {
            "grok-codex-native/pyproject.toml", "grok-codex-native/scripts/build-release.py", "grok-codex-native/MANIFEST.sha256",
        }
        manifest = archive.extractfile("grok-codex-native/MANIFEST.sha256").read().decode()
        for line in manifest.splitlines():
            digest, relative = line.split("  ", 1)
            assert hashlib.sha256(archive.extractfile("grok-codex-native/" + relative).read()).hexdigest() == digest
    (project / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n')
    refused = subprocess.run(command + [str(tmp_path / "dirty")], capture_output=True, text=True)
    assert refused.returncode != 0
    assert "Stage or commit" in refused.stderr
    assert not (tmp_path / "dirty").exists()


def test_release_refuses_indexed_private_file(tmp_path):
    project = tmp_path / "repo"
    (project / "scripts").mkdir(parents=True)
    (project / "deploy").mkdir()
    shutil.copyfile(SCRIPT, project / "scripts/build-release.py")
    (project / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (project / "deploy/.env").write_text("PRIVATE_TEST_SENTINEL\n")
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    refused = subprocess.run([sys.executable, str(project / "scripts/build-release.py"), "--out", str(tmp_path / "out")],
                             capture_output=True, text=True)
    assert refused.returncode != 0
    assert "Private or generated path" in refused.stderr
