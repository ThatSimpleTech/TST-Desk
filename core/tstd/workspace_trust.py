"""Trust prompt for repo-supplied workspace config (TD-5113).

A workspace's ``.tst/config.yaml`` usually arrives with a clone. Applied on
open, it let a repo grant itself shell ``auto`` rules, a network allowlist
or raised caps before anyone had read it. Now the daemon reads the file
once, hashes those exact bytes, and lists what it loosens against the
packaged defaults:

- a file that loosens nothing applies as written, with nothing to ask;
- a file that loosens anything applies only once the user has trusted
  that exact hash. Until then, or after a decline, the workspace runs on
  the packaged defaults: the whole file is set aside, not just the
  loosened keys.

Decisions live in the user's data dir (``workspace_trust.yaml``), never in
the workspace, so a repo cannot ship its own trust. Each workspace keeps
one record, the hash the user decided on and the decision. A file whose
hash differs asks again. TST Desk's own writes to the file (always-allow,
spend cap, import approvals) go through :meth:`TrustStore.before_write`
and :meth:`TrustStore.after_write`, so the user's own changes keep the
trust they were made under and are refused while the file is untrusted.
A write keeps that trust only for settings the user has seen or just set;
one that exposes anything else leaves the file pending.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from .boundary_config import BoundaryConfig, boundary_from_mapping, workspace_config_path
from .config import ConfigError
from .host_memory import default_shell_memory_mb
from .policy import approved_imports_from_mapping, policy_from_mapping

TRUST_FILE = "workspace_trust.yaml"

# ``none``: no file, a comment-only file, or one that loosens nothing.
# It applies as written and there is nothing to decide.
TrustState = Literal["none", "pending", "trusted", "declined"]
Decision = Literal["trusted", "declined"]

# Top-level keys TST Desk does not read from a workspace yet. A repo that
# sets them is asking for something only user config may grant today, so
# they count as loosening now and a later reader (hooks: TD-5134) inherits
# the gate instead of having to remember it.
_UNREAD_LOOSENING_KEYS: tuple[str, ...] = ("mcp_servers", "hooks")

_DRIVE = re.compile(r"^[A-Za-z]:")


class UntrustedConfigError(ConfigError):
    """TST Desk refused to write into a config the user has not trusted."""


@dataclass(frozen=True)
class Loosened:
    """One setting the file loosens, as the trust card shows it."""

    key: str
    value: str
    default: str


@dataclass(frozen=True)
class WorkspaceConfigView:
    """One read of ``.tst/config.yaml``: the bytes' hash, what they loosen,
    and whether they apply.

    ``data`` is the parsed mapping, ``None`` when the file is absent or
    comment-only. Callers parse :attr:`effective`, never ``data``, so an
    untrusted file can only ever yield the packaged defaults.
    """

    path: Path
    data: dict[str, Any] | None
    sha256: str | None
    loosened: tuple[Loosened, ...]
    state: TrustState

    @property
    def applies(self) -> bool:
        """True when the file's settings are in force."""
        return self.state in ("none", "trusted")

    @property
    def effective(self) -> dict[str, Any] | None:
        """The mapping to load settings from: ``None`` (defaults) until trusted."""
        return self.data if self.applies else None


# ── What loosens ───────────────────────────────────────────────────────


def _escapes_workspace(pattern: str) -> bool:
    """A ``writable_paths`` entry that names somewhere outside the workspace.

    The path guard refuses such writes whatever the file says, so these can
    never widen the wall; they are still shown, because a repo asking for
    them is asking for more than the default.
    """
    if pattern.startswith(("/", "\\", "~")) or _DRIVE.match(pattern):
        return True
    return ".." in re.split(r"[\\/]", pattern)


def _summary(value: Any) -> str:
    """The whole value, never cut short (TD-5158).

    Trust applies the whole file, so a card that hid the tail of a hook
    command would ask the user to trust text they never saw. The card
    scrolls; a long value costs space, not safety.
    """
    return yaml.safe_dump(value, default_flow_style=True, sort_keys=True).strip()


def _raised(key: str, value: float, default: float) -> list[Loosened]:
    if value > default:
        return [Loosened(key, f"{value:g}", f"{default:g}")]
    return []


