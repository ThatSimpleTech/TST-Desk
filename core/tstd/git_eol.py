"""git's end-of-line rules, redone without running git's conversion (TD-5157).

The daemon stages and restores file bytes itself so no filter driver runs
(:mod:`tstd.git_stage`, :mod:`tstd.git_restore`). End-of-line conversion
is not a driver: it is git's own code, driven by the ``text``, ``eol`` and
``crlf`` attributes and ``core.autocrlf`` / ``core.eol``. Doing it here
keeps checkpoints what ``git add`` made them (a CRLF file normalised to
LF where git would) and gives a restored file CRLF back where a checkout
would. The decision follows git's ``convert.c``: ``convert_attrs``,
``crlf_to_git``, ``crlf_to_worktree`` and their "safer autocrlf" checks.
``ident`` and ``working-tree-encoding`` are not applied.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .git_raw import checked_git, run_git

_LONE_LF = re.compile(rb"(?<!\r)\n")
_LONE_CR = re.compile(rb"\r(?!\n)")
# Control bytes git counts as non-printable (BS, HT, ESC and FF are printable;
# CR and LF are counted apart).
_NONPRINTABLE = bytes(b for b in [*range(32), 127] if b not in (8, 9, 10, 12, 13, 27))
_TRUTHY = frozenset({"true", "yes", "on", "1"})


@dataclass(frozen=True)
class EolConfig:
    """``core.autocrlf`` (``true``/``false``/``input``) and ``core.eol``."""

    autocrlf: str = "false"
    eol: str = ""
    native_crlf: bool = os.name == "nt"

    def text_eol_is_crlf(self) -> bool:
        if self.autocrlf == "true":
            return True
        if self.autocrlf == "input":
            return False
        if self.eol == "crlf":
            return True
        if self.eol == "lf":
            return False
        return self.native_crlf


def _crlf_action(value: str) -> str:
    """git's ``git_path_check_crlf`` for a ``text`` or ``crlf`` attribute value."""
    return {"set": "text", "unset": "binary", "input": "text-input", "auto": "auto"}.get(
        value, "undefined"
    )


def eol_action(attrs: Mapping[str, str], cfg: EolConfig) -> str:
    """git's ``convert_attrs`` decision: ``binary``, ``text-input``,
    ``text-crlf``, ``auto``, ``auto-input`` or ``auto-crlf``."""
    action = _crlf_action(attrs.get("text", "unspecified"))
    if action == "undefined":
        action = _crlf_action(attrs.get("crlf", "unspecified"))
    if action != "binary":
        eol = attrs.get("eol", "unspecified")
        if action == "auto" and eol in ("lf", "crlf"):
            action = "auto-input" if eol == "lf" else "auto-crlf"
        elif eol == "lf":
            action = "text-input"
        elif eol == "crlf":
            action = "text-crlf"
    if action == "text":
        action = "text-crlf" if cfg.text_eol_is_crlf() else "text-input"
    if action == "undefined":
        action = {"true": "auto-crlf", "input": "auto-input"}.get(cfg.autocrlf, "binary")
    return action


def is_binary(data: bytes) -> bool:
    """git's ``convert_is_binary`` over the counts ``gather_stats`` keeps."""
    if b"\0" in data or _LONE_CR.search(data):
        return True
    nonprintable = sum(data.count(bytes([b])) for b in _NONPRINTABLE)
    printable = len(data) - nonprintable - data.count(b"\r") - data.count(b"\n")
    if data.endswith(b"\x1a"):
        nonprintable -= 1  # a trailing EOF mark is not counted
    return (printable >> 7) < nonprintable


def has_text_crlf(blob: bytes) -> bool:
    """git's ``has_crlf_in_index`` test on the blob the index holds."""
    return b"\r\n" in blob and not is_binary(blob)


def checkin_eol(
    data: bytes, attrs: Mapping[str, str], cfg: EolConfig, *, index_has_crlf: bool
) -> bytes:
    """*data* as ``git add`` stores it: CRLF to LF where git's rules say so.

    *index_has_crlf* is :func:`has_text_crlf` of the blob the index holds
    for the path; an ``auto`` file whose stored copy has CRLF is left
    alone ("safer autocrlf").
    """
    action = eol_action(attrs, cfg)
    if action == "binary" or b"\r\n" not in data:
        return data
    if action.startswith("auto") and (index_has_crlf or is_binary(data)):
        return data
    return data.replace(b"\r\n", b"\n")


def checkout_eol(data: bytes, attrs: Mapping[str, str], cfg: EolConfig) -> bytes:
    """*data* as a checkout writes it: lone LF to CRLF where git's rules say so."""
    action = eol_action(attrs, cfg)
    crlf = action in ("text-crlf", "auto-crlf") or (action == "auto" and cfg.text_eol_is_crlf())
    if not data or not crlf or not _LONE_LF.search(data):
        return data
    if action.startswith("auto") and (b"\r" in data or is_binary(data)):
        return data  # "safer autocrlf": a file with any CR is left alone
    return _LONE_LF.sub(b"\r\n", data)


async def eol_config(top: Path, env: Mapping[str, str]) -> tuple[EolConfig, bool]:
    """The repo's :class:`EolConfig`, and whether ``core.symlinks`` is on."""
    rc, out, _err = await run_git(
        top, "config", "-z", "--get-regexp", r"^core\.(autocrlf|eol|symlinks)$", env=env
    )
    values: dict[str, str] = {}
    if rc == 0:
        for item in out.split(b"\0"):
            key, newline, value = item.decode("utf-8", errors="replace").partition("\n")
            if key:
                values[key] = value.strip().lower() if newline else "true"
    raw = values.get("core.autocrlf", "false")
    autocrlf = "input" if raw == "input" else ("true" if raw in _TRUTHY else "false")
    eol = values.get("core.eol", "")
    eol = "" if eol == "native" else eol
    symlinks = values.get("core.symlinks", "false" if os.name == "nt" else "true") in _TRUTHY
    return EolConfig(autocrlf=autocrlf, eol=eol), symlinks


async def path_attributes(
    top: Path, rels: Sequence[str], env: Mapping[str, str], *, cached: bool
) -> dict[str, dict[str, str]]:
    """``text``, ``eol``, ``crlf`` and ``filter`` for each path.

    ``cached`` reads ``.gitattributes`` from the index only (a checkout of
    the commit in it); otherwise the working tree comes first, as for
    ``git add``. ``check-attr`` runs no command.
    """
    if not rels:
        return {}
    out = await checked_git(
        top,
        "check-attr",
        *(["--cached"] if cached else []),
        "-z",
        "--stdin",
        "text",
        "eol",
        "crlf",
        "filter",
        env=env,
        stdin=b"".join(os.fsencode(r) + b"\0" for r in rels),
    )
    fields = out.split(b"\0")
    attrs: dict[str, dict[str, str]] = {}
    for i in range(0, len(fields) - 2, 3):
        path, name, value = (os.fsdecode(f) for f in fields[i : i + 3])
        attrs.setdefault(path, {})[name] = value
    return attrs
