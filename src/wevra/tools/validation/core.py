from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    description: str
    passed: bool


@dataclass(frozen=True, slots=True)
class ValidationResult:
    name: str
    errors: tuple[str, ...]
    checks: tuple[ValidationCheck, ...] = ()

    @property
    def is_ok(self) -> bool:
        return not self.errors


def record_check(
    checks: list[ValidationCheck],
    errors: list[str],
    *,
    passed: bool,
    description: str,
    error: str | None = None,
) -> bool:
    checks.append(ValidationCheck(description=description, passed=passed))
    if not passed:
        errors.append(error or description)

    return passed


def read_text_for_validation(
    path: Path | Traversable,
    checks: list[ValidationCheck],
    errors: list[str],
    *,
    description: str,
) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        record_check(
            checks,
            errors,
            passed=False,
            description=description,
            error=f"Unable to read {path}: {exc}",
        )
        return None
