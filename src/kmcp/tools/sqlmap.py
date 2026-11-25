"""SQLMap SQL injection tool."""

from __future__ import annotations

import shlex
from typing import Any

from kmcp.execution import engine
from kmcp.models import ToolError
from kmcp.responses import error_response, success_response
from kmcp.tools.base import BaseTool
from kmcp.validation import validate_required, validate_timeout, validate_url

# sqlmap options that consume a value. When supplied as two tokens we emit the
# canonical ``--option=value`` form so values can never be read as positional
# targets (a common cause of "too many arguments" / "option requires argument").
_VALUE_OPTIONS = frozenset(
    {
        "-u", "--url", "-d", "--dbms", "--dbms-cred", "-r", "--request",
        "--data", "--param-del", "--cookie", "--cookie-del", "--load-cookies",
        "--user-agent", "--host", "--referer", "--headers", "--auth-type",
        "--auth-cred", "--auth-file", "--proxy", "--proxy-cred", "--proxy-file",
        "--tor-port", "--tor-type", "--delay", "--timeout", "--retries",
        "--randomize", "--scope", "--eval", "--technique", "--time-sec",
        "--union-cols", "--union-char", "--union-from", "--dns-domain",
        "--second-order", "--level", "--risk", "--threads", "--output-dir",
        "--results-file", "--prefix", "--suffix", "--skip", "--test-filter",
        "--test-skip", "--attempts", "--string", "--regexp", "--not-string",
        "--code", "-v", "--verbose", "--crawl", "--crawl-depth", "--exclude",
    }
)


def _normalize_extra_args(extra: str | list[str] | tuple[str, ...]) -> list[str]:
    """Return deterministic sqlmap tokens from agent-supplied extra_args."""
    if isinstance(extra, str):
        tokens = shlex.split(extra)
    else:
        tokens = [str(token) for token in extra if str(token).strip()]

    result: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-u", "--url"):
            # The wrapper owns the target (validated above); ignore overrides.
            index += 2 if index + 1 < len(tokens) and not tokens[index + 1].startswith("-") else 1
            continue
        if token == "--batch" or token.startswith("--batch="):
            # Emit --batch exactly once, at the end of the command.
            index += 1
            continue
        if (
            token.startswith("--")
            and token in _VALUE_OPTIONS
            and "=" not in token
            and index + 1 < len(tokens)
        ):
            value = tokens[index + 1]
            if not value.startswith("-"):
                result.append(f"{token}={value}")
                index += 2
                continue
        result.append(token)
        index += 1
    return result


class SqlmapTool(BaseTool):
    @property
    def name(self) -> str:
        return "sqlmap"

    @property
    def description(self) -> str:
        return "Run sqlmap for SQL injection detection and exploitation."

    @property
    def default_timeout(self) -> int:
        return 900

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Target URL with injectable parameter",
                },
                "extra_args": {
                    "type": "string",
                    "description": "Additional sqlmap arguments (e.g. '--dbs --batch')",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 900)",
                    "default": 900,
                },
            },
            "required": ["target"],
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        validate_required(arguments, "target")
        validate_url(arguments["target"])
        if "timeout" in arguments:
            validate_timeout(arguments["timeout"], max_val=3600)

    def build_command(self, arguments: dict[str, Any]) -> list[str]:
        cmd = ["sqlmap", "-u", arguments["target"]]
        extra = arguments.get("extra_args")
        if extra:
            cmd.extend(_normalize_extra_args(extra))
        cmd.append("--batch")
        return cmd

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self.validate(arguments)
        except ValueError as e:
            return error_response(ToolError(error="Validation error", details=str(e)))
        return success_response(
            await engine.execute(
                command=self.build_command(arguments),
                tool=self.name,
                timeout=arguments.get("timeout", self.default_timeout),
            )
        )
