"""theHarvester OSINT tool."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from kmcp.execution import engine
from kmcp.models import ToolError
from kmcp.responses import error_response, success_response
from kmcp.tools.base import BaseTool
from kmcp.validation import validate_domain, validate_required, validate_timeout

# theHarvester reads proxies.yaml from this well-known path when proxy
# support is requested. When it is absent the tool aborts, so we drop proxy
# flags and explain instead of handing agents an opaque failure.
_PROXY_CONFIG = Path("/etc/theHarvester/proxies.yaml")
_PROXY_FLAGS = {"-p", "--proxy", "--proxies", "--proxy-file"}


class TheharvesterTool(BaseTool):
    @property
    def name(self) -> str:
        return "theharvester"

    @property
    def description(self) -> str:
        return "Run theHarvester for email, subdomain, and name harvesting from public sources."

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Target domain"},
                "source": {
                    "type": "string",
                    "description": "Data sources (e.g. 'all', 'google,bing,linkedin')",
                    "default": "all",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per source (default 500)",
                    "default": 500,
                },
                "extra_args": {
                    "type": "string",
                    "description": "Additional theHarvester arguments",
                },
                "timeout": {"type": "integer", "default": 300},
            },
            "required": ["domain"],
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        validate_required(arguments, "domain")
        validate_domain(arguments["domain"])
        if "timeout" in arguments:
            validate_timeout(arguments["timeout"])

    def build_command(self, arguments: dict[str, Any]) -> list[str]:
        cmd = [
            "theHarvester",
            "-d",
            arguments["domain"],
            "-b",
            arguments.get("source", "all"),
            "-l",
            str(arguments.get("limit", 500)),
        ]
        self._proxy_skipped = False
        extra = arguments.get("extra_args")
        if extra:
            parts = shlex.split(extra)
            if not _PROXY_CONFIG.is_file():
                filtered: list[str] = []
                idx = 0
                while idx < len(parts):
                    part = parts[idx]
                    if part in _PROXY_FLAGS or part.startswith(("--proxy=", "--proxies=")):
                        self._proxy_skipped = True
                        # Skip a detached value too (e.g. `--proxy-file /path`),
                        # but never swallow the next flag.
                        if (
                            "=" not in part
                            and idx + 1 < len(parts)
                            and not parts[idx + 1].startswith("-")
                        ):
                            idx += 1
                        idx += 1
                        continue
                    filtered.append(part)
                    idx += 1
                parts = filtered
            cmd.extend(parts)
        return cmd

    def _config_note(self) -> str:
        if _PROXY_CONFIG.is_file():
            return ""
        note = (
            f"Note: theHarvester proxy config {_PROXY_CONFIG} is missing; "
            "proxy support is unavailable. Provide a proxies.yaml at that path "
            "to enable it."
        )
        if getattr(self, "_proxy_skipped", False):
            note += " Proxy flags in extra_args were skipped so the scan could run."
        return note

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self.validate(arguments)
        except ValueError as e:
            return error_response(ToolError(error="Validation error", details=str(e)))
        result = await engine.execute(
                command=self.build_command(arguments),
                tool=self.name,
                timeout=arguments.get("timeout", self.default_timeout),
            )
        note = self._config_note()
        if note:
            result.stdout = (result.stdout or "") + "\n" + note
        return success_response(result)
