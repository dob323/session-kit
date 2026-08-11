"""Install Session Kit's provider prompt hooks without owning user config.

The lifecycle transaction captures both targets before this module runs. This
module owns the smaller contract inside them: one provenance-marked handler per
provider, merged beside every unrelated handler and setting, atomically
published, and reread byte-for-byte before activation may continue.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from typing import Any, Mapping


MAX_CONFIG_BYTES = 1024 * 1024
EVENT = "UserPromptSubmit"
CLAUDE_COMMAND = (
    '"$HOME/.local/lib/session-kit/current/extras/hooks/sk_session_events.py"'
)
CODEX_COMMAND = (
    'python3 "$HOME/.local/lib/session-kit/current/lib/sessionkit_supervisor/'
    'provider_hooks.py" codex-hook'
)
DESCRIPTION = "Session Kit automatic project intake."
PROVENANCE_KEY = "sessionKitProvenance"
PROVENANCE_OWNER = "session-kit"
PROVENANCE_SCHEMA = 1
ACCEPTANCE_SCHEMA = 1
SHA256_RE = re.compile(r"[0-9a-f]{64}")
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


class HookConfigError(ValueError):
    """A provider config cannot be changed without risking user settings."""


@dataclass
class _Node:
    value: Any
    start: int
    end: int
    close: int | None = None
    items: list["_Node"] = field(default_factory=list)
    members: list[tuple[str, "_Node"]] = field(default_factory=list)


class _Spans:
    """Small JSON span parser used to preserve every user byte around our entry."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.decoder = json.JSONDecoder()

    def _space(self, offset: int) -> int:
        while offset < len(self.text) and self.text[offset].isspace():
            offset += 1
        return offset

    def parse(self) -> _Node:
        node, offset = self._value(self._space(0))
        if self._space(offset) != len(self.text):
            raise HookConfigError("provider hook config has trailing JSON content")
        return node

    def _value(self, offset: int) -> tuple[_Node, int]:
        start = offset
        if offset >= len(self.text):
            raise HookConfigError("provider hook config is incomplete")
        if self.text[offset] == "{":
            offset = self._space(offset + 1)
            members: list[tuple[str, _Node]] = []
            value: dict[str, Any] = {}
            if offset < len(self.text) and self.text[offset] != "}":
                while True:
                    try:
                        key, offset = self.decoder.raw_decode(self.text, offset)
                    except json.JSONDecodeError as exc:
                        raise HookConfigError("provider hook object key is invalid") from exc
                    if not isinstance(key, str):
                        raise HookConfigError("provider hook object key must be a string")
                    if key in value:
                        raise HookConfigError("provider hook JSON contains a duplicate key")
                    offset = self._space(offset)
                    if offset >= len(self.text) or self.text[offset] != ":":
                        raise HookConfigError("provider hook object member is incomplete")
                    child, offset = self._value(self._space(offset + 1))
                    members.append((key, child))
                    value[key] = child.value
                    offset = self._space(offset)
                    if offset < len(self.text) and self.text[offset] == ",":
                        offset = self._space(offset + 1)
                        continue
                    break
            if offset >= len(self.text) or self.text[offset] != "}":
                raise HookConfigError("provider hook object is incomplete")
            return _Node(value, start, offset + 1, close=offset, members=members), offset + 1
        if self.text[offset] == "[":
            offset = self._space(offset + 1)
            items: list[_Node] = []
            if offset < len(self.text) and self.text[offset] != "]":
                while True:
                    child, offset = self._value(offset)
                    items.append(child)
                    offset = self._space(offset)
                    if offset < len(self.text) and self.text[offset] == ",":
                        offset = self._space(offset + 1)
                        continue
                    break
            if offset >= len(self.text) or self.text[offset] != "]":
                raise HookConfigError("provider hook list is incomplete")
            return _Node([item.value for item in items], start, offset + 1, close=offset, items=items), offset + 1
        try:
            value, end = self.decoder.raw_decode(self.text, offset)
        except json.JSONDecodeError as exc:
            raise HookConfigError("provider hook JSON value is invalid") from exc
        return _Node(value, start, end), end


def _member(node: _Node, key: str) -> _Node | None:
    for name, child in node.members:
        if name == key:
            return child
    return None


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _append_member(text: str, object_node: _Node, key: str, value: object) -> str:
    assert object_node.close is not None
    addition = _compact(key) + ":" + _compact(value)
    if object_node.members:
        position = object_node.members[-1][1].end
        addition = "," + addition
    else:
        position = object_node.close
    return text[:position] + addition + text[position:]


