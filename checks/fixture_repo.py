import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description="Create a new local-only Git/skill/secret fixture for deployment verification")
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(mode=0o700)
    seed = root / "seed"
    inputs = root / "inputs"
    secrets = root / "secrets"
    seed.mkdir()
    inputs.mkdir()
    secrets.mkdir(mode=0o700)

    def git(*command):
        subprocess.run(["git", "-c", "user.name=Deployment probe", "-c", "user.email=probe@example.invalid", *command], check=True,
                       stdout=subprocess.DEVNULL)

    git("init", "--initial-branch=main", str(seed))
    (seed / "README.md").write_text("Local disposable fixture.\n")
    git("-C", str(seed), "add", ".")
    git("-C", str(seed), "commit", "-m", "Initial fixture")
    git("-C", str(seed), "checkout", "-b", "feature-probe")
    skill = seed / ".agents/skills/native-route-probe"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: native-route-probe\ndescription: Verify a requested repository skill in a disposable native deployment test.\n---\n\nWhen explicitly included, write exactly SKILL_F05_OK to skill-route.txt in the task's delivery directory, then read it back.\n")
    (seed / "ref-marker.txt").write_text("BASE_REF_OK")
    git("-C", str(seed), "add", ".")
    git("-C", str(seed), "commit", "-m", "Non-default fixture ref and skill")
    repo = inputs / "fixture.git"
    git("clone", "--bare", str(seed), str(repo))
    git("-C", str(repo), "update-ref", "refs/pull/123/head", "refs/heads/feature-probe")
    git("-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main")
    value = b"DISPOSABLE_SECRET_FIXTURE_ONLY"
    (secrets / "DEPLOYMENT_SENTINEL").write_bytes(value)
    (secrets / "DEPLOYMENT_SENTINEL").chmod(0o600)
    print(json.dumps({"inputsDir": str(inputs), "secretsDir": str(secrets),
                      "repo": "file:///run/grok-codex/inputs/fixture.git", "ref": "refs/pull/123/head",
                      "secretDigest": hashlib.sha256(value).hexdigest()}))


if __name__ == "__main__":
    main()
