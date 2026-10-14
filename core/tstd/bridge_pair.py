"""Adding and forgetting a paired computer: keychain and file (TD-5015).

Add order: ``pair_ok`` on the other computer, then the keychain write,
then ``computers.yaml``. When the keychain or the file fails, the hub
uses the key it holds in memory, once, to remove itself on the other
computer (no orphan device there), and deletes whatever it wrote here.

Re-pairing a computer this hub already knows (same ``machine_id``, or the
same address for one that sent none) keeps its ``mch``, so every bridged
session id and session window survives. At the same address the pair
frame carries ``replaces`` and the other computer retires the old entry
itself; at a new address the old entry is removed with the old key.

Keys are read from the keychain once per daemon run and kept in memory.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine
from typing import Any

from . import keychain
from .keychain import KeychainError, KeychainLockedError
from .logging import get_logger
from .machine_pairing import DialPolicy, PairError, PairOutcome, PairTarget, redeem, remove_self
from .machines import MAX_MACHINES, MachineLimitError, MachineRecord, MachineRegistry
from .protocol import build_error
from .remote_devices import desktop_name, iso_utc

log = get_logger("tstd.bridge_pair")

Spawn = Callable[[Coroutine[Any, Any, Any]], None]


class MachineKeys:
    """Each paired computer's key, read once from the keychain, then memory only."""

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}

    async def get(self, mch: str) -> str:
        """The key. Raises KeychainError (KeychainLockedError when locked)."""
        key = self._keys.get(mch)
        if key is None:
            key = await keychain.get_machine_key(mch)
            self._keys[mch] = key
        return key

    async def get_or_none(self, mch: str) -> str | None:
        try:
            return await self.get(mch)
        except KeychainError:
            return None

    def put(self, mch: str, key: str) -> None:
        self._keys[mch] = key

    def drop(self, mch: str) -> None:
        self._keys.pop(mch, None)

    def clear(self) -> None:
        self._keys.clear()


async def _clean_remote(target: PairTarget, outcome: PairOutcome, policy: DialPolicy) -> None:
    """Undo a pairing on the other computer with the key it just gave us."""
    await remove_self(target.host, target.port, outcome.token, outcome.device_id, policy=policy)


async def _undo_key(mch: str, old_key: str | None) -> None:
    with contextlib.suppress(KeychainError):
        if old_key is not None:
            await keychain.store_machine_key(mch, old_key)
        else:
            await keychain.delete_machine_key(mch)


def _known(
    registry: MachineRegistry, outcome: PairOutcome, at_address: MachineRecord | None
) -> MachineRecord | None:
    """The record this pairing renews, if any."""
    if outcome.machine_id:
        found = registry.by_machine_id(outcome.machine_id)
        if found is not None:
            return found
    if at_address is not None and at_address.machine_id in (None, outcome.machine_id):
        return at_address
    return None


async def pair_machine(
    registry: MachineRegistry,
    keys: MachineKeys,
    target: PairTarget,
    *,
    policy: DialPolicy,
    wall: Callable[[], float],
    spawn: Spawn,
) -> MachineRecord:
    """Pair with the computer at *target* and store it. Raises PairError."""
    at_address = registry.by_address(target.host, target.port)
    replaces = await keys.get_or_none(at_address.id) if at_address is not None else None
    outcome = await redeem(target, device_name=desktop_name(), replaces=replaces, policy=policy)
    existing = _known(registry, outcome, at_address)
    if existing is None and registry.is_full:
        await _clean_remote(target, outcome, policy)
        raise PairError(
            "machine_limit",
            f"This computer already has {MAX_MACHINES} computers paired. Remove one first.",
        )
    mch = existing.id if existing is not None else registry.new_id()
    old_key = await keys.get_or_none(mch) if existing is not None else None
    try:
        await keychain.store_machine_key(mch, outcome.token)
    except KeychainError:
        await _clean_remote(target, outcome, policy)
        raise PairError(
            "keychain_locked", "Couldn't save the key in the keychain. Unlock it and try again."
        ) from None
    record = MachineRecord(
        id=mch,
        name=outcome.desktop_name or target.name or target.host,
        host=target.host,
        port=target.port,
        device_id=outcome.device_id,
        paired_at=existing.paired_at if existing is not None else iso_utc(wall()),
        machine_id=outcome.machine_id or (existing.machine_id if existing else None),
        last_seen=existing.last_seen if existing is not None else None,
    )
    try:
        registry.put(record)
    except (OSError, MachineLimitError):
        await _undo_key(mch, old_key)
        await _clean_remote(target, outcome, policy)
        raise PairError(
            "pair_failed", "Couldn't save the list of computers. Check the disk and try again."
        ) from None
    keys.put(mch, outcome.token)
    log.info(
        "paired a computer",
        extra={"extra_fields": {"machine": mch, "host": target.host, "port": target.port}},
    )
    moved = existing is not None and (existing.host, existing.port) != (target.host, target.port)
    if (
        moved
        and old_key is not None
        and existing is not None
        and (existing.device_id != outcome.device_id)
    ):
        # Same computer at a new address: retire the old entry there.
        spawn(remove_self(target.host, target.port, old_key, existing.device_id, policy=policy))
    return record


async def forget_machine(
    registry: MachineRegistry,
    keys: MachineKeys,
    record: MachineRecord,
    *,
    policy: DialPolicy,
    spawn: Spawn,
) -> str | None:
    """Keychain entry, then the file entry. An error frame, or None.

    The other computer is told in the background (best effort, 3 s) with
    the key read beforehand.
    """
    mch = record.id
    key = await keys.get_or_none(mch)
    try:
        await keychain.delete_machine_key(mch)
    except KeychainLockedError:
        return build_error(
            "keychain_locked", "The keychain is locked. Unlock it and remove the computer again."
        )
    except KeychainError:
        pass  # already gone
    try:
        registry.remove(mch)
    except OSError:
        return build_error("remove_failed", "Couldn't save the list of computers.")
    keys.drop(mch)
    if key is not None:
        spawn(remove_self(record.host, record.port, key, record.device_id, policy=policy))
    log.info("removed a computer", extra={"extra_fields": {"machine": mch}})
    return None
