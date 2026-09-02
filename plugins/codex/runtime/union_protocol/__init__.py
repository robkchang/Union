"""Union protocol: the wire models and cryptography every Union client shares.

A client needs three things from this package:

* `keys`     - generate and persist a node identity (Ed25519 + X25519).
* `signing`  - sign every request to the server; verify the server's roster.
* `seal`     - encrypt a message to one or many recipients; open one addressed to you.

`models` holds the pydantic models for every request, response, and event body.
The server validates with them and generates its OpenAPI document from them.
It needs the `models` extra (`pip install union-protocol[models]`); clients
that build plain dicts do not import it.
"""
from . import constants, keys, seal, signing, ulid  # noqa: F401

__all__ = ["constants", "keys", "models", "seal", "signing", "ulid"]
__version__ = "0.1.0"


def __getattr__(name):
    if name == "models":
        from . import models
        return models
    raise AttributeError(name)
