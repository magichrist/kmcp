"""Wfuzz wrapper for web fuzzing (directories, parameters, headers)."""

from __future__ import annotations

import shlex
from typing import Any

from kmcp.tools.base import BaseTool
from kmcp.tools.normalization import NormalizedToolMixin
from kmcp.tools.persist import parse_wfuzz, run_tool_with_persistence
from kmcp.validation import validate_required


def _filter_tokens(filters: str | list[str] | tuple[str, ...]) -> list[str]:
    """Normalize a filter into exactly one ``--filter <expression>`` pair.

    Accepts a bare expression (``200``), a pre-prefixed string
    (``--filter 200`` / ``--filter=200``), or a list of tokens.
    """
    if isinstance(filters, str):
        raw = filters.strip()
        if not raw:
            return []
        if raw.startswith("--filter="):
            expression = raw.split("=", 1)[1].strip()
        elif raw.startswith("--filter"):
            tokens = shlex.split(raw)
            if tokens and tokens[0] == "--filter":
                expression = " ".join(tokens[1:]).strip()
            else:
                expression = raw
        else:
            expression = raw
    else:
        tokens = [str(token).strip() for token in filters if str(token).strip()]
        if not tokens:
            return []
        if tokens[0].startswith("--filter="):
            expression = tokens[0].split("=", 1)[1].strip()
        elif tokens[0] == "--filter":
            expression = " ".join(tokens[1:]).strip()
        else:
            expression = " ".join(tokens)
    return ["--filter", expression] if expression else []


def _parse(stdout: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return parse_wfuzz(stdout), []


class WfuzzTool(BaseTool, NormalizedToolMixin):
    _skip_central_persist = True

    @property
    def name(self) -> str:
        return "wfuzz"

    @property
    def description(self) -> str:
        return (
            "wfuzz web fuzzer — fuzz directories, parameters, and headers using "
            "FUZZ placeholders. Result table hits are parsed into findings."
        )

    @property
    def default_timeout(self) -> int:
        return 600

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Target URL with FUZZ placeholder (e.g. http://host/FUZZ)",
                },
                "wordlist": {
                    "type": "string",
                    "description": "Path to wordlist",
                    "default": "/usr/share/wordlists/dirb/common.txt",
                },
                "methods": {
                    "type": "string",
                    "description": "HTTP method (-X), default GET",
                },
                "filters": {
                    "type": "string",
                    "description": "Filter expression, e.g. '--filter 200' → c=200",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds",
                    "default": 600,
                },
            },
            "required": ["target"],
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        validate_required(arguments, "target")
        target = arguments.get("target", "")
        if target and "FUZZ" not in target:
            raise ValueError("target URL must contain a FUZZ placeholder")

    def build_command(self, arguments: dict[str, Any]) -> list[str]:
        wordlist = arguments.get("wordlist") or "/usr/share/wordlists/dirb/common.txt"
        # wfuzz treats the first non-option positional as the payload-marked
        # URL. Options (including --filter) must come before it, otherwise it
        # is parsed as an extra payload and wfuzz dies with "Too many
        # arguments". Keep the target last.
        cmd = ["wfuzz", "-w", wordlist]
        methods = arguments.get("methods")
        if methods:
            cmd.extend(["-X", methods])
        filters = arguments.get("filters")
        if filters:
            cmd.extend(_filter_tokens(filters))
        cmd.append(arguments["target"])
        return cmd

    def normalize(self, stdout: str, stderr: str, exit_code: int, **kwargs: Any) -> dict[str, Any]:
        findings = parse_wfuzz(stdout)
        return {
            "normalized": {
                "hits_found": len(findings),
                "urls": [f["url"] for f in findings],
                "raw_output": (stdout or "").strip()[:2000],
            }
        }

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await run_tool_with_persistence(self, arguments, parser=_parse)
