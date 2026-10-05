#!/usr/bin/env python3
"""Maintain and validate the rpp plugin registry (index.json and plugins/*.json)."""

import argparse
import hashlib
import gzip
import io
import json
import re
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 10_000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PRE = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*"
SEMVER_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    rf"(?:-({_PRE}))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_NUM, _WILD = r"(?:0|[1-9][0-9]*)", r"[*xX]"
# Mirrors the Rust `semver` crate's VersionReq grammar.
_COMPARATOR = (
    rf"{_WILD}|(?:=|>=|>|<=|<|~|\^)?\s*{_NUM}"
    rf"(?:\.(?:{_NUM}(?:\.(?:{_NUM}(?:-{_PRE})?|{_WILD}))?|{_WILD}(?:\.{_WILD})?))?"
)
COMPARATOR_RE = re.compile(_COMPARATOR)


def semver_key(version: str) -> tuple:
    """Sort key following semver 2.0 precedence; build metadata is ignored."""
    m = SEMVER_RE.fullmatch(version)
    if not m:
        raise ValueError(f"invalid semver {version!r}")
    major, minor, patch, pre = m.groups()
    if pre is None:
        pre_key: tuple = (1,)
    else:
        parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
        pre_key = (0, parts)
    return (int(major), int(minor), int(patch), pre_key)


def is_semver(value: object) -> bool:
    return isinstance(value, str) and SEMVER_RE.fullmatch(value) is not None


def is_version_req(value: object) -> bool:
    return isinstance(value, str) and all(
        COMPARATOR_RE.fullmatch(part.strip()) for part in value.split(",")
    )


def load_plugins(root: Path) -> tuple[dict[str, dict], list[str]]:
    plugins: dict[str, dict] = {}
    problems: list[str] = []
    for path in sorted((root / "plugins").glob("*.json")):
        rel = f"plugins/{path.name}"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            problems.append(f"{rel}: unreadable JSON: {e}")
            continue
        errors = validate_plugin(path.stem, data)
        problems.extend(f"{rel}: {e}" for e in errors)
        if not errors:
            plugins[path.stem] = data
    return plugins, problems


def validate_plugin(stem: str, data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["expected a JSON object"]
    errors: list[str] = []
    name, repo = data.get("name"), data.get("repository")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        errors.append("invalid name")
    elif name != stem:
        errors.append(f"name {name!r} does not match file stem {stem!r}")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        errors.append("repository must be https://github.com/<owner>/<repo>")
        repo = None
    if data.get("description") is not None and not isinstance(data["description"], str):
        errors.append("description must be a string or null")
    versions = data.get("versions")
    if not isinstance(versions, list) or not versions:
        return errors + ["versions must be a non-empty list"]
    keys = []
    for i, v in enumerate(versions):
        label = f"versions[{i}]"
        if not isinstance(v, dict):
            errors.append(f"{label}: expected an object")
            continue
        if not is_semver(v.get("version")):
            errors.append(f"{label}: invalid semver version")
        else:
            label = f"version {v['version']}"
            keys.append(semver_key(v["version"]))
        url = v.get("url")
        if not isinstance(url, str):
            errors.append(f"{label}: url must be a string")
        elif repo and not url.startswith(f"{repo}/releases/download/"):
            errors.append(f"{label}: url must start with {repo}/releases/download/")
        if not isinstance(v.get("sha256"), str) or not SHA256_RE.fullmatch(v["sha256"]):
            errors.append(f"{label}: sha256 must be 64 lowercase hex characters")
        if not is_version_req(v.get("rpp")):
            errors.append(f"{label}: rpp must be a semver version requirement")
        if not isinstance(v.get("yanked", False), bool):
            errors.append(f"{label}: yanked must be a boolean")
    if len(keys) == len(versions) and any(a >= b for a, b in zip(keys, keys[1:])):
        errors.append("versions must be unique and sorted oldest-first")
    return errors


def build_index(plugins: dict[str, dict]) -> str:
    entries = []
    for name in sorted(plugins):
        p = plugins[name]
        # rpp's index.json schema requires a version for `latest`, so plugins without a
        # stable release are left out rather than listed with null or a prerelease.
        stable = [
            v["version"]
            for v in p["versions"]
            if not v.get("yanked", False) and SEMVER_RE.fullmatch(v["version"]).group(4) is None
        ]
        if stable:
            latest = max(stable, key=semver_key)
            entries.append(
                {
                    "name": p["name"],
                    "description": p.get("description"),
                    "repository": p["repository"],
                    "latest": latest,
                }
            )
    return json.dumps(entries, indent=2) + "\n"


def git_show(root: Path, ref: str, path: str) -> str | None:
    r = subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=root, capture_output=True, text=True
    )
    return r.stdout if r.returncode == 0 else None


def compare_to_base(head: dict, base: dict) -> list[str]:
    errors = []
    for field in ("name", "repository"):
        if head[field] != base.get(field):
            errors.append(f"{field} must not change")
    head_by_version = {v["version"]: v for v in head["versions"]}
    for bv in base["versions"]:
        hv = head_by_version.get(bv["version"])
        if hv is None:
            errors.append(f"published version {bv['version']} was removed")
            continue
        fields = (set(bv) | set(hv)) - {"yanked"}
        for f in sorted(fields):
            if bv.get(f) != hv.get(f):
                errors.append(f"published version {bv['version']}: {f} must not change")
        if bv.get("yanked", False) and not hv.get("yanked", False):
            errors.append(f"published version {bv['version']}: cannot be un-yanked")
    return errors


