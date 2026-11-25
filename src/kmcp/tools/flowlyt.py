"""Flowlyt CI/CD security analyzer tool."""

from __future__ import annotations

import logging
import re
import shlex
from typing import Any

from kmcp.execution import engine
from kmcp.models import ToolError
from kmcp.responses import error_response, success_response
from kmcp.tools.base import BaseTool

logger = logging.getLogger("kmcp.tools")

# "owner/repo" is a remote GitHub-style slug, not a local filesystem path.
_SLUG_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


def _repo_flag(repo: str) -> str:
    """flowlyt needs an explicit --repo (path) or --url (remote reference)."""
    if repo.startswith(("http://", "https://", "git@")) or _SLUG_RE.match(repo):
        return "--url"
    return "--repo"


class FlowlytTool(BaseTool):
    @property
    def name(self) -> str:
        return "flowlyt"

    @property
    def description(self) -> str:
        return (
            "Run Flowlyt multi-platform CI/CD security analyzer. "
            "Scans GitHub/GitLab/Bitbucket repos and workflow files for security issues."
        )

    @property
    def default_timeout(self) -> int:
        return 300

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository to scan (e.g. 'owner/repo' or full URL)",
                },
                "workflow_file": {
                    "type": "string",
                    "description": "Specific workflow file to scan (e.g. '.github/workflows/ci.yml')",
                },
                "extra_args": {
                    "type": "string",
                    "description": "Additional Flowlyt arguments",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 300)",
                    "default": 300,
                },
            },
            "required": ["repo"],
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        pass

    def build_command(self, arguments: dict[str, Any]) -> list[str]:
        cmd = ["flowlyt", "scan"]

        repo = arguments["repo"]
        cmd.extend([_repo_flag(repo), repo])

        if arguments.get("workflow_file"):
            cmd.extend(["--file", arguments["workflow_file"]])

        extra = arguments.get("extra_args")
        if extra:
            cmd.extend(shlex.split(extra))

        return cmd

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self.validate(arguments)
        except ValueError as e:
            return error_response(ToolError(error="Validation error", details=str(e)))
        except Exception as e:
            logger.exception("Tool %s failed", self.name)
            return error_response(ToolError(error="Execution failed", details=str(e)))

        timeout = arguments.get("timeout", self.default_timeout)
        cmd = self.build_command(arguments)

        result = await engine.execute(cmd, tool=self.name, timeout=timeout)
        return success_response(result)
