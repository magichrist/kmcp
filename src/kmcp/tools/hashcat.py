"""Hashcat wrapper for GPU-accelerated hash cracking."""

from __future__ import annotations

import json
import shutil
from typing import Any

from kmcp.models import ToolError
from kmcp.responses import error_response
from kmcp.tools.base import BaseTool
from kmcp.tools.normalization import NormalizedToolMixin
from kmcp.tools.persist import parse_hashcat, run_tool_with_persistence
from kmcp.validation import validate_required


def _missing_binary_error(binary: str, install_hint: str) -> dict[str, Any]:
    """Return a structured, non-crashing error for an absent hashcat binary."""
    details = {
        "missing_binary": binary,
        "install_hint": install_hint,
        "message": f"Binary '{binary}' was not found in PATH. {install_hint}",
    }
    return error_response(
        ToolError(error="Missing dependency", details=json.dumps(details, indent=2))
    )


def _parse(stdout: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    creds = parse_hashcat(stdout)
    findings = [
        {
            "type": "cracked_password",
            "severity": "high",
            "title": f"Cracked hash: {c['username'][:24]}…",
            "description": f"password={c['password']}",
            "evidence": str(c),
        }
        for c in creds
    ]
    return findings, creds


class HashcatTool(BaseTool, NormalizedToolMixin):
    _skip_central_persist = True

    @property
    def name(self) -> str:
        return "hashcat"

    @property
    def description(self) -> str:
        return (
            "Hashcat GPU password recovery. Cracked hash:password pairs are "
            "parsed from output and stored as credentials."
        )

    @property
    def default_timeout(self) -> int:
        return 900

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "hash_file": {"type": "string", "description": "Path to the hash file"},
                "mode": {
                    "type": "integer",
                    "description": "Hash mode number (-m), e.g. 0 for MD5",
                },
                "wordlist": {"type": "string", "description": "Path to wordlist"},
                "rule": {"type": "string", "description": "Rule file (-r)"},
                "show": {
                    "type": "boolean",
                    "description": "Show cracked hashes (--show)",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds",
                    "default": 900,
                },
            },
            "required": ["hash_file"],
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        validate_required(arguments, "hash_file")

    def build_command(self, arguments: dict[str, Any]) -> list[str]:
        cmd = ["hashcat"]
        mode = arguments.get("mode")
        if mode is not None:
            cmd.extend(["-m", str(mode)])
        # hashcat's positional order is: hashfile [wordlist]. Putting the
        # wordlist first makes hashcat treat it as the hash file.
        cmd.append(arguments["hash_file"])
        wordlist = arguments.get("wordlist")
        if wordlist:
            cmd.append(wordlist)
        rule = arguments.get("rule")
        if rule:
            cmd.extend(["-r", rule])
        if arguments.get("show"):
            cmd.append("--show")
        return cmd

    def normalize(self, stdout: str, stderr: str, exit_code: int, **kwargs: Any) -> dict[str, Any]:
        creds = parse_hashcat(stdout)
        return {
            "normalized": {
                "cracked_count": len(creds),
                "credentials": creds,
                "raw_output": (stdout or "").strip()[:2000],
            }
        }

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self.validate(arguments)
        except ValueError as e:
            return error_response(ToolError(error="Validation error", details=str(e)))

        cmd = self.build_command(arguments)
        if shutil.which(cmd[0]) is None:
            return _missing_binary_error(
                cmd[0], "Install it with: apt update && apt install -y hashcat"
            )
        return await run_tool_with_persistence(self, arguments, parser=_parse)
