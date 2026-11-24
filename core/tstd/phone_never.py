"""What no phone permission opens (owner decision #5, TD-5301).

A paired phone acts as the person, but some acts are the computer's
whatever a phone was granted. They are hard refusals with no switch:

- releasing the computer-use stop (TD-5297) and ``shutdown``: both
  pure, read off the message (``phone_verbs.never_from_a_phone``);
- sending a saved key to a server it does not already go to;
- changing the mail server without typing the mail password again, which
  would send the saved password to the new server.

The last two depend on what is saved, so they are read against the live
config here, before the permission check: a phone that has the grant
gets the same answer as one that does not. ``add_model`` and the model
list already keep a saved key on its own server for every client
(TD-5182); this covers the two verbs that change where a key goes.
"""

from __future__ import annotations

from .config import ModelConfig, credential_base_url
from .key_hosts import key_hosts, key_refusal, origin
from .phone_verbs import never_from_a_phone
from .protocol import ClientMessage, SetCredential, SetEmailNotify, SetTierCredential

COMPUTER_ONLY = "Only the computer itself can do that."
KEY_ELSEWHERE = (
    "A phone cannot send a saved key to another server. Type the key for that "
    "server instead, or make this change at the computer."
)
MAIL_PASSWORD = (
    "To change the mail server from a phone, type the mail password again. "
    "The saved one is not sent to a new server."
)


def never_refusal(msg: ClientMessage, config: ModelConfig) -> str | None:
    """Why a phone may never send *msg*, or None. The same with any grant."""
    if never_from_a_phone(msg):
        return COMPUTER_ONLY
    if isinstance(msg, SetCredential):
        return KEY_ELSEWHERE if _moves_key(msg, config) else None
    if isinstance(msg, SetTierCredential):
        return KEY_ELSEWHERE if _binds_elsewhere(msg, config) else None
    if isinstance(msg, SetEmailNotify):
        return MAIL_PASSWORD if _new_mail_host(msg, config) else None
    return None


def _moves_key(msg: SetCredential, config: ModelConfig) -> bool:
    """A saved key's own host changed or cleared. A new key has no secret yet."""
    if msg.base_url is None or not msg.credential or msg.credential not in config.credentials:
        return False
    hosts = {origin(url) for url in key_hosts(config, msg.credential)}
    after = msg.base_url.strip()
    if not after:
        # Cleared: the key goes to the URL of each model it is bound to.
        cleared = config.model_copy(deep=True)
        cleared.credentials[msg.credential].base_url = None
        return any(origin(url) not in hosts for url in key_hosts(cleared, msg.credential))
    return origin(after) not in hosts


def _binds_elsewhere(msg: SetTierCredential, config: ModelConfig) -> bool:
    """A key saved without a host, bound to a model on a server it is not sent to.

    A key saved with a host goes there whatever it is bound to.
    """
    credential = msg.credential.strip()
    if not credential or credential_base_url(config, credential) is not None:
        return False
    preset = config.presets.get(msg.preset)
    tiers = preset.tier_map() if preset is not None else {}
    tier = next((t for name, t in tiers.items() if name == msg.tier), None)
    if tier is None:
        return False  # the handler refuses an unknown model itself
    return key_refusal(config, credential, tier.base_url) is not None


def _new_mail_host(msg: SetEmailNotify, config: ModelConfig) -> bool:
    """SMTP to a different server with the saved password, not a typed one."""
    if msg.method != "smtp" or msg.password:
        return False
    return msg.host.strip().lower() != config.notify.email.host.strip().lower()
