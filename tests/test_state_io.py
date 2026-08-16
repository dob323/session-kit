"""The safe-read discipline for private state files, pinned.

``read_private_json`` guards every state read on this machine: O_NOFOLLOW, a
regular file only, exactly mode 0600, owned by the current user, bounded
bytes. Its previous coverage lived in a doctor test retired with the one-door
rebuild (2026-08-12); these tests pin the refusals directly so a regression
that follows a symlink or reads a loose-mode file cannot ship undetected.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

from sessionkit_inventory.state_io import (  # noqa: E402
    CollectionError,
    _read_state_json,
    atomic_write_json,
    atomic_write_private_json,
    read_private_json,
)


class ReadPrivateJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self._raw = tempfile.TemporaryDirectory(prefix=".state-io-")
        self.addCleanup(self._raw.cleanup)
        self.root = Path(self._raw.name)

    def _private(self, name: str, payload: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_a_conforming_file_reads(self) -> None:
        path = self._private("ok.json", {"answer": 42})
        self.assertEqual({"answer": 42}, read_private_json(path, max_bytes=4096))

    def test_a_planted_symlink_is_refused_not_followed(self) -> None:
        real = self._private("real.json", {"secret": True})
        link = self.root / "link.json"
        link.symlink_to(real)
        with self.assertRaises(CollectionError):
            read_private_json(link, max_bytes=4096)

    def test_a_loose_mode_is_refused(self) -> None:
        for mode in (0o640, 0o644, 0o666, 0o400):
            with self.subTest(mode=oct(mode)):
                path = self._private(f"mode-{mode}.json", {})
                os.chmod(path, mode)
                with self.assertRaises(CollectionError):
                    read_private_json(path, max_bytes=4096)

    def test_a_directory_is_refused(self) -> None:
        directory = self.root / "dir.json"
        directory.mkdir(mode=0o700)
        with self.assertRaises(CollectionError):
            read_private_json(directory, max_bytes=4096)

    def test_an_oversized_file_is_refused_not_truncated(self) -> None:
        path = self._private("big.json", {})
        path.write_text('{"pad": "' + "x" * 200 + '"}', encoding="utf-8")
        os.chmod(path, 0o600)
        with self.assertRaises(CollectionError):
            read_private_json(path, max_bytes=64)

    def test_missing_files_follow_the_allow_missing_switch(self) -> None:
        absent = self.root / "absent.json"
        self.assertIsNone(read_private_json(absent, max_bytes=64, allow_missing=True))
        with self.assertRaises(CollectionError):
            read_private_json(absent, max_bytes=64)


class MutableStateReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._raw = tempfile.TemporaryDirectory(prefix=".mutable-state-")
        self.addCleanup(self._raw.cleanup)
        self.root = Path(self._raw.name)

    def test_only_a_missing_predecessor_is_read_as_no_predecessor(self) -> None:
        missing = self.root / "missing.json"
        self.assertIsNone(_read_state_json(missing, load_json_file=self._load))

        malformed = self.root / "malformed.json"
        malformed.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            _read_state_json(malformed, load_json_file=self._load)

        unreadable = self.root / "unreadable.json"
        unreadable.write_text("{}", encoding="utf-8")
        unreadable.chmod(0)
        try:
            with self.assertRaises(OSError):
                _read_state_json(unreadable, load_json_file=self._load)
        finally:
            unreadable.chmod(0o600)

    @staticmethod
    def _load(path: Path) -> object:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)


class AtomicStateWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._raw = tempfile.TemporaryDirectory(prefix=".atomic-state-")
        self.addCleanup(self._raw.cleanup)
        self.root = Path(self._raw.name)

    def _assert_interposed_publication_is_refused(self, writer: object) -> None:
        path = self.root / "state.json"
        attacker = b'{"attacker": true}\n'
        real_replace = os.replace
        interposed = False

        def replace_then_interpose(source: object, destination: object) -> None:
            nonlocal interposed
            real_replace(source, destination)
            if Path(destination) == path and not interposed:
                replacement = self.root / "interposed.json"
                replacement.write_bytes(attacker)
                replacement.chmod(0o600)
                real_replace(replacement, path)
                interposed = True

        with mock.patch(
            "sessionkit_inventory.state_io.os.replace",
            side_effect=replace_then_interpose,
        ):
            with self.assertRaisesRegex(
                CollectionError, "no longer names the file that was published"
            ):
                writer(path, {"expected": True})  # type: ignore[operator]
        self.assertTrue(interposed)
        self.assertEqual(attacker, path.read_bytes())

    def test_atomic_json_refuses_an_interposed_published_path(self) -> None:
        self._assert_interposed_publication_is_refused(atomic_write_json)

    def test_atomic_private_json_refuses_an_interposed_published_path(self) -> None:
        self._assert_interposed_publication_is_refused(atomic_write_private_json)


if __name__ == "__main__":
    unittest.main()