def _append_item(text: str, array_node: _Node, value: object) -> str:
    assert array_node.close is not None
    addition = _compact(value)
    if array_node.items:
        position = array_node.items[-1].end
        addition = "," + addition
    else:
        position = array_node.close
    return text[:position] + addition + text[position:]


def _remove_item(text: str, array_node: _Node, index: int) -> str:
    item = array_node.items[index]
    if index:
        start = array_node.items[index - 1].end
    elif len(array_node.items) > 1:
        start = item.start
        return text[:start] + text[array_node.items[1].start :]
    else:
        start = item.start
    return text[:start] + text[item.end :]


def _remove_member(text: str, object_node: _Node, key: str) -> str:
    index = next(i for i, (name, _) in enumerate(object_node.members) if name == key)
    child = object_node.members[index][1]
    # Object member keys begin after the preceding value (or at the object's
    # first non-space byte). Added members are always last, so this exact
    # reversal preserves all whitespace that existed before installation.
    if index:
        start = object_node.members[index - 1][1].end
    else:
        start = object_node.start + 1
        while start < child.start and text[start].isspace():
            start += 1
        if len(object_node.members) > 1:
            next_child = object_node.members[1][1]
            key_start = text.rfind('"', start, child.start)
            del key_start  # first-member removal is not used for owned insertions
            return text[:start] + text[next_child.start :]
    return text[:start] + text[child.end :]


def _tree(text: str) -> _Node:
    return _Spans(text).parse()


def _handler(command: str, restore: Mapping[str, str]) -> dict[str, Any]:
    return {
        "command": command,
        PROVENANCE_KEY: {
            "owner": PROVENANCE_OWNER,
            "restore": dict(restore),
            "schemaVersion": PROVENANCE_SCHEMA,
        },
        "timeout": 3,
        "type": "command",
    }


def _group(handler: Mapping[str, Any]) -> dict[str, Any]:
    return {"hooks": [dict(handler)]}


def _owned_handler(value: object, command: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    provenance = value.get(PROVENANCE_KEY)
    return bool(
        value.get("command") == command
        and isinstance(provenance, Mapping)
        and provenance.get("owner") == PROVENANCE_OWNER
        and provenance.get("schemaVersion") == PROVENANCE_SCHEMA
        and isinstance(provenance.get("restore"), Mapping)
    )


def _restore_state(value: Mapping[str, Any]) -> dict[str, str]:
    provenance = value[PROVENANCE_KEY]
    assert isinstance(provenance, Mapping)
    restore = provenance["restore"]
    assert isinstance(restore, Mapping)
    expected = {"description", "event", "file", "group", "hooks"}
    if (
        set(restore) != expected
        or restore.get("description") not in {"added", "preserved"}
        or restore.get("event") not in {"absent", "present"}
        or restore.get("file") not in {"absent", "present"}
        or restore.get("group") not in {"created", "existing"}
        or restore.get("hooks") not in {"absent", "present"}
    ):
        raise HookConfigError("provider hook provenance is invalid")
    return {key: str(restore[key]) for key in expected}


def _open_parent(path: Path, *, create: bool) -> int:
    """Open every ancestor without following links and return the parent fd."""
    if not path.is_absolute() or ".." in path.parts:
        raise HookConfigError(f"provider hook path is unsafe: {path}")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(path.parent.parts[1:]):
            final = index == len(path.parent.parts[1:]) - 1
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            info = os.fstat(child)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.geteuid()}
                or (info.st_mode & 0o022) != 0
            ):
                os.close(child)
                raise HookConfigError(f"provider hook directory is unsafe: {path.parent}")
            if final and info.st_uid != os.geteuid():
                os.close(child)
                raise HookConfigError(f"provider hook directory is not user-owned: {path.parent}")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(path: Path) -> tuple[dict[str, Any], bytes | None]:
    try:
        parent = _open_parent(path, create=False)
    except FileNotFoundError:
        return {}, None
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
    except FileNotFoundError:
        os.close(parent)
        return {}, None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_size > MAX_CONFIG_BYTES
        ):
            raise HookConfigError(f"provider hook config is unsafe: {path}")
        raw = os.read(descriptor, info.st_size + 1)
        text = raw.decode("utf-8")
        value = _tree(text).value
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HookConfigError(f"provider hook config is incomplete or invalid: {path}") from exc
    finally:
        os.close(descriptor)
        os.close(parent)
    if not isinstance(value, dict):
        raise HookConfigError(f"provider hook config must contain a JSON object: {path}")
    return value, raw


