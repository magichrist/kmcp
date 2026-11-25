"""SpiderFoot OSINT automation tool."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from typing import Any

from kmcp.execution import engine
from kmcp.models import ToolError
from kmcp.responses import error_response, success_response
from kmcp.tools.base import BaseTool
from kmcp.validation import validate_required, validate_timeout

# sfcli.py (the binary behind `spiderfoot-cli`) is an argparse front-end for
# a *client* CLI: `-s` is the server URL and there is no top-level `-m`/`-t`.
# Scans are started with the interactive `start` command, supplied via `-e`.
# Passing scan flags as argv produced "unrecognized arguments" errors.
_CLI_VALUE_FLAGS = {"-s", "--server", "-u", "--user", "-p", "--pass", "-P", "--passfile", "-l", "-o"}
_CLI_SWITCH_FLAGS = {"-d", "--debug", "-n", "-i", "--insecure", "-q", "-k", "-b", "-v"}
_PROTECTED_FLAGS = {"-e", "--exec"}  # reserved for our generated commands file


class SpiderfootTool(BaseTool):
    @property
    def name(self) -> str:
        return "spiderfoot"

    @property
    def description(self) -> str:
        return "Run SpiderFoot for OSINT automation and reconnaissance."

    @property
    def default_timeout(self) -> int:
        return 900

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Target domain, IP, or keyword",
                },
                "modules": {
                    "type": "string",
                    "description": "Specific modules to run (comma-separated, or 'all')",
                    "default": "all",
                },
                "extra_args": {
                    "type": "string",
                    "description": "Additional spiderfoot-cli arguments",
                },
                "timeout": {"type": "integer", "default": 900},
            },
            "required": ["target"],
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        validate_required(arguments, "target")
        if "timeout" in arguments:
            validate_timeout(arguments["timeout"], max_val=3600)

    def _commands_script(self, arguments: dict[str, Any]) -> str:
        """Build the non-interactive `start` command fed to sfcli.py via -e."""
        target = shlex.quote(str(arguments["target"]))
        modules = str(arguments.get("modules") or "all")
        if modules == "all":
            return f"start {target} -u all\n"
        return f"start {target} -m {shlex.quote(modules)}\n"

    def _filter_extra_args(self, extra: str) -> tuple[list[str], list[str]]:
        """Keep supported sfcli.py flags, drop flags the CLI does not accept."""
        kept: list[str] = []
        skipped: list[str] = []
        parts = shlex.split(extra)
        idx = 0
        while idx < len(parts):
            part = parts[idx]
            key = part.split("=", 1)[0]
            if key in _PROTECTED_FLAGS or key not in _CLI_VALUE_FLAGS | _CLI_SWITCH_FLAGS:
                skipped.append(part)
                idx += 1
                continue
            kept.append(part)
            if (
                key in _CLI_VALUE_FLAGS
                and "=" not in part
                and idx + 1 < len(parts)
                and not parts[idx + 1].startswith("-")
            ):
                idx += 1
                kept.append(parts[idx])
            idx += 1
        return kept, skipped

    def build_command(self, arguments: dict[str, Any]) -> list[str]:
        # sfcli.py reads scan commands from a file when given -e; a generated
        # temp file avoids a shell and keeps all argv values explicit.
        fd, path = tempfile.mkstemp(prefix="kmcp_spiderfoot_", suffix=".sf")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self._commands_script(arguments))
        self._commands_file = path

        cmd = ["spiderfoot-cli", "-e", path]
        extra = arguments.get("extra_args")
        self._skipped_args: list[str] = []
        if extra:
            kept, self._skipped_args = self._filter_extra_args(extra)
            cmd.extend(kept)
        return cmd

    def _cleanup_commands_file(self) -> None:
        path = getattr(self, "_commands_file", None)
        if not path:
            return
        try:
            os.unlink(path)
        except OSError:
            pass
        self._commands_file = ""

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self.validate(arguments)
        except ValueError as e:
            return error_response(ToolError(error="Validation error", details=str(e)))

        cmd = self.build_command(arguments)
        try:
            result = await engine.execute(
                command=cmd,
                tool=self.name,
                timeout=arguments.get("timeout", self.default_timeout),
            )
        finally:
            self._cleanup_commands_file()

        skipped = getattr(self, "_skipped_args", [])
        if skipped:
            note = (
                "SpiderFoot CLI does not accept these arguments at the top level; "
                f"they were skipped: {json.dumps(skipped)}. "
                "Use 'modules' for module selection."
        )
            result.stdout = (result.stdout or "") + "\n" + note
        return success_response(result)
