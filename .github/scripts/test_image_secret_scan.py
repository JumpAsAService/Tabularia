"""Tests for image_secret_scan.py: fingerprints pick up secrets where they actually live,
and `scan` finds them in every corner of an image without false alarms.

  python3 .github/scripts/test_image_secret_scan.py -v
"""
import base64
import gzip
import hashlib
import io
import json
import os
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import image_secret_scan as scan  # noqa: E402

FAKE_PEM = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(
    base64.b64encode(secrets.token_bytes(48)).decode() for _ in range(6)) + "\n-----END PRIVATE KEY-----\n"


def fingerprints_of(*files: Path, repo: str = "") -> str:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "fingerprints.txt"
        assert scan.main(["fingerprint", "--repo", repo, "-o", str(out), *map(str, files)]) == 0
        return out.read_text()


def tar_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def image(folder: Path, layers: list[dict[str, bytes]], config: bytes = b"{}") -> Path:
    """A tar shaped like `docker save`: manifest, config, one tar per layer."""
    files = {"config.json": config}
    names = []
    for i, layer in enumerate(layers):
        names.append(f"layer{i}/layer.tar")
        files[names[-1]] = tar_of(layer)
    files["manifest.json"] = json.dumps([{"Config": "config.json", "RepoTags": ["x:1"], "Layers": names}]).encode()
    path = folder / "image.tar"
    path.write_bytes(tar_of(files))
    return path


