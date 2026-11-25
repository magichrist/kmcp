"""Masscan wrapper for high-speed port scanning."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from kmcp.tools.base import BaseTool
from kmcp.tools.normalization import NormalizedToolMixin
from kmcp.tools.persist import parse_masscan, run_tool_with_persistence
from kmcp.validation import validate_ports, validate_required

# masscan does not resolve DNS names — it only accepts numeric addresses,
# CIDR blocks, or numeric address ranges (e.g. 10.0.0.1-10.0.0.20).
_DEFAULT_PORTS = "80,443,8080"
_IP_RANGE_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}-\d{1,3}(?:\.\d{1,3}){3}$")


def _is_numeric_target(target: str) -> bool:
    """True for an IP address, CIDR block, or numeric masscan range."""
    try:
        ipaddress.ip_address(target)
        return True
    except ValueError:
        pass
    try:
        ipaddress.ip_network(target, strict=False)
        return True
    except ValueError:
        pass
    return bool(_IP_RANGE_RE.match(target))


def _parse(stdout: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return parse_masscan(stdout), []


class MasscanTool(BaseTool, NormalizedToolMixin):
    _skip_central_persist = True

    @property
    def name(self) -> str:
        return "masscan"

    @property
    def description(self) -> str:
        return (
            "masscan — the fastest TCP/UDP port scanner. Discovered open ports "
            "are parsed into structured findings."
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
                    "description": "Target IP or CIDR range (e.g. 10.0.0.0/24)",
                },
                "ports": {
                    "type": "string",
                    "description": "Ports to scan (e.g. '80,443' or '1-65535')",
                },
                "rate": {
                    "type": "integer",
                    "description": "Packet rate (default 1000)",
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
        target = arguments["target"]
        if not _is_numeric_target(target):
            # A structured validation error is far better than emitting a
            # command that masscan will reject with a cryptic DNS error.
            raise ValueError(
                f"masscan requires a numeric IP address or CIDR range, got {target!r}. "
                "Resolve the hostname to an IP first (e.g. with nmap/httpx/dig)."
            )
        ports = arguments.get("ports")
        if ports:
            validate_ports(ports)

    def build_command(self, arguments: dict[str, Any]) -> list[str]:
        target = arguments["target"]
        if not _is_numeric_target(target):
            raise ValueError(
                f"masscan requires a numeric IP address or CIDR range, got {target!r}."
            )
        ports = arguments.get("ports") or _DEFAULT_PORTS
        cmd = ["masscan", target, "-p", ports]
        cmd.extend(["--rate", str(arguments.get("rate", 1000))])
        return cmd

    def normalize(self, stdout: str, stderr: str, exit_code: int, **kwargs: Any) -> dict[str, Any]:
        findings = parse_masscan(stdout)
        return {
            "normalized": {
                "open_ports": len(findings),
                "ports": [{"port": f["port"], "protocol": f["service"]} for f in findings],
                "raw_output": (stdout or "").strip()[:2000],
            }
        }

    async def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await run_tool_with_persistence(self, arguments, parser=_parse)
