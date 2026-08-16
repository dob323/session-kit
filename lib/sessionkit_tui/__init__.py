"""The picker's screen layer.

A pure core and a thin curses shell. `model` reads the session snapshot the
rest of the kit already produces, `rows` projects it into what a person sees,
`frame` turns that into painted lines, `state` answers keys and clicks, and
`actions` names the `sp` command every choice runs. Only `app` touches curses,
and only `runner` starts a process, so every rule this picker follows is
testable without a terminal.
"""

__all__ = [
    "actions",
    "frame",
    "model",
    "rows",
    "state",
    "voice",
]