class Fingerprint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def labels(self, text):
        return {label for *_, label in scan.parse_fingerprints(text)}

    def test_env_takes_the_secrets_and_leaves_the_rest(self):
        env = self.dir / ".env"
        env.write_text("# comment\nSTORAGE__SECRET_KEY='abcdefghijklmnopqrstuvwxyz012345'\n"
                       "STORAGE__ENDPOINT=https://s3.example.com/a/long/enough/path\n"
                       "DB__PASSWORD=short\nexport JWT__SECRET=0123456789abcdef0123456789abcdef\n")
        self.assertEqual(self.labels(fingerprints_of(env)), {".env:STORAGE__SECRET_KEY", ".env:JWT__SECRET"})

    def test_a_quoted_value_is_fingerprinted_without_the_quotes(self):
        env = self.dir / ".env"
        env.write_text('AI__SECRET_KEY="11111111-2222-3333-4444-555555555555"\n')
        (length, _, sha, _), = scan.parse_fingerprints(fingerprints_of(env))
        self.assertEqual(length, 36)
        self.assertEqual(sha, hashlib.sha256(b"11111111-2222-3333-4444-555555555555").hexdigest())

    def test_google_service_account(self):
        key = {"type": "service_account", "project_id": "p", "private_key_id": "f" * 40,
               "private_key": FAKE_PEM, "client_email": "robot@project-123.iam.gserviceaccount.com"}
        f = self.dir / "sa.json"
        f.write_text(json.dumps(key))
        labels = self.labels(fingerprints_of(f))
        self.assertIn("sa.json:private_key_id", labels)
        self.assertIn("sa.json:client_email", labels)
        self.assertEqual(sum(1 for x in labels if ":line" in x), 6)  # the six body lines

    def test_service_account_in_base64_inside_an_env_file(self):
        key = {"type": "service_account", "private_key_id": "e" * 40, "private_key": FAKE_PEM}
        env = self.dir / ".env"
        env.write_text("BIGQUERY__CREDENTIALS_B64=" + base64.b64encode(json.dumps(key).encode()).decode() + "\n")
        labels = self.labels(fingerprints_of(env))
        self.assertIn(".env:BIGQUERY__CREDENTIALS_B64", labels)
        self.assertIn(".env:BIGQUERY__CREDENTIALS_B64(base64):private_key_id", labels)
        self.assertEqual(sum(1 for x in labels if "(base64):private_key:" in x), 6)

    def test_docker_config_and_url_with_password(self):
        token = "dckr_pat_" + "x" * 30
        f = self.dir / "config.json"
        f.write_text(json.dumps({"auths": {"https://index.docker.io/v1/": {
            "auth": base64.b64encode(f"me:{token}".encode()).decode()}}}))
        g = self.dir / ".git-credentials"
        g.write_text("https://user:ghp_" + "y" * 36 + "@github.com\n")
        shas = {sha for _, _, sha, _ in scan.parse_fingerprints(fingerprints_of(f, g))}
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), shas)
        self.assertIn(hashlib.sha256(b"ghp_" + b"y" * 36).hexdigest(), shas)

    def test_names_that_point_at_a_secret_are_not_secrets(self):
        f = self.dir / "sa.json"
        f.write_text(json.dumps({"token_uri": "https://oauth2.googleapis.com/token",
                                 "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                                 "client_secret": "s" * 24}))
        env = self.dir / ".env"
        env.write_text("GOOGLE_KEY_FILE=/run/secrets/google-service-account.json\nAPI_TOKEN=" + "t" * 20 + "\n")
        self.assertEqual(self.labels(fingerprints_of(f, env)), {"sa.json:client_secret", ".env:API_TOKEN"})

    def test_kubeconfig_with_the_key_in_base64(self):
        k = self.dir / "config"
        k.write_text("users:\n- name: x\n  user:\n    client-key-data: "
                     + base64.b64encode(FAKE_PEM.encode()).decode() + "\n")
        labels = self.labels(fingerprints_of(k))
        self.assertEqual(sum(1 for x in labels if "(base64):key0:line" in x), 6)

    def test_values_already_in_the_repository_are_left_out(self):
        repo = self.dir / "repo"
        repo.mkdir()
        (repo / "example.env").write_text("JWT__SECRET=change-me-before-production-please\n")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        env = self.dir / ".env"
        env.write_text("JWT__SECRET=change-me-before-production-please\nSTORAGE__SECRET_KEY=" + "q" * 32 + "\n")
        self.assertEqual(self.labels(fingerprints_of(env, repo=str(repo))), {".env:STORAGE__SECRET_KEY"})

    def test_only_the_owner_can_read_the_fingerprint_file(self):
        env = self.dir / ".env"
        env.write_text("A_TOKEN=" + "t" * 20 + "\n")
        out = self.dir / "fingerprints.txt"
        scan.main(["fingerprint", "--repo", "", "-o", str(out), str(env)])
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)

    def test_a_missing_file_stops_everything(self):
        self.assertEqual(scan.main(["fingerprint", "--repo", "", str(self.dir / "missing.env")]), 2)


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.secret = "SCW" + secrets.token_hex(16)            # 35 characters
        self.odd = "p@ss:w/rd!#\"' " + secrets.token_hex(8)    # characters that split words
        env = self.dir / ".env"
        env.write_text(f"STORAGE__SECRET_KEY={self.secret}\nDB__PASSWORD={self.odd}\n")
        self.fingerprints = self.dir / "fingerprints.txt"
        self.fingerprints.write_text(fingerprints_of(env))

    def tearDown(self):
        self.tmp.cleanup()

    def run_scan(self, path, chunk=scan.CHUNK):
        scanner = scan.Scanner(scan.parse_fingerprints(self.fingerprints.read_text()), chunk=chunk)
        scanner.image(str(path))
        return scanner

    def places(self, scanner):
        return {where for _, where, _ in scanner.found}

    def test_clean_image(self):
        noise = secrets.token_bytes(3_000_000)
        img = image(self.dir, [{"usr/lib/noise.bin": noise, "app/main.py": b"print('hello')\n" * 1000}])
        self.assertEqual(self.run_scan(img).found, [])
        self.assertEqual(scan.main(["scan", str(img), "--fingerprints", str(self.fingerprints)]), 0)

    def test_plain_secret_at_every_alignment(self):
        # the prefilter reads 8-byte windows from eight different starts: each must be covered
        for shift in range(9):
            with self.subTest(shift=shift):
                img = image(self.dir, [{"app/config.py": b"x" * shift + self.secret.encode() + b"\n"}])
                self.assertEqual(self.places(self.run_scan(img)), {"layer 1:/app/config.py"})

    def test_secret_at_the_very_end_of_a_file(self):
        img = image(self.dir, [{"app/tail": b"a" * 1001 + self.secret.encode()}])
        self.assertEqual(len(self.run_scan(img).found), 1)

    def test_characters_that_split_words(self):
        img = image(self.dir, [{"etc/conf": b"password=" + self.odd.encode() + b";\n"}])
        self.assertEqual([label for label, _, _ in self.run_scan(img).found], [".env:DB__PASSWORD"])

    def test_deleted_by_a_later_layer(self):
        img = image(self.dir, [{"app/.env": self.secret.encode()}, {"app/.wh..env": b""}])
        self.assertEqual(self.places(self.run_scan(img)), {"layer 1:/app/.env"})

    def test_straddling_two_chunks(self):
        for start in range(56, 72):  # 64-byte chunks: the secret crosses the boundary
            with self.subTest(start=start):
                img = image(self.dir, [{"data.bin": b"z" * start + self.secret.encode() + b"z" * 200}])
                self.assertEqual(len(self.run_scan(img, chunk=64).found), 1)

    def test_inside_zip_gzip_and_tar(self):
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("package/settings.py", f"KEY = '{self.secret}'\n")
        tgz = gzip.compress(tar_of({"inner/.env": f"S={self.secret}".encode()}))
        img = image(self.dir, [{"opt/x.whl": z.getvalue(), "opt/y.tar.gz": tgz,
                                "opt/z.gz": gzip.compress(self.secret.encode())}])
        # an uncompressed tar holds its files verbatim: the secret shows in the stream and in the member
        self.assertEqual(self.places(self.run_scan(img)), {
            "layer 1:/opt/x.whl!package/settings.py",
            "layer 1:/opt/y.tar.gz!gunzip",
            "layer 1:/opt/y.tar.gz!gunzip!inner/.env",
            "layer 1:/opt/z.gz!gunzip"})

    def test_in_the_image_config(self):
        config = json.dumps({"config": {"Env": [f"STORAGE__SECRET_KEY={self.secret}"]}}).encode()
        img = image(self.dir, [{"a": b"nothing"}], config=config)
        self.assertEqual(self.places(self.run_scan(img)), {"config.json"})

    def test_found_means_exit_1_and_no_value_printed(self):
        img = image(self.dir, [{"app/.env": self.secret.encode()}])
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            code = scan.main(["scan", str(img), "--fingerprints", str(self.fingerprints)])
        finally:
            sys.stdout = old
        self.assertEqual(code, 1)
        self.assertIn("FOUND .env:STORAGE__SECRET_KEY", out.getvalue())
        self.assertNotIn(self.secret, out.getvalue())

    def test_no_fingerprints_no_certificate(self):
        img = image(self.dir, [{"a": b"b"}])
        os.environ.pop("SECRET_FINGERPRINTS", None)
        self.assertEqual(scan.main(["scan", str(img)]), 2)
        empty = self.dir / "empty.txt"
        empty.write_text("# just a comment\n")
        self.assertEqual(scan.main(["scan", str(img), "--fingerprints", str(empty)]), 2)

    def test_a_file_that_is_not_an_image(self):
        fake = self.dir / "fake.tar"
        fake.write_bytes(tar_of({"a.txt": b"hello"}))
        self.assertEqual(scan.main(["scan", str(fake), "--fingerprints", str(self.fingerprints)]), 2)

    def test_fingerprints_from_the_environment_variable(self):
        img = image(self.dir, [{"app/.env": self.secret.encode()}])
        os.environ["SECRET_FINGERPRINTS"] = self.fingerprints.read_text()
        try:
            self.assertEqual(scan.main(["scan", str(img)]), 1)
        finally:
            del os.environ["SECRET_FINGERPRINTS"]


if __name__ == "__main__":
    unittest.main()
