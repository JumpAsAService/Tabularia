"""Tests for image_licence_check.py: what a package's declared licence means for an
AGPL-3.0 image, and that `check` fails exactly when one cannot be shipped.

  python3 .github/scripts/test_image_licence_check.py -v
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import image_licence_check as lc  # noqa: E402


def py(name, expression=None, license=None, classifiers=()):
    return {"kind": "python", "name": name, "version": "1.0", "expression": expression,
            "license": license, "classifiers": [f"License :: {c}" for c in classifiers]}


def npm(name, license):
    return {"kind": "npm", "name": name, "version": "1.0", "license": license}


class Expressions(unittest.TestCase):
    def ok(self, text):
        self.assertTrue(lc.expression_ok(text)[0], text)

    def bad(self, text):
        self.assertFalse(lc.expression_ok(text)[0], text)

    def test_permissive_and_weak_copyleft_pass(self):
        for text in ("MIT", "Apache-2.0", "BSD-3-Clause", "MPL-2.0", "LGPL-2.1", "LGPL-2.1-only", "ISC", "PSF-2.0",
                     "UPL-1.0 OR Apache-2.0", "MPL-2.0 AND (Apache-2.0 OR MIT)", "GPL-2.0-or-later", "GPL-2.0+",
                     "GPL-3.0-only", "AGPL-3.0-only", "Unlicense", "0BSD", "CC0-1.0"):
            self.ok(text)

    def test_what_an_agpl_image_cannot_carry_fails(self):
        for text in ("Proprietary", "LicenseRef-Proprietary", "SSPL-1.0", "BUSL-1.1", "Elastic-2.0",
                     "MIT AND Commons-Clause", "CC-BY-NC-4.0", "PolyForm-Noncommercial-1.0.0",
                     "GPL-2.0-only", "GPL-2.0", "UNLICENSED", "MIT AND GPL-2.0-only"):
            self.bad(text)

    def test_an_alternative_saves_it_and_a_conjunction_does_not(self):
        self.ok("BSD-3-Clause OR GPL-2.0")
        self.ok("(GPL-2.0-only OR MIT)")
        self.bad("(GPL-2.0-only OR SSPL-1.0) AND MIT")
        self.bad("GPL-2.0-only WITH Classpath-exception-2.0")

    def test_lgpl_is_not_mistaken_for_gpl(self):
        self.ok("LGPL-2.0-only")
        self.ok("LGPL-2.0")

    def test_free_text_is_read_for_the_known_bad_words(self):
        self.ok("BSD 3-Clause License")
        self.ok("LGPL with exceptions")
        self.ok("Dual License")
        self.bad("Other/Proprietary License")
        self.bad("Server Side Public License")
        self.bad("Business Source License 1.1")
        self.bad("free for non-commercial use")


class Packages(unittest.TestCase):
    def test_the_sql_server_odbc_driver_fails(self):
        ok, why = lc.verdict(py("mssql-python-odbc", license="Other/Proprietary License",
                                classifiers=["Other/Proprietary License"]))
        self.assertFalse(ok)
        self.assertIn("Proprietary", why)

    def test_the_expression_is_authoritative_over_the_classifiers(self):
        self.assertTrue(lc.verdict(py("x", expression="MIT", classifiers=["Other/Proprietary License"]))[0])

    def test_a_proprietary_or_gpl2_classifier_fails_without_an_expression(self):
        self.assertFalse(lc.verdict(py("x", classifiers=["Other/Proprietary License"]))[0])
        self.assertFalse(lc.verdict(py("x", classifiers=["OSI Approved :: GNU General Public License v2 (GPLv2)"]))[0])
        self.assertTrue(lc.verdict(py("x", classifiers=["OSI Approved :: GNU General Public License v2 or later (GPLv2+)"]))[0])
        self.assertTrue(lc.verdict(py("x", classifiers=["OSI Approved :: GNU Library or Lesser General Public License (LGPL)"]))[0])

    def test_what_the_images_hold_today_passes(self):
        for p in (py("psycopg2-binary", license="LGPL with exceptions",
                     classifiers=["OSI Approved :: GNU Library or Lesser General Public License (LGPL)"]),
                  py("python-dateutil", license="Dual License",
                     classifiers=["OSI Approved :: BSD License", "OSI Approved :: Apache Software License"]),
                  py("polars", license="Copyright (c) 2025 Ritchie Vink", classifiers=["OSI Approved :: MIT License"]),
                  py("paramiko", expression="LGPL-2.1"),
                  py("oracledb", expression="UPL-1.0 OR Apache-2.0"),
                  py("certifi", license="MPL-2.0", classifiers=["OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)"]),
                  npm("vue", "MIT"), npm("@intlify/shared", "MIT")):
            self.assertTrue(lc.verdict(p)[0], p["name"])

    def test_npm_forms_of_the_licence(self):
        self.assertTrue(lc.verdict(npm("a", {"type": "MIT"}))[0])
        self.assertTrue(lc.verdict(npm("a", [{"type": "GPL-2.0"}, {"type": "MIT"}]))[0])  # old dual form
        self.assertFalse(lc.verdict(npm("a", "UNLICENSED"))[0])
        self.assertFalse(lc.verdict(npm("a", "SEE LICENSE IN EULA.txt"))[0])

    def test_no_declared_licence_is_reported_not_failed(self):
        ok, why = lc.verdict(py("mssql-python"))
        self.assertTrue(ok)
        self.assertEqual(why, lc.UNDECLARED)


class Reviewed(unittest.TestCase):
    def test_node_forge_is_reviewed_for_its_declared_licence(self):
        self.assertIn(("npm", "node-forge"), lc.REVIEWED)
        failures, _ = lc.check([npm("node-forge", "(BSD-3-Clause OR GPL-2.0)")])
        self.assertEqual(failures, [])

    def test_a_reviewed_package_whose_licence_changes_is_checked_again(self):
        reviewed = {("npm", "dual"): ("MIT OR SSPL-1.0", "reviewed: used under MIT")}
        self.assertEqual(lc.check([npm("dual", "SSPL-1.0")], reviewed)[0][0]["name"], "dual")
        bad_but_reviewed = {("npm", "odd"): ("Commons Clause", "reviewed for the test")}
        self.assertEqual(lc.check([npm("odd", "Commons Clause")], bad_but_reviewed)[0], [])


class Main(unittest.TestCase):
    def run_main(self, packages):
        with tempfile.TemporaryDirectory() as d:
            listing = Path(d) / "packages.json"
            listing.write_text(json.dumps(packages))
            out = io.StringIO()
            with redirect_stdout(out):
                code = lc.main(["check", "--from-json", str(listing), "image:test"])
            return code, out.getvalue()

    def test_an_image_with_the_odbc_driver_fails_naming_it(self):
        code, log = self.run_main([py("mssql-python"), py("mssql-python-odbc", classifiers=["Other/Proprietary License"]),
                                   py("polars", classifiers=["OSI Approved :: MIT License"])])
        self.assertEqual(code, 1)
        self.assertIn("::error::", log)
        self.assertIn("mssql-python-odbc", log)
        self.assertIn("mssql-python: no declared licence", log)

    def test_a_clean_image_passes(self):
        code, log = self.run_main([py("polars", classifiers=["OSI Approved :: MIT License"]), npm("vue", "MIT")])
        self.assertEqual(code, 0)
        self.assertIn("2 packages", log)

    def test_an_empty_listing_cannot_certify(self):
        code, _ = self.run_main([])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