def _boundary_loosened(path: Path, data: dict[str, Any]) -> list[Loosened]:
    try:
        cfg = boundary_from_mapping(path, data)
    except ConfigError:
        return []  # an invalid section applies nothing: the loader falls back to defaults
    base = BoundaryConfig()
    out: list[Loosened] = []
    network = cfg.boundary.network
    if isinstance(network, list) and network:
        out.append(Loosened("boundary.network", ", ".join(network), "deny"))
    for pattern in cfg.boundary.writable_paths:
        if _escapes_workspace(pattern):
            out.append(
                Loosened("boundary.writable_paths", pattern, "** (inside the workspace only)")
            )
    for name in ("spend_usd", "wall_clock_hours", "max_iterations"):
        out += _raised(f"caps.{name}", getattr(cfg.caps, name), getattr(base.caps, name))
    out += _shell_memory_loosened(cfg.caps.shell_memory_mb)
    for name in ("max_file_bytes", "max_total_bytes", "max_count"):
        out += _raised(
            f"attachments.{name}", getattr(cfg.attachments, name), getattr(base.attachments, name)
        )
    out += _raised("memory.max_lines", cfg.memory.max_lines, base.memory.max_lines)
    # A done-check (TD-5124) runs on its own at every turn end: a command
    # through the shell tool, or a statement sent to the model as a user
    # message when it fails. Like ``hooks``, a repo's own counts as soon
    # as it names one.
    for check in cfg.stop_check.checks:
        out.append(Loosened("stop_check.checks", check, "none"))
    return out


def _shell_memory_loosened(configured: int | None) -> list[Loosened]:
    """``caps.shell_memory_mb`` above half of this machine's RAM (TD-5116).

    The default is worked out here, not stored, so the same file can loosen
    on a small laptop and tighten on a big box. With RAM unknown there is
    no default cap, and any value narrows.
    """
    default = default_shell_memory_mb()
    if configured is None or default is None or configured <= default:
        return []
    return [Loosened("caps.shell_memory_mb", str(configured), f"{default} (half of RAM)")]


def _policy_loosened(path: Path, data: dict[str, Any]) -> list[Loosened]:
    try:
        policy = policy_from_mapping(path, data)
    except ConfigError:
        return []
    return [
        Loosened("policy.rules", f"{rule.tool}: {rule.args} → auto", "ask (class B and C)")
        for rule in policy.rules
        if rule.effect == "auto"
    ]


def _imports_loosened(path: Path, data: dict[str, Any]) -> list[Loosened]:
    try:
        imports = approved_imports_from_mapping(path, data)
    except ConfigError:
        return []
    return [
        Loosened("approved_external_imports", str(p), "none (each import asks)")
        for p in sorted(imports)
    ]


def find_loosened(path: Path, data: dict[str, Any]) -> list[Loosened]:
    """Every setting in *data* that is looser than the packaged defaults.

    Judged on the same validated models the loaders apply, so a section
    the loader would refuse (and replace with defaults) loosens nothing.
    Tightening (``allowed_commands``, ``never`` rules, lower caps) never
    counts.
    """
    out = _policy_loosened(path, data)
    out += _boundary_loosened(path, data)
    out += _imports_loosened(path, data)
    for key in _UNREAD_LOOSENING_KEYS:
        value = data.get(key)
        if value not in (None, [], {}, ""):
            out.append(Loosened(key, _summary(value), "none"))
    return out


# ── Reading the file ───────────────────────────────────────────────────


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigError(f"Failed to read {path}: {e}") from e


def _parse(path: Path, raw: bytes) -> dict[str, Any] | None:
    try:
        data: Any = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def read_view(workspace: str | Path, store: TrustStore | None) -> WorkspaceConfigView:
    """Read ``.tst/config.yaml`` once and judge it against *store*.

    The hash, the loosened list and the settings all come from the same
    bytes, so what the user trusted is exactly what applies. ``store`` is
    ``None`` only off the daemon (library use and tests): nothing is
    gated there and a loosening file reads as trusted.

    Raises:
        ConfigError: If the file cannot be read, is not valid YAML, or is
            not a mapping.
    """
    path = workspace_config_path(workspace)
    raw = _read_bytes(path)
    if raw is None:
        return WorkspaceConfigView(path, None, None, (), "none")
    digest = hashlib.sha256(raw).hexdigest()
    data = _parse(path, raw)
    loosened = tuple(find_loosened(path, data)) if data is not None else ()
    state: TrustState = "none"
    if loosened:
        state = "trusted" if store is None else store.state_for(workspace, digest)
    return WorkspaceConfigView(path, data, digest, loosened, state)


def trusted_imports(workspace: str | Path, store: TrustStore | None) -> frozenset[Path]:
    """Durable external-import approvals, empty while the config is untrusted.

    Raises:
        ConfigError: The config cannot be read or the key is malformed.
    """
    view = read_view(workspace, store)
    return approved_imports_from_mapping(view.path, view.effective)


