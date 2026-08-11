"""Vendored Maniple oversight primitives, adapted for Session Kit.

Source: github.com/Martian-Engineering/maniple
Commit: 0987ccf59552989600f6134e6602abe72a3214d0
License: MIT, per the source project's pyproject.toml.

The upstream copyright and MIT permission notice ship beside this package in
``LICENSE``; ``NOTICE`` records the exact upstream file mapping and the
Session Kit modifications. Both files must travel with any copy of this
directory.
"""

from .registry import SessionRegistry, Worker

__all__ = ["SessionRegistry", "Worker"]
