from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from wybra.auth.result import (
    ERROR_INVALID_PASSWORD,
    ERROR_PASSWORD_TOO_SHORT,
    ERROR_PASSWORD_TOO_WEAK,
    Result,
)

PasswordStrengthLabel = Literal["weak", "fair", "good", "strong"]

MINIMUM_PERSONAL_FRAGMENT_LENGTH: Final = 4
DEFAULT_MINIMUM_LENGTH: Final = 12
DEFAULT_MINIMUM_SCORE: Final = 0.45
DEFAULT_MINIMUM_CHARACTER_CATEGORIES: Final = 2
COMMON_PASSWORD_SCORE_CAP: Final = 0.35
LENGTH_SCORE_DIVISOR: Final = 24
MAXIMUM_LENGTH_SCORE: Final = 0.55
CHARACTER_CATEGORY_SCORE: Final = 0.1
MAXIMUM_CHARACTER_CATEGORY_SCORE: Final = 0.35
UNIQUENESS_SCORE_DIVISOR: Final = 12
MAXIMUM_UNIQUENESS_RATIO: Final = 1.0
UNIQUENESS_SCORE_WEIGHT: Final = 0.2
MINIMUM_UNIQUE_CHARACTER_WARNING_COUNT: Final = 4
REPEATED_CHARACTER_WARNING_DIVISOR: Final = 3
WEAK_SCORE_THRESHOLD: Final = 0.25
FAIR_SCORE_THRESHOLD: Final = 0.5
GOOD_SCORE_THRESHOLD: Final = 0.75
DEFAULT_COMMON_PASSWORD_FRAGMENTS: Final[tuple[str, ...]] = (
    "admin",
    "changeme",
    "changeit",
    "letmein",
    "p4ssw0rd",
    "pass",
    "password",
    "qwerty",
    "test",
    "tester",
    "welcome",
)


@dataclass(frozen=True, slots=True)
class PasswordStrength:
    """Operator/UI-facing password strength result."""

    score: float
    label: PasswordStrengthLabel
    feedback: tuple[str, ...] = ()


class PasswordPolicy(Protocol):
    """Password policy boundary for validation and strength feedback."""

    def strength(self, password: str, user: Any | None = None) -> PasswordStrength:
        """Return a strength estimate suitable for UI feedback."""
        ...

    def validate(self, password: str, user: Any | None = None) -> Result[str]:
        """Return a branchable validation result for identity write paths."""
        ...


@dataclass(frozen=True, slots=True)
class DefaultPasswordPolicy:
    """Default password policy for local identity accounts."""

    minimum_length: int = DEFAULT_MINIMUM_LENGTH
    minimum_score: float = DEFAULT_MINIMUM_SCORE
    minimum_character_categories: int = DEFAULT_MINIMUM_CHARACTER_CATEGORIES
    common_fragments: tuple[str, ...] = DEFAULT_COMMON_PASSWORD_FRAGMENTS

    def strength(self, password: str, user: Any | None = None) -> PasswordStrength:
        if not password.strip():
            return PasswordStrength(
                score=0.0,
                label="weak",
                feedback=("Password must not be blank.",),
            )

        categories = _character_categories(password)
        personal_fragments = _personal_fragments(user)
        feedback = _strength_feedback(password, categories, self, personal_fragments)
        score = _strength_score(password, categories)

        lowered_password = password.casefold()
        if any(
            fragment in lowered_password
            for fragment in (*self.common_fragments, *personal_fragments)
        ):
            score = min(score, COMMON_PASSWORD_SCORE_CAP)

        return PasswordStrength(
            score=_clamp_score(score),
            label=_strength_label(score),
            feedback=tuple(feedback),
        )

    def validate(self, password: str, user: Any | None = None) -> Result[str]:
        if not password.strip():
            return Result.failure(
                ERROR_INVALID_PASSWORD,
                "Password must not be blank.",
            )

        if len(password) < self.minimum_length:
            return Result.failure(
                ERROR_PASSWORD_TOO_SHORT,
                f"Password must be at least {self.minimum_length} characters.",
            )

        categories = _character_categories(password)
        if len(categories) < self.minimum_character_categories:
            return Result.failure(
                ERROR_PASSWORD_TOO_WEAK,
                "Password must use more character variety.",
            )

        strength = self.strength(password, user)
        if strength.score < self.minimum_score:
            feedback = " ".join(strength.feedback).strip()
            message = "Password is too weak."
            if feedback:
                message = f"{message} {feedback}"
            return Result.failure(ERROR_PASSWORD_TOO_WEAK, message)

        return Result.ok()


