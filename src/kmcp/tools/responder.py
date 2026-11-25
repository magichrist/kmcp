"""Responder wrapper for LLMNR/NBT-NS/MDNS poisoning."""

from __future__ import annotations

import json
import re
from typing import Any

from kmcp.models import ToolError
from kmcp.responses import error_response
from kmcp.tools.base import BaseTool
from kmcp.tools.normalization import NormalizedToolMixin
from kmcp.tools.persist import parse_responder_hashes, run_tool_with_persistence
from kmcp.validation import validate_required

# Responder is a Python script; a missing python3-netifaces surfaces as an
# import traceback on stderr rather than a normal tool error.
_NETIFACES_ERROR = re.compile(r"No module named ['\"]?netifaces['\"]?", re.IGNORECASE)


def _parse(stdout: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings = parse_responder_hashes(stdout)
    return findings, []


class ResponderTool(BaseTool, NormalizedToolMixin):
    _skip_central_persist = True

    @property
    def name(self) -> str:
        return "responder"

    @property
    def description(self) -> str:
        return (
            "Responder LLMNR/NBT-NS/MDNS poisoner — captures authentication "
            "hashes (NTLMv1/NTLMv2) on the local network. Captured hashes are "
            "parsed from output and recorded as high-severity findings."
        )

    @property
    def default_timeout(self) -> int:
        return 300

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "interface": {
                    "type": "string",
                    "description": "Network interface to listen on (e.g. 'eth0')",
                },
                "analyze": {
                    "type": "boolean",
                    "description": "Passive analysis mode (poisoning disabled) (default false)",
                    "default": False,
                },
                "log_dir": {
                    "type": "string",
                    "description": "Directory for captured hash logs (default /usr/share/responder/logs)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 300)",
                    "default": 300,
                },
            },
            "required": ["interface"],
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        validate_required(arguments, "interface")

    def build_command(self, arguments: dict[str, Any]) -> list[str]:
        cmd = ["responder", "-I", arguments["interface"]]
        if arguments.get("analyze"):
            cmd.append("-A")
        log_dir = arguments.get("log_dir")
        if log_dir:
            cmd.extend(["-lw", log_dir])
        return cmd

    def normalize(self, stdout: str, stderr: str, exit_code: int, **kwargs: Any) -> dict[str, Any]:
        hashes = parse_responder_hashes(stdout)
        return {
            "normalized": {
                "hashes_captured": len(hashes),
                "poisoner_output": (stdout or "").strip()[:2000],
            }
        }

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await run_tool_with_persistence(self, arguments, parser=_parse)
        if result.get("isError"):
            return result

        # Convert the raw Python import traceback into a structured dependency
        # error so agents can self-correct instead of retrying blindly.
        try:
            payload = json.loads(result["content"][0]["text"])
            raw = payload.get("raw") or payload
            stderr = str(raw.get("stderr") or "")
        except (KeyError, TypeError, ValueError):
            return result

        if _NETIFACES_ERROR.search(stderr):
            details = {
                "missing_dependency": "python3-netifaces",
                "install_hint": (
                    "Install it with: apt update && apt install -y python3-netifaces "
                    "(or pip install netifaces for the Python interpreter that runs responder)."
                ),
                "stderr": stderr[-1000:],
            }
            return error_response(
                ToolError(
                    error="Missing dependency",
                    details=json.dumps(details, indent=2),
                )
            )
        return result
