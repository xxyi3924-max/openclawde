from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class RiskTier(str, Enum):
    LOW = "LOW"        # read-only: grep, read, list, web_search, fetch_url, read_notes
    MEDIUM = "MEDIUM"  # write but reversible: write_file, edit_file, write_note, task ops
    HIGH = "HIGH"      # destructive/exec: run_python, run_shell, delete_note, web servers


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict
    fn: Callable
    risk: RiskTier = RiskTier.MEDIUM
    planning_allowed: bool = False  # True = available even in read-only planning mode

    def to_api_dict(self) -> dict:
        """Return plain dict in Anthropic API tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