def _strength_score(password: str, categories: frozenset[str]) -> float:
    length_score = min(len(password) / LENGTH_SCORE_DIVISOR, MAXIMUM_LENGTH_SCORE)
    category_score = min(
        len(categories) * CHARACTER_CATEGORY_SCORE,
        MAXIMUM_CHARACTER_CATEGORY_SCORE,
    )
    uniqueness_score = (
        min(len(set(password)) / UNIQUENESS_SCORE_DIVISOR, MAXIMUM_UNIQUENESS_RATIO)
        * UNIQUENESS_SCORE_WEIGHT
    )
    return length_score + category_score + uniqueness_score


def _character_categories(password: str) -> frozenset[str]:
    categories: set[str] = set()
    if any(char.islower() for char in password):
        categories.add("lowercase")
    if any(char.isupper() for char in password):
        categories.add("uppercase")
    if any(char.isdigit() for char in password):
        categories.add("digit")
    if any(char.isspace() for char in password):
        categories.add("separator")
    if any(not char.isalnum() and not char.isspace() for char in password):
        categories.add("symbol")
    return frozenset(categories)


def _strength_feedback(
    password: str,
    categories: frozenset[str],
    policy: DefaultPasswordPolicy,
    personal_fragments: frozenset[str],
) -> list[str]:
    feedback: list[str] = []
    if len(password) < policy.minimum_length:
        feedback.append(f"Use at least {policy.minimum_length} characters.")
    if len(categories) < policy.minimum_character_categories:
        feedback.append("Use more character variety.")
    if any(fragment in password.casefold() for fragment in policy.common_fragments):
        feedback.append("Avoid common password words.")
    if any(fragment in password.casefold() for fragment in personal_fragments):
        feedback.append("Avoid using account details.")
    minimum_unique_characters = max(
        MINIMUM_UNIQUE_CHARACTER_WARNING_COUNT,
        len(password) // REPEATED_CHARACTER_WARNING_DIVISOR,
    )
    if len(set(password)) < minimum_unique_characters:
        feedback.append("Avoid repeated characters.")
    return feedback


def _personal_fragments(user: Any | None) -> frozenset[str]:
    if user is None:
        return frozenset()

    fragments: set[str] = set()
    for attribute in (
        "email",
        "display_name",
        "preferred_name",
        "full_name",
        "username",
        "id",
    ):
        value = getattr(user, attribute, None)
        if isinstance(value, str):
            fragments.update(_normalised_fragments(value))
        elif value is not None and attribute == "id":
            fragments.update(_normalised_fragments(str(value)))

    return frozenset(fragments)


def _normalised_fragments(value: str) -> set[str]:
    fragments: set[str] = set()
    normalised = value.casefold().strip()
    if len(normalised) >= MINIMUM_PERSONAL_FRAGMENT_LENGTH:
        fragments.add(normalised)

    if "@" in normalised:
        local_part, _, domain = normalised.partition("@")
        fragments.update(_normalised_fragments(local_part))
        fragments.update(_normalised_fragments(domain))

    for separator in (" ", ".", "-", "_", "+", "@"):
        for part in normalised.split(separator):
            part = part.strip()
            if len(part) >= MINIMUM_PERSONAL_FRAGMENT_LENGTH:
                fragments.add(part)

    return fragments


def _strength_label(score: float) -> PasswordStrengthLabel:
    score = _clamp_score(score)
    if score < WEAK_SCORE_THRESHOLD:
        return "weak"
    if score < FAIR_SCORE_THRESHOLD:
        return "fair"
    if score < GOOD_SCORE_THRESHOLD:
        return "good"
    return "strong"


def _clamp_score(score: float) -> float:
    return max(0.0, min(1.0, score))
