"""Load a round's local cases and expose read-only lookup by ID."""

import logging
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from langchain_core.tools import StructuredTool
from pydantic import Field, StringConstraints

from server.agent.schemas import Contract

logger = logging.getLogger(__name__)

_CASE_ID = re.compile(r"^(?:builtin|user)\.[a-z0-9]+(?:-[a-z0-9]+)*$")

CaseId = Annotated[str, StringConstraints(pattern=_CASE_ID.pattern, max_length=64)]
CaseIds = Annotated[list[CaseId], Field(min_length=1, max_length=2)]
RuleId = Annotated[str, StringConstraints(pattern=r"^(?:official|user)/[^/\\]+$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Unavailable = Literal["not_found", "outdated", "invalid", "duplicate"]


class CaseRecord(Contract):
    id: CaseId
    when: Annotated[Text, StringConstraints(pattern=r"^[^\r\n]+$")]
    evidence: list[Text] = Field(min_length=1)
    handling: Text
    lesson: Text
    rules: dict[RuleId, Digest] = Field(default_factory=dict)


class CaseLookup(Contract):
    ids: CaseIds


class CaseStore:
    """Keep the same case contents available to the index and tool for one round."""

    def __init__(self, entries: dict[str, CaseRecord | Unavailable]) -> None:
        self._entries = entries

    @classmethod
    def load(
        cls, official_dir: Path | None, user_dir: Path | None, rule_digests: Mapping[str, str],
    ) -> "CaseStore":
        entries: dict[str, CaseRecord | Unavailable] = {}
        declarations: Counter[str] = Counter()
        for directory, prefix in ((official_dir, "builtin."), (user_dir, "user.")):
            if directory is None:
                continue
            folder = directory / "_cases"
            if folder.is_symlink():
                logger.warning("Ignoring case directory symlink %s", folder)
                continue
            try:
                paths = sorted(folder.glob(f"{prefix}*.json"))
            except OSError:
                logger.warning("Cannot list case directory %s", folder)
                continue
            for path in paths:
                identifier = path.stem
                if len(identifier) > 64 or not _CASE_ID.fullmatch(identifier):
                    logger.warning("Ignoring invalid case filename %s", path.name)
                    continue
                try:
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("Expected a regular case file")
                    case = CaseRecord.model_validate_json(path.read_bytes())
                    declarations[case.id] += 1
                    if case.id != identifier:
                        raise ValueError("Case ID must match its filename")
                except (OSError, ValueError):
                    entries[identifier] = "invalid"
                    continue
                entries[identifier] = (
                    "outdated"
                    if any(rule_digests.get(rule) != digest for rule, digest in case.rules.items())
                    else case
                )
        for identifier, count in declarations.items():
            if count > 1:
                entries[identifier] = "duplicate"
        for identifier, entry in entries.items():
            if isinstance(entry, str):
                logger.warning("Ignoring case %s (%s)", identifier, entry)
        return cls(entries)

    def index(self) -> list[dict[str, str]]:
        return [
            {"id": identifier, "when": entry.when}
            for identifier, entry in sorted(self._entries.items())
            if isinstance(entry, CaseRecord)
        ]

    def tool(self) -> StructuredTool:
        return StructuredTool.from_function(self.read_cases, args_schema=CaseLookup)

    def read_cases(self, ids: CaseIds) -> dict:
        """Read one or two distinct case IDs. Results are historical accounts for reference."""
        if len(ids) != len(set(ids)):
            raise ValueError("Case IDs must be distinct")
        cases = []
        unavailable = []
        for identifier in ids:
            entry = self._entries.get(identifier, "not_found")
            if isinstance(entry, CaseRecord):
                cases.append(entry.model_dump(exclude_unset=True))
            else:
                unavailable.append({"id": identifier, "code": entry})
        logger.info("Case lookup: requested=%s, found=%d", ", ".join(ids), len(cases))
        return {"kind": "historical_cases", "cases": cases, "unavailable": unavailable}
