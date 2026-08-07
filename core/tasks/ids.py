"""Task ids."""

from __future__ import annotations

import secrets


def new_task_id() -> str:
    return secrets.token_hex(4)
