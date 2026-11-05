"""Voice Chat participant identity structures and helpers."""

from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass
class PublicIdentity:
    peer_id: int
    name: str
    username: str = ""
    kind: str = "user"


def escaped_notice(identity: PublicIdentity) -> tuple[str, str]:
    name = html.escape(identity.name[:256], quote=True)
    username = html.escape(identity.username[:64], quote=True) if identity.username else ""
    return name, username
