"""What an update changed, and what it could not do."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Report:
    changes: list[str] = field(default_factory=list)  # "C4: Value '0.1u' -> 'C'"
    warnings: list[str] = field(
        default_factory=list
    )  # done differently than KiCad might
    errors: list[str] = field(
        default_factory=list
    )  # could not be done; the file keeps it

    def change(self, message: str) -> None:
        self.changes.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def extend(self, other: Report, prefix: str = "") -> None:
        self.changes += [prefix + m for m in other.changes]
        self.warnings += [prefix + m for m in other.warnings]
        self.errors += [prefix + m for m in other.errors]

    @property
    def ok(self) -> bool:
        return not self.errors