def fetch(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "rpp-registry"})
    with urlopen(req, timeout=60) as resp:
        data = resp.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds 128 MiB")
    return data


class _CappedReader:
    """Reads a gzip stream, failing once more than MAX_UNPACKED_BYTES come out."""

    def __init__(self, data: bytes):
        self._gz = gzip.GzipFile(fileobj=io.BytesIO(data))
        self._total = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._gz.read(size if size >= 0 else MAX_UNPACKED_BYTES + 1)
        self._total += len(chunk)
        if self._total > MAX_UNPACKED_BYTES:
            raise ValueError(f"archive unpacks to more than {MAX_UNPACKED_BYTES} bytes")
        return chunk


def validate_archive(data: bytes, name: str, version: str) -> list[str]:
    errors = []
    manifest = None
    try:
        with tarfile.open(fileobj=_CappedReader(data), mode="r|") as tar:
            for count, member in enumerate(tar, 1):
                if count > MAX_ENTRIES:
                    return [f"archive has more than {MAX_ENTRIES} entries"]
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    errors.append(f"unsafe path {member.name!r}")
                if not (member.isreg() or member.isdir()) or member.issparse():
                    errors.append(f"entry {member.name!r} is not a regular file or directory")
                elif member.size > MAX_FILE_BYTES:
                    errors.append(f"entry {member.name!r} exceeds {MAX_FILE_BYTES} bytes")
                elif member.isreg() and path.parts == ("rpp.json",):
                    if member.size > MAX_MANIFEST_BYTES:
                        errors.append(f"rpp.json exceeds {MAX_MANIFEST_BYTES} bytes")
                    else:
                        manifest = tar.extractfile(member).read()
    except (tarfile.TarError, OSError, EOFError, ValueError, zlib.error) as e:
        return [f"unreadable archive: {e}"]
    if errors:
        return errors
    if manifest is None:
        return ["archive has no rpp.json at its root"]
    try:
        info = json.loads(manifest)
    except ValueError as e:
        return [f"rpp.json is not valid JSON: {e}"]
    if not isinstance(info, dict) or info.get("name") != name or info.get("version") != version:
        return [f"rpp.json must have name {name!r} and version {version!r}"]
    return []


def check_archive(version: dict, name: str) -> list[str]:
    try:
        data = fetch(version["url"])
    except (OSError, ValueError) as e:
        return [f"download failed: {e}"]
    if hashlib.sha256(data).hexdigest() != version["sha256"]:
        return ["sha256 does not match the downloaded archive"]
    return validate_archive(data, name, version["version"])


def check(root: Path, base: str | None = None, download: bool = False) -> list[str]:
    plugins, problems = load_plugins(root)
    index_path = root / "index.json"
    stale = not index_path.is_file() or index_path.read_text("utf-8") != build_index(plugins)
    if stale and not problems:
        problems.append("index.json: out of date, run `python3 scripts/registry.py index`")

    new_versions: list[tuple[str, dict]] = []
    if base is not None:
        if subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
            cwd=root,
            capture_output=True,
        ).returncode:
            return problems + [f"--base: unknown git ref {base!r}"]
        listing = subprocess.run(
            ["git", "ls-tree", "--name-only", base, "plugins/"],
            cwd=root,
            capture_output=True,
            text=True,
        ).stdout.split()
        for rel in listing:
            if rel.endswith(".json") and Path(rel).stem not in plugins:
                if not (root / rel).is_file():
                    problems.append(f"{rel}: published plugin was removed")
        for name, plugin in plugins.items():
            rel = f"plugins/{name}.json"
            text = git_show(root, base, rel)
            try:
                old = json.loads(text) if text is not None else None
            except ValueError:
                old = None
            if old is None or validate_plugin(name, old):
                new_versions += [(name, v) for v in plugin["versions"]]
                continue
            problems.extend(f"{rel}: {e}" for e in compare_to_base(plugin, old))
            seen = {v["version"] for v in old["versions"]}
            new_versions += [(name, v) for v in plugin["versions"] if v["version"] not in seen]
    elif download:
        new_versions = [(n, v) for n, p in plugins.items() for v in p["versions"]]

    for name, v in new_versions:
        problems.extend(f"plugins/{name}.json: version {v['version']}: {e}" for e in check_archive(v, name))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("index", help="regenerate index.json")
    chk = sub.add_parser("check", help="validate plugins, index.json and new archives")
    chk.add_argument("--base", help="git ref to compare against")
    chk.add_argument("--download", action="store_true", help="verify every archive")
    args = parser.parse_args()

    if args.command == "index":
        plugins, problems = load_plugins(args.root)
        if problems:
            for problem in problems:
                print(problem)
            return 1
        (args.root / "index.json").write_text(build_index(plugins), encoding="utf-8")
        return 0
    problems = check(args.root, args.base, args.download)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
