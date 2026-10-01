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


def make_archive(manifest: dict | None, symlink: bool = False) -> bytes:
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
                    version_entry("1.2.0", yanked=True)], "zeta")
        self.write([version_entry("1.0.0", yanked=True)], "alpha")
        self.write([version_entry("0.1.0")], "beta")
        index = json.loads((self.root / "index.json").read_text())
        self.assertEqual([(e["name"], e["latest"]) for e in index], [("beta", "0.1.0"), ("zeta", "1.1.0")])

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

    def test_stale_index_fails(self):
        self.write([version_entry("1.0.0")])
        (self.root / "index.json").write_text("[]\n")
        problems = registry.check(self.root)
        self.assertTrue(any(p.startswith("index.json:") for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