def _event_groups(document: dict[str, Any], *, create: bool) -> list[Any] | None:
    hooks = document.get("hooks")
    if hooks is None:
        if not create:
            return None
        hooks = {}
        document["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise HookConfigError("provider hook config's hooks field must be an object")
    groups = hooks.get(EVENT)
    if groups is None:
        if not create:
            return None
        groups = []
        hooks[EVENT] = groups
    if not isinstance(groups, list):
        raise HookConfigError(f"provider hook config's {EVENT} field must be a list")
    return groups


def _compatible_group(groups: list[Any]) -> tuple[dict[str, Any] | None, bool]:
    """Return a broad existing matcher group, or signal that one must be added."""
    for value in groups:
        if not isinstance(value, dict):
            continue
        if value.get("matcher") not in {None, ""}:
            continue
        hooks = value.get("hooks")
        if isinstance(hooks, list):
            return value, False
    return None, True


def _encoded(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_verified_write(path: Path, payload: bytes) -> None:
    parent = _open_parent(path, create=True)
    temporary = f".{path.name}.{secrets.token_hex(12)}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        verify = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        try:
            info = os.fstat(verify)
            observed = os.read(verify, len(payload) + 1)
        finally:
            os.close(verify)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or observed != payload
        ):
            raise HookConfigError(f"provider hook config failed post-write verification: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _codex_acceptance(payload: Mapping[str, Any]) -> None:
    """Mark one staged stdin prompt accepted from inside UserPromptSubmit."""
    marker_raw = os.environ.get("SESSION_KIT_PROMPT_HANDOFF_ACCEPTANCE")
    handoff_raw = os.environ.get("SESSION_KIT_PROMPT_HANDOFF")
    handoff_root_raw = os.environ.get("SESSION_KIT_START_DIR")
    expected_digest = os.environ.get("SESSION_KIT_PROMPT_HANDOFF_SHA256")
    expected_bytes = os.environ.get("SESSION_KIT_PROMPT_HANDOFF_BYTES")
    if (
        not marker_raw
        or not handoff_raw
        or not handoff_root_raw
        or not expected_digest
        or not expected_bytes
    ):
        return
    if not SHA256_RE.fullmatch(expected_digest):
        return
    try:
        byte_count = int(expected_bytes)
    except ValueError:
        return
    event = payload.get("hook_event_name") or payload.get("event")
    if event != EVENT:
        return
    prompt = payload.get("prompt")
    session_id = payload.get("session_id") or payload.get("sessionId")
    turn_id = payload.get("turn_id") or payload.get("turnId")
    if (
        not isinstance(prompt, str)
        or not isinstance(session_id, str)
        or UUID_RE.fullmatch(session_id) is None
        or not isinstance(turn_id, str)
        or UUID_RE.fullmatch(turn_id) is None
    ):
        return
    prompt_bytes = prompt.encode("utf-8")
    if len(prompt_bytes) != byte_count:
        return
    if hashlib.sha256(prompt_bytes).hexdigest() != expected_digest:
        return
    marker = Path(marker_raw)
    handoff = Path(handoff_raw)
    handoff_root = Path(handoff_root_raw)
    if (
        not marker.is_absolute()
        or not handoff.is_absolute()
        or not handoff_root.is_absolute()
        or ".." in marker.parts
        or ".." in handoff.parts
        or ".." in handoff_root.parts
        or handoff.parent != handoff_root
        or handoff.suffix != ".prompt"
        or marker != Path(f"{handoff}.accepted")
    ):
        return
    try:
        parent = _open_parent(handoff, create=False)
        handoff_fd = os.open(
            handoff.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        handoff_info = os.fstat(handoff_fd)
        root_info = os.fstat(parent)
        os.close(handoff_fd)
        os.close(parent)
    except (OSError, HookConfigError):
        return
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != os.geteuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
        or not stat.S_ISREG(handoff_info.st_mode)
        or stat.S_ISLNK(handoff_info.st_mode)
        or handoff_info.st_uid != os.geteuid()
        or handoff_info.st_nlink != 1
        or stat.S_IMODE(handoff_info.st_mode) != 0o600
        or handoff_info.st_size != byte_count
    ):
        return
    record = {
        "bytes": byte_count,
        "schema_version": ACCEPTANCE_SCHEMA,
        "session_id": session_id,
        "sha256": expected_digest,
        "status": "accepted",
        "turn_id": turn_id,
    }
    encoded = (_compact(record) + "\n").encode("utf-8")
    try:
        existing = marker.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if (
            stat.S_ISREG(existing.st_mode)
            and not stat.S_ISLNK(existing.st_mode)
            and existing.st_uid == os.geteuid()
            and stat.S_IMODE(existing.st_mode) == 0o600
            and marker.read_bytes() == encoded
        ):
            return
        raise HookConfigError("conflicting prompt acceptance record")
    _atomic_verified_write(marker, encoded)


def codex_hook() -> int:
    """Accept the handoff first, then pass the exact payload to intake."""
    try:
        raw = sys.stdin.buffer.read(MAX_CONFIG_BYTES + 1)
        if not raw or len(raw) > MAX_CONFIG_BYTES:
            return 0
        decoded = _tree(raw.decode("utf-8")).value
        if not isinstance(decoded, Mapping):
            return 0
        try:
            _codex_acceptance(decoded)
        except BaseException:
            pass
        if (
            os.environ.get("SESSION_KIT_TESTING") == "1"
            and os.environ.get("SESSION_KIT_TEST_FAILPOINT") == "codex-intake-after-acceptance"
        ):
            return 0
        intake_hook = Path(__file__).resolve().parents[2] / "extras/hooks/sk_codex_intake.py"
        environment = os.environ.copy()
        acceptance_path = environment.get("SESSION_KIT_PROMPT_HANDOFF_ACCEPTANCE")
        handoff_path = environment.get("SESSION_KIT_PROMPT_HANDOFF")
        if acceptance_path and handoff_path:
            environment["SESSION_KIT_SOURCE_ACCEPTANCE_PATH"] = f"{handoff_path}.source_accepted"
            environment["SESSION_KIT_INTAKE_COMMIT_PATH"] = f"{handoff_path}.intake_committed"
        subprocess.run(
            [sys.executable, os.fspath(intake_hook)],
            input=raw,
            check=False,
            env=environment,
            timeout=3,
        )
    except BaseException:
        # Provider prompt hooks are advisory. A missing acceptance marker is
        # surfaced by the launcher without ever blocking the submitted turn.
        pass
    return 0


def configure(path: Path, *, command: str, enabled: bool) -> dict[str, Any]:
    """Add or remove one owned handler without claiming its matcher group."""
    document, before = _read(path)
    file_state = "absent" if before is None else "present"
    hooks_state = "present" if "hooks" in document else "absent"
    hooks_before = document.get("hooks")
    event_state = (
        "present"
        if isinstance(hooks_before, Mapping) and EVENT in hooks_before
        else "absent"
    )

    groups = _event_groups(document, create=False)
    owned: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    if groups is not None:
        for group in groups:
            if not isinstance(group, dict):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if _owned_handler(handler, command):
                    assert isinstance(handler, Mapping)
                    owned.append((group, handler))

    if enabled and len(owned) > 1:
        raise HookConfigError("provider hook config contains multiple owned handlers")

    if enabled and not owned:
        group, created = _compatible_group(groups or [])
        restore = {
            "description": "preserved",
            "event": event_state,
            "file": file_state,
            "group": "created" if created else "existing",
            "hooks": hooks_state,
        }
        add_description = command == CODEX_COMMAND and "description" not in document
        if add_description:
            restore["description"] = "added"
        handler = _handler(command, restore)
        if before is None:
            fresh: dict[str, Any] = {}
            if add_description:
                fresh["description"] = DESCRIPTION
            fresh["hooks"] = {EVENT: [_group(handler)]}
            after = _encoded(fresh)
        else:
            text = before.decode("utf-8")
            root = _tree(text)
            if add_description:
                text = _append_member(text, root, "description", DESCRIPTION)
                root = _tree(text)
            hooks_node = _member(root, "hooks")
            if hooks_node is None:
                text = _append_member(text, root, "hooks", {})
                root = _tree(text)
                hooks_node = _member(root, "hooks")
            assert hooks_node is not None
            event_node = _member(hooks_node, EVENT)
            if event_node is None:
                text = _append_member(text, hooks_node, EVENT, [])
                root = _tree(text)
                hooks_node = _member(root, "hooks")
                assert hooks_node is not None
                event_node = _member(hooks_node, EVENT)
            assert event_node is not None
            current_groups = event_node.value
            assert isinstance(current_groups, list)
            compatible, _ = _compatible_group(current_groups)
            if compatible is None:
                text = _append_item(text, event_node, _group(handler))
            else:
                group_index = next(
                    index for index, value in enumerate(current_groups)
                    if value is compatible
                )
                group_node = event_node.items[group_index]
                handlers_node = _member(group_node, "hooks")
                assert handlers_node is not None
                text = _append_item(text, handlers_node, handler)
            after = text.encode("utf-8")
        _atomic_verified_write(path, after)
        verified, _ = _read(path)
        verified_groups = _event_groups(verified, create=False) or []
        visible = sum(
            1
            for group_value in verified_groups
            if isinstance(group_value, Mapping)
            for handler_value in group_value.get("hooks", [])
            if _owned_handler(handler_value, command)
        )
        if visible != 1:
            raise HookConfigError("provider hook activation did not leave exactly one owned handler")
        return {"changed": True, "path": os.fspath(path)}
    if not enabled and owned:
        restore_states = [_restore_state(handler) for _, handler in owned]
        if any(state != restore_states[0] for state in restore_states[1:]):
            raise HookConfigError("provider hook provenance records disagree")
        restore = restore_states[0]
        assert before is not None
        text = before.decode("utf-8")
        while True:
            root = _tree(text)
            hooks_node = _member(root, "hooks")
            event_node = _member(hooks_node, EVENT) if hooks_node else None
            located: tuple[_Node, int, _Node, int] | None = None
            if event_node is not None:
                for group_index, group_node in enumerate(event_node.items):
                    handlers_node = _member(group_node, "hooks")
                    if handlers_node is None:
                        continue
                    for handler_index, handler_node in enumerate(handlers_node.items):
                        if _owned_handler(handler_node.value, command):
                            located = (
                                handlers_node,
                                handler_index,
                                event_node,
                                group_index,
                            )
            if located is None:
                break
            handlers_node, handler_index, event_node, group_index = located
            if restore["group"] == "created" and len(handlers_node.items) == 1:
                text = _remove_item(text, event_node, group_index)
            else:
                text = _remove_item(text, handlers_node, handler_index)

        root = _tree(text)
        hooks_node = _member(root, "hooks")
        event_node = _member(hooks_node, EVENT) if hooks_node else None
        if (
            restore["event"] == "absent"
            and hooks_node is not None
            and event_node is not None
            and event_node.value == []
        ):
            text = _remove_member(text, hooks_node, EVENT)
            root = _tree(text)
            hooks_node = _member(root, "hooks")
        if restore["hooks"] == "absent" and hooks_node is not None and hooks_node.value == {}:
            text = _remove_member(text, root, "hooks")
            root = _tree(text)
        description_node = _member(root, "description")
        if (
            restore["description"] == "added"
            and description_node is not None
            and description_node.value == DESCRIPTION
        ):
            text = _remove_member(text, root, "description")
            root = _tree(text)
        if restore["file"] == "absent" and root.value == {}:
            path.unlink()
            if path.exists() or path.is_symlink():
                raise HookConfigError(f"provider hook config could not be removed: {path}")
            return {"changed": True, "path": os.fspath(path)}
        after = text.encode("utf-8")
        _atomic_verified_write(path, after)
        return {"changed": True, "path": os.fspath(path)}

    if before is None:
        return {"changed": False, "path": os.fspath(path)}
    if enabled and len(owned) != 1:
        raise HookConfigError("provider hook activation is not uniquely visible")
    if not path.is_file():
        if path.exists() or path.is_symlink():
            raise HookConfigError(f"provider hook config could not be removed: {path}")
    return {"changed": False, "path": os.fspath(path)}


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if values == ["codex-hook"]:
        return codex_hook()
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("enable", "disable"))
    parser.add_argument("--claude-settings", required=True, type=Path)
    parser.add_argument("--codex-hooks", required=True, type=Path)
    args = parser.parse_args(values)
    enabled = args.action == "enable"
    configure(args.claude_settings, command=CLAUDE_COMMAND, enabled=enabled)
    if (
        os.environ.get("SESSION_KIT_TESTING") == "1"
        and os.environ.get("SESSION_KIT_TEST_FAILPOINT") == "provider-hooks-claude"
    ):
        raise HookConfigError("isolated test failpoint after Claude hook activation")
    configure(args.codex_hooks, command=CODEX_COMMAND, enabled=enabled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
