import hashlib
import http.server
import io
import json
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import registry

REPO = "https://github.com/acme/widget"


def make_archive(manifest: dict | None, symlink: bool = False, padding: int = 0) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if manifest is not None:
            body = json.dumps(manifest).encode()
            info = tarfile.TarInfo("rpp.json")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        if symlink:
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        if padding:
            pad = tarfile.TarInfo("pad.bin")
            pad.size = padding
            tar.addfile(pad, io.BytesIO(bytes(padding)))
    return buf.getvalue()


def version_entry(version: str, archive: bytes = b"", **extra) -> dict:
    return {
        "version": version,
        "url": f"{REPO}/releases/download/v{version}/widget-{version}.rpp.tgz",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "rpp": ">=0.2",
        **extra,
    }


class RegistryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "plugins").mkdir()
        self.files: dict[str, bytes] = {}
        files = self.files

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = files.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"
        real_fetch = registry.fetch
        patcher = mock.patch.object(
            registry, "fetch", lambda url: real_fetch(url.replace("https://github.com", origin))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, version: str, archive: bytes) -> None:
        self.files[f"/acme/widget/releases/download/v{version}/widget-{version}.rpp.tgz"] = archive

    def write(self, versions: list[dict], name: str = "widget") -> None:
        plugin = {"name": name, "repository": REPO, "description": None, "versions": versions}
        (self.root / "plugins" / f"{name}.json").write_text(json.dumps(plugin, indent=2) + "\n")
        plugins, problems = registry.load_plugins(self.root)
        self.assertEqual(problems, [])
        (self.root / "index.json").write_text(registry.build_index(plugins))

    def commit(self) -> None:
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=self.root,
                check=True,
            )

    def test_index_orders_and_skips_yanked(self):
        self.assertLess(registry.semver_key("1.1.0-rc.2"), registry.semver_key("1.1.0-rc.10"))
        self.assertLess(registry.semver_key("1.1.0-rc.1"), registry.semver_key("1.1.0"))
        self.assertLess(registry.semver_key("1.2.0"), registry.semver_key("1.10.0"))
        self.write([version_entry("1.0.0"), version_entry("1.1.0-rc.1"), version_entry("1.1.0"),
                    version_entry("1.2.0", yanked=True), version_entry("1.3.0-beta.1")], "zeta")
        self.write([version_entry("1.0.0", yanked=True)], "alpha")
        self.write([version_entry("0.1.0")], "beta")
        self.write([version_entry("0.1.0-alpha.0"), version_entry("0.1.0-nightly.20261003.g9872875840f7")],
                   "gamma")
        index = json.loads((self.root / "index.json").read_text())
        self.assertEqual([(e["name"], e["latest"]) for e in index], [("beta", "0.1.0"), ("zeta", "1.1.0")])

    def test_nightly_formats_order_and_append(self):
        versions = [
            "0.1.0-alpha.0",
            "0.1.0-beta.2",
            "0.1.0-nightly.20261002.g67305fc8e4c3",
            "0.1.0-nightly.20261003.g9872875840f7",
            "0.1.0-nightly.20261004093000.g12336a991a34",
            "0.1.0-nightly.20261004181500.g0123456789ab",
            "0.1.0-rc.1",
            "0.1.0",
        ]
        keys = [registry.semver_key(v) for v in versions]
        self.assertEqual(keys, sorted(keys))
        self.write([version_entry(v) for v in versions[2:4]])
        self.commit()
        archive = make_archive({"name": "widget", "version": versions[4]})
        self.serve(versions[4], archive)
        self.write([version_entry(v) for v in versions[2:4]] + [version_entry(versions[4], archive)])
        self.assertEqual(registry.check(self.root, base="HEAD"), [])

    def test_rejects_edited_published_version(self):
        self.write([version_entry("1.0.0")])
        self.commit()
        self.write([version_entry("1.0.0", b"other")])
        problems = registry.check(self.root, base="HEAD")
        self.assertTrue(any("sha256 must not change" in p for p in problems), problems)

    def test_allows_yanking(self):
        self.write([version_entry("1.0.0")])
        self.commit()
        self.write([version_entry("1.0.0", yanked=True)])
        self.assertEqual(registry.check(self.root, base="HEAD"), [])

    def test_accepts_new_version(self):
        self.write([version_entry("1.0.0")])
        self.commit()
        archive = make_archive({"name": "widget", "version": "1.1.0"})
        self.serve("1.1.0", archive)
        self.write([version_entry("1.0.0"), version_entry("1.1.0", archive)])
        self.assertEqual(registry.check(self.root, base="HEAD"), [])

    def test_rejects_bad_hash(self):
        archive = make_archive({"name": "widget", "version": "1.0.0"})
        self.serve("1.0.0", archive)
        self.write([version_entry("1.0.0", b"different")])
        problems = registry.check(self.root, download=True)
        self.assertTrue(any("sha256 does not match" in p for p in problems), problems)

    def test_rejects_archive_with_wrong_manifest(self):
        archive = make_archive({"name": "widget", "version": "9.9.9"})
        self.serve("1.0.0", archive)
        self.write([version_entry("1.0.0", archive)])
        problems = registry.check(self.root, download=True)
        self.assertTrue(any("rpp.json must have name" in p for p in problems), problems)

    def test_rejects_symlink_entry(self):
        archive = make_archive({"name": "widget", "version": "1.0.0"}, symlink=True)
        self.serve("1.0.0", archive)
        self.write([version_entry("1.0.0", archive)])
        problems = registry.check(self.root, download=True)
        self.assertTrue(any("not a regular file" in p for p in problems), problems)

    def test_rejects_oversized_unpacked_archive(self):
        archive = make_archive({"name": "widget", "version": "1.0.0"}, padding=4096)
        with mock.patch.object(registry, "MAX_UNPACKED_BYTES", 2048):
            errors = registry.validate_archive(archive, "widget", "1.0.0")
        self.assertTrue(any("unpacks to more than" in e for e in errors), errors)

    def test_rejects_sparse_entry(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("sparse")
            info.type = tarfile.GNUTYPE_SPARSE
            tar.addfile(info)
        errors = registry.validate_archive(buf.getvalue(), "widget", "1.0.0")
        self.assertTrue(any("not a regular file" in e for e in errors), errors)

    def test_rejects_oversized_manifest(self):
        archive = make_archive({"name": "widget", "version": "1.0.0", "pad": "x" * 200})
        with mock.patch.object(registry, "MAX_MANIFEST_BYTES", 100):
            errors = registry.validate_archive(archive, "widget", "1.0.0")
        self.assertTrue(any("rpp.json exceeds" in e for e in errors), errors)

    def test_rejects_too_many_entries(self):
        archive = make_archive({"name": "widget", "version": "1.0.0"}, symlink=True)
        with mock.patch.object(registry, "MAX_ENTRIES", 1):
            errors = registry.validate_archive(archive, "widget", "1.0.0")
        self.assertTrue(any("more than 1 entries" in e for e in errors), errors)

    def test_rejects_inserted_version(self):
        self.write([version_entry("1.0.0"), version_entry("1.2.0")])
        self.commit()
        archive = make_archive({"name": "widget", "version": "1.1.0"})
        self.serve("1.1.0", archive)
        self.write([version_entry("1.0.0"), version_entry("1.1.0", archive), version_entry("1.2.0")])
        problems = registry.check(self.root, base="HEAD")
        self.assertTrue(any("must be appended" in p for p in problems), problems)

    def test_semver_is_ascii_and_anchored(self):
        self.assertFalse(registry.is_semver("1.0.0\n"))
        self.assertFalse(registry.is_semver("١.0.0"))
        self.assertTrue(registry.is_semver("1.0.0-rc.1+build.5"))

    def test_version_requirement_syntax(self):
        for ok in ("*", ">=0.2", "^1.2.3", "~1.2", ">=1, <2", "1.*", "=1.2.3-rc.1", "1.x.x"):
            self.assertTrue(registry.is_version_req(ok), ok)
        for bad in ("", " ", ">=", "1.2.3.4", "1.*.3", "latest", ">=1,", "01.2", "=>1", "1.0\n,"):
            self.assertFalse(registry.is_version_req(bad), bad)

    def test_stale_index_fails(self):
        self.write([version_entry("1.0.0")])
        (self.root / "index.json").write_text("[]\n")
        problems = registry.check(self.root)
        self.assertTrue(any(p.startswith("index.json:") for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
