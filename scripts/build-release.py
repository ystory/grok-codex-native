#!/usr/bin/env python3
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tomllib


def main():
    parser = argparse.ArgumentParser(description="Build a source-only deployment archive from the reviewed Git index")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    if subprocess.run(["git", "diff", "--quiet", "--", "."], cwd=project).returncode != 0:
        parser.error("Stage or commit all tracked project changes before packaging the reviewed Git index")
    version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    names = subprocess.check_output(["git", "ls-files", "-z", "--", "."], cwd=project).decode().split("\0")
    files = [project / name for name in names if name]
    if not files or not (project / "pyproject.toml") in files:
        parser.error("Stage or commit the reviewed project files before packaging")
    for path in files:
        relative = path.relative_to(project)
        if path.is_symlink() or not path.is_file():
            parser.error(f"Release inputs must be regular files: {relative}")
        if any(part in {".env", ".local", ".git", "__pycache__", ".venv"} for part in relative.parts):
            parser.error(f"Private or generated path was included in the Git index: {relative}")
    manifest = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(project).as_posix()}\n" for path in sorted(files))
    args.out.mkdir(parents=True, exist_ok=True)
    archive = args.out / f"grok-codex-native-{version}.tar.gz"
    with archive.open("xb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as target:
            for path in sorted(files):
                info = target.gettarinfo(str(path), arcname="grok-codex-native/" + path.relative_to(project).as_posix())
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                with path.open("rb") as content:
                    target.addfile(info, content)
            content = manifest.encode()
            info = tarfile.TarInfo("grok-codex-native/MANIFEST.sha256")
            info.mode, info.size = 0o644, len(content)
            target.addfile(info, io.BytesIO(content))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{digest}  {archive.name}\n")
    print(json.dumps({"archive": str(archive), "sha256": digest, "files": len(files)}))


if __name__ == "__main__":
    main()