# ── The decision store ─────────────────────────────────────────────────


def _key(workspace: str | Path) -> str:
    try:
        return str(Path(workspace).expanduser().resolve())
    except OSError:
        return str(Path(workspace).expanduser().absolute())


def same_workspace(a: str | Path, b: str | Path) -> bool:
    """True when *a* and *b* name the same workspace, as the store keys it."""
    return _key(a) == _key(b)


class TrustStore:
    """Per-workspace trust decisions, in the user's data dir.

    An unreadable or garbled file reads as empty: every loosening config
    asks again, which fails closed.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / TRUST_FILE
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data: Any = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return {}
        workspaces = data.get("workspaces") if isinstance(data, dict) else None
        if not isinstance(workspaces, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for key, record in workspaces.items():
            if not isinstance(record, dict):
                continue
            sha, decision = record.get("sha256"), record.get("decision")
            if isinstance(sha, str) and decision in ("trusted", "declined"):
                out[str(key)] = {"sha256": sha, "decision": decision}
        return out

    def state_for(self, workspace: str | Path, sha256: str) -> TrustState:
        """``trusted`` / ``declined`` when the stored hash matches, else ``pending``."""
        with self._lock:
            record = self._load().get(_key(workspace))
        if record is None or record["sha256"] != sha256:
            return "pending"
        return "trusted" if record["decision"] == "trusted" else "declined"

    def record(self, workspace: str | Path, sha256: str, decision: Decision) -> None:
        """Store *decision* for *workspace*'s config with hash *sha256*."""
        with self._lock:
            records = self._load()
            records[_key(workspace)] = {"sha256": sha256, "decision": decision}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            body = {"workspaces": dict(sorted(records.items()))}
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".trust.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("# TST Desk workspace config trust (TD-5113). Not a workspace file.\n")
                    yaml.safe_dump(body, f, sort_keys=False)
                os.replace(tmp, self.path)
            except BaseException:
                os.unlink(tmp)
                raise

    def view(self, workspace: str | Path) -> WorkspaceConfigView:
        """:func:`read_view` against this store."""
        return read_view(workspace, self)

    # ── TST Desk's own writes ──────────────────────────────────────────

    def before_write(self, workspace: str | Path) -> WorkspaceConfigView:
        """Refuse a write into a config whose settings are not in force.

        Merging the user's rule into a file they have not trusted would
        either bury it under settings that do not apply or, once stamped,
        trust the repo's settings along with it.

        Returns:
            The read the write starts from, for :meth:`after_write`.

        Raises:
            UntrustedConfigError: The file is pending or declined.
            ConfigError: The file cannot be read or parsed.
        """
        view = self.view(workspace)
        if not view.applies:
            raise UntrustedConfigError(
                f"{view.path} loosens settings you have not trusted, so TST Desk "
                "will not save into it. Trust the workspace config first, or "
                "approve this once."
            )
        return view

    def after_write(
        self, workspace: str | Path, before: WorkspaceConfigView, owns: tuple[str, ...]
    ) -> WorkspaceConfigView:
        """Keep the trust a write was made under, for what the user has seen.

        The new hash is stamped trusted only when every setting it loosens
        was already in force before the write (*before*), or sits under a
        key this write sets (*owns*, e.g. ``caps.spend_usd``). Anything else
        came from a section the loader refused before the write, which the
        write made valid (a budget saved over a bad ``caps`` value exposes
        the ``boundary.network`` beside it). The card never listed it, so
        the new hash stays pending and the card asks.

        Returns:
            The read after the write: in force (``applies``) or pending.
        """
        after = self.view(workspace)
        if after.sha256 is None or after.state != "pending":
            return after
        seen = set(before.loosened) if before.applies else set()
        unseen = [item for item in after.loosened if item not in seen and item.key not in owns]
        if unseen:
            return after
        self.record(workspace, after.sha256, "trusted")
        return replace(after, state="trusted")

    def write_through(
        self, workspace: str | Path, write: Callable[[], None], owns: tuple[str, ...]
    ) -> WorkspaceConfigView:
        """:meth:`before_write`, *write*, :meth:`after_write`."""
        before = self.before_write(workspace)
        write()
        return self.after_write(workspace, before, owns)

    def writable(self, workspace: str | Path) -> bool:
        """True when :meth:`before_write` would accept a write now."""
        try:
            self.before_write(workspace)
        except ConfigError:
            return False
        return True
