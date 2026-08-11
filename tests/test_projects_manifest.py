"""The committable ``session-kit.toml`` reader.

The manifest decides what a launch does, so these tests care about two things
above all: that a malformed or hostile file is refused by name rather than
half-applied, and that the hand-written subset parser reads a manifest to the
same values ``tomllib`` does — because a manifest that means one thing on
Python 3.13 and another on 3.10 is worse than no manifest at all.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from lib.sessionkit_projects import manifest

try:
    import tomllib
except ImportError:  # Python 3.10 is supported and has no tomllib.
    tomllib = None  # type: ignore[assignment]


FULL = """
# A complete manifest.
name = "demo-api"
description = "Demo API service"
root = "."
provider = "codex"
account = "work"
model = "gpt-5.1-codex"
startup = "sp msg main 'demo up'"

[[team]]
role = "reviewer"
provider = "claude"
model = "claude-opus-5"
expertise = "review"
scope = "Review the diff."

[[team]]
role = "builder"
provider = "codex"
model = "gpt-5.1-codex"
expertise = "implementation"
scope = "Implement the plan."
branch = "feature/demo"
"""


class SubsetParserTests(unittest.TestCase):
    def test_a_full_manifest_parses_to_its_exact_values(self) -> None:
        values = manifest.loads(FULL)
        self.assertEqual(values["name"], "demo-api")
        self.assertEqual(values["provider"], "codex")
        self.assertEqual(values["account"], "work")
        self.assertEqual(values["model"], "gpt-5.1-codex")
        self.assertEqual(values["startup"], "sp msg main 'demo up'")
        self.assertEqual(values["root"], ".")
        self.assertEqual(
            [role["role"] for role in values["team"]], ["reviewer", "builder"]
        )
        self.assertEqual(values["team"][1]["branch"], "feature/demo")
        self.assertIsNone(values["team"][0]["branch"])

    def test_comments_blank_lines_and_trailing_comments_are_ignored(self) -> None:
        values = manifest.loads(
            '# leading\n\nname = "demo" # trailing\n\n# between\nprovider = "claude"\n'
        )
        self.assertEqual(values["name"], "demo")
        self.assertEqual(values["provider"], "claude")

    def test_literal_and_escaped_strings_read_the_same_as_toml_says(self) -> None:
        parsed = manifest.parse_subset(
            "literal = 'a \\\\ backslash'\n"
            'escaped = "line\\nbreak\\ttab \\u00e9 \\"quoted\\""\n'
        )
        self.assertEqual(parsed["literal"], "a \\\\ backslash")
        self.assertEqual(parsed["escaped"], 'line\nbreak\ttab \u00e9 "quoted"')

    def test_booleans_integers_and_arrays_parse(self) -> None:
        # No manifest field uses these yet; the parser supports them so a
        # future field does not need a second parser written under pressure.
        parsed = manifest.parse_subset("a = true\nb = -12\nc = [1, 2]\nd = []\n")
        self.assertEqual(parsed, {"a": True, "b": -12, "c": [1, 2], "d": []})

    def test_an_unknown_top_level_key_is_refused_by_name(self) -> None:
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.loads('name = "demo"\nprovidr = "claude"\n')
        self.assertIn("providr", str(caught.exception))

    def test_an_unknown_section_is_refused_with_its_line_number(self) -> None:
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.loads('name = "demo"\n[settings]\nmodel = "x"\n')
        self.assertIn("line 2", str(caught.exception))
        self.assertIn("[settings]", str(caught.exception))

    def test_an_unknown_array_section_is_refused(self) -> None:
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.loads("[[workers]]\nrole = 'a'\n")
        self.assertIn("[[workers]]", str(caught.exception))

    def test_a_duplicate_key_is_refused_rather_than_last_one_winning(self) -> None:
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.loads('model = "a"\nmodel = "b"\n')
        self.assertIn("set twice", str(caught.exception))

    def test_a_key_after_a_team_header_belongs_to_that_team_as_toml_says(self) -> None:
        # Matching TOML here is what keeps the subset a subset. The documented
        # layout puts project settings above the first [[team]] for this reason.
        values = manifest.loads("[[team]]\nrole = 'a'\n\nmodel = 'claude-opus-5'\n")
        self.assertIsNone(values["model"])
        self.assertEqual(values["team"][0]["model"], "claude-opus-5")

    def test_unsupported_toml_constructs_are_refused_not_guessed(self) -> None:
        for text, expected in (
            ('name = """multi"""\n', "multi-line"),
            ("name = { a = 1 }\n", "inline tables"),
            ('name = "unterminated\n', "closing quote"),
            ("name = 2026-08-11\n", "only strings"),
            ('name = "a" garbage\n', "unexpected text"),
            ('"quoted" = "a"\n', "quoted keys"),
            ("name\n", "expected key = value"),
            ('name = "\\q"\n', "unsupported string escape"),
            ('name = "\\u00"\n', "four hex digits"),
            ("a = [[1]]\n", "nested arrays"),
            ("a = [1 2]\n", "separated by commas"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(manifest.ManifestError) as caught:
                    manifest.parse_subset(text)
                self.assertIn(expected, str(caught.exception))

    @unittest.skipIf(tomllib is None, "tomllib is unavailable on this Python")
    def test_the_subset_parser_agrees_with_real_toml(self) -> None:
        """The subset is a subset: where both read a file, they must agree."""
        for text in (
            FULL,
            'name = "demo"\nstartup = "a\\nb"\n',
            "# only comments\n",
            "[[team]]\nrole = 'solo'\n",
        ):
            with self.subTest(text=text[:40]):
                mine = manifest.parse_subset(text)
                theirs = tomllib.loads(text)
                self.assertEqual(mine, theirs)


class ValidationTests(unittest.TestCase):
    def test_an_absolute_root_is_refused(self) -> None:
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.loads('root = "/etc"\n')
        self.assertIn("not absolute", str(caught.exception))

    def test_a_root_that_climbs_out_of_the_manifest_is_refused(self) -> None:
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.loads('root = "../elsewhere"\n')
        self.assertIn("climb above", str(caught.exception))

    def test_a_relative_root_is_normalised(self) -> None:
        self.assertEqual(manifest.loads('root = "./src//app/"\n')["root"], "src/app")
        self.assertEqual(manifest.loads("")["root"], ".")

    def test_field_shapes_are_enforced(self) -> None:
        for text, expected in (
            ('name = "Demo Api"\n', "name is not in the accepted form"),
            ('provider = "gemini"\n', "provider must be claude, codex, or shell"),
            ('account = "WORK"\n', "account is not in the accepted form"),
            ('model = "a model"\n', "model is not in the accepted form"),
            ("[[team]]\nrole = 'a'\nprovider = 'shell'\n", "must be claude or codex"),
            ("[[team]]\nmodel = 'a'\n", "needs a role name"),
            ("[[team]]\nrole = 'a'\nnope = 'b'\n", "unknown key"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(manifest.ManifestError) as caught:
                    manifest.loads(text)
                self.assertIn(expected, str(caught.exception))

    def test_two_team_roles_may_not_share_a_name(self) -> None:
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.loads("[[team]]\nrole = 'a'\n\n[[team]]\nrole = 'a'\n")
        self.assertIn("share one role name", str(caught.exception))

    def test_control_characters_are_refused_in_text(self) -> None:
        # A startup command is a line this host would run. An escape sequence
        # can repaint the terminal that is asking whether to approve it, and a
        # newline turns one reviewed command into two.
        for text in (
            'startup = "clear\\u001b[2J"\n',
            'startup = "one\\ntwo"\n',
            'startup = "one\\ttwo"\n',
            'description = "a\\u0007b"\n',
        ):
            with self.subTest(text=text):
                with self.assertRaises(manifest.ManifestError) as caught:
                    manifest.loads(text)
                self.assertIn("control characters", str(caught.exception))

    def test_an_empty_manifest_is_valid_and_asks_for_nothing(self) -> None:
        values = manifest.loads("")
        self.assertIsNone(values["name"])
        self.assertIsNone(values["provider"])
        self.assertEqual(values["team"], [])

    def test_a_blank_string_reads_as_unset_rather_than_empty(self) -> None:
        self.assertIsNone(manifest.loads('model = "   "\n')["model"])


class ReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="session-kit-manifest.")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def write(self, text: str, name: str = manifest.MANIFEST_NAME) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_file_reads_to_validated_values(self) -> None:
        values = manifest.read(self.write(FULL))
        self.assertEqual(values["name"], "demo-api")
        self.assertEqual(len(values["team"]), 2)

    def test_a_symlinked_manifest_is_refused(self) -> None:
        real = self.write('name = "real"\n', name="real.toml")
        link = self.root / manifest.MANIFEST_NAME
        link.symlink_to(real)
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.read(link)
        self.assertIn("symlink", str(caught.exception))

    def test_an_oversized_manifest_is_refused_before_it_is_read(self) -> None:
        path = self.write("# " + "x" * (manifest.MAX_MANIFEST_BYTES + 1))
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.read(path)
        self.assertIn("larger than", str(caught.exception))

    def test_a_manifest_with_too_many_lines_is_refused(self) -> None:
        path = self.write("\n" * (manifest.MAX_MANIFEST_LINES + 1))
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.read(path)
        self.assertIn("longer than", str(caught.exception))

    def test_more_team_roles_than_the_bound_are_refused(self) -> None:
        text = "".join(
            f"[[team]]\nrole = 'r{index}'\n\n"
            for index in range(manifest.MAX_TEAM_ROLES + 1)
        )
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.loads(text)
        self.assertIn("more than", str(caught.exception))

    def test_invalid_utf8_and_null_bytes_are_refused(self) -> None:
        path = self.root / manifest.MANIFEST_NAME
        path.write_bytes(b'name = "\xff\xfe"\n')
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.read(path)
        self.assertIn("valid UTF-8", str(caught.exception))
        path.write_text('name = "a\x00b"\n', encoding="utf-8")
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.read(path)
        self.assertIn("null byte", str(caught.exception))

    def test_a_missing_manifest_reports_the_path(self) -> None:
        with self.assertRaises(manifest.ManifestError) as caught:
            manifest.read(self.root / "absent.toml")
        self.assertIn("absent.toml", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
