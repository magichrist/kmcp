"""Amass attack surface mapping tool."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from kmcp.execution import engine
from kmcp.models import ToolError
from kmcp.responses import error_response, success_response
from kmcp.tools.base import BaseTool
from kmcp.validation import (
    validate_domain,
    validate_enum,
    validate_required,
    validate_timeout,
)

# Amass initialises libpostal name-generation data on first use and needs root
# for that download.  Never invoke sudo from the wrapper: that can block the
# MCP server on an interactive password prompt.
_LIBPOSTAL_DATA_DIRS = (
    Path("/usr/share/amass/data"),
    Path("/usr/share/amass/wordlists"),
    Path.home() / ".config" / "amass" / "data",
)


def _libpostal_data_available() -> bool:
    """Best-effort check that amass already has its data files seeded."""
    for directory in _LIBPOSTAL_DATA_DIRS:
        try:
            if directory.is_dir() and any(directory.iterdir()):
                return True
        except OSError:
            continue
    return False


def _root_required_error() -> dict[str, Any]:
    details = {
        "missing_privilege": "root",
        "reason": "amass needs root once to download/initialise libpostal name data",
        "install_hint": (
            "Run `sudo apt update && sudo apt install -y libpostal-data` and seed the "
            "data once with `sudo amass enum -d example.com`, then retry as the "
            "server user. This wrapper deliberately does not invoke sudo to avoid "
            "hanging on a password prompt."
        ),
    }
    return error_response(
        ToolError(
            error="Root required for amass data",
            details=json.dumps(details, indent=2),
        )
)


class AmassTool(BaseTool):
    @property
    def name(self) -> str:
        return "amass"

    @property
    def description(self) -> str:
        return "Run amass for attack surface mapping and subdomain enumeration."

    @property
    def default_timeout(self) -> int:
        return 900

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Target domain"},
                "mode": {
                    "type": "string",
                    "description": "Amass mode: 'enum' (default) or 'intel'",
                    "default": "enum",
                },
                "extra_args": {
                    "type": "string",
                    "description": "Additional amass arguments",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 900)",
                    "default": 900,
                },
            },
            "required": ["domain"],
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        validate_required(arguments, "domain")
        validate_domain(arguments["domain"])
        if "mode" in arguments:
            validate_enum(arguments["mode"], ["enum", "intel"])
        if "timeout" in arguments:
            validate_timeout(arguments["timeout"], max_val=3600)

    def build_command(self, arguments: dict[str, Any]) -> list[str]:
        mode = arguments.get("mode", "enum")
        cmd = ["amass", mode, "-d", arguments["domain"]]
        extra = arguments.get("extra_args")
        if extra:
            cmd.extend(shlex.split(extra))
        return cmd

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self.validate(arguments)
        except ValueError as e:
            return error_response(ToolError(error="Validation error", details=str(e)))
        # Avoid the root-only libpostal download path entirely for unprivileged
        # enum runs (intel mode needs no name data); emit a clear error instead
        # of waiting on a sudo prompt.
        if (
            arguments.get("mode", "enum") == "enum"
            and getattr(os, "geteuid", lambda: 0)() != 0
            and not _libpostal_data_available()
        ):
            return _root_required_error()
        return success_response(
            await engine.execute(
                command=self.build_command(arguments),
                tool=self.name,
                timeout=arguments.get("timeout", self.default_timeout),
            )
        )
