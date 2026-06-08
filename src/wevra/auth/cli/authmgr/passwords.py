from __future__ import annotations

import sys
from typing import Literal

import click

# Raw CLI input may include aliases; runtime code uses the normalised values.
PasswordSourceInput = Literal["-", "stdin", "prompt"]
PasswordSource = Literal["-", "prompt"]
PASSWORD_SOURCE_STDIN: PasswordSource = "-"
PASSWORD_SOURCE_PROMPT: PasswordSource = "prompt"
PASSWORD_SOURCE_STDIN_ALIAS: PasswordSourceInput = "stdin"


class PasswordSourceError(Exception):
    """Raised when a password source cannot produce a usable password."""


def _read_password(value: PasswordSource) -> str:
    match value:
        case "-":
            if sys.stdin.isatty():
                raise PasswordSourceError(
                    "Refusing to read password from interactive stdin; "
                    "pipe a password or omit --password for a hidden prompt."
                )
            line = sys.stdin.readline()
            if line == "":
                raise PasswordSourceError("No password received on stdin.")

            password = line.rstrip("\r\n")
            if sys.stdin.read(1):
                raise PasswordSourceError(
                    "Password stdin input must contain exactly one line."
                )
            return password
        case "prompt":
            return click.prompt(
                "Password",
                hide_input=True,
                confirmation_prompt=True,
                err=True,
            )
        case _:
            raise PasswordSourceError(f"Unsupported password source: {value!r}")


def _read_required_password(value: PasswordSource | None) -> str:
    if value is None:
        value = PASSWORD_SOURCE_PROMPT
    return _read_password(value)
