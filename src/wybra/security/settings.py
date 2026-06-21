"""Typed settings for the security module."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from wybra.config import BaseSettings
from wybra.config.types import ConfigDef
from wybra.security.config import module_config
from wybra.security.cors import CorsPolicySet, load_cors_policy_set
from wybra.security.headers import (
    CrossOriginOpenerPolicy,
    SecurityHeaderOptions,
    validate_cross_origin_opener_policy,
)


@dataclass(frozen=True, slots=True)
class SecuritySettings(BaseSettings):
    module_config: ClassVar[ConfigDef] = module_config
    config_section: ClassVar[str | None] = "app.security"

    cross_origin_opener_policy: CrossOriginOpenerPolicy | None = "same-origin"
    asset_cors: CorsPolicySet = field(default_factory=CorsPolicySet)

    @classmethod
    def load_settings(cls, config) -> SecuritySettings:  # type: ignore[override]
        asset_cors_values = cls.section_values(config, "app.assets.cors")
        return cls(
            **cls.settings_kwargs(config),
            asset_cors=load_cors_policy_set(asset_cors_values, "app.assets.cors"),
        )

    def __post_init__(self) -> None:
        validate_cross_origin_opener_policy(self.cross_origin_opener_policy)

    @property
    def header_options(self) -> SecurityHeaderOptions:
        return SecurityHeaderOptions(
            cross_origin_opener_policy=self.cross_origin_opener_policy
        )
