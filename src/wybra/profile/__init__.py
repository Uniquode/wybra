"""User profile infrastructure."""

from wybra.auth import AuthCapability
from wybra.db import DatabaseCapability
from wybra.forms import FormsCapability
from wybra.media import MediaCapability
from wybra.profile.capabilities import (
    ProfileCapability,
    ProfileCapabilityError,
    ProfileImage,
    ProfileInputError,
    ProfileUser,
    SiteProfileCapability,
    profile_picture_storage_key,
)
from wybra.profile.editing import render_profile_bio
from wybra.profile.persistence import TortoiseProfileRepository
from wybra.profile.phone import (
    CountryChoice,
    NormalisedPhoneContact,
    SubdivisionChoice,
    country_choices,
    country_flag,
    normalise_phone_contact,
    subdivision_choices,
)
from wybra.profile.settings import (
    DEFAULT_EDITABLE_PROFILE_FIELDS,
    PROFILE_FIELD_METADATA,
    ProfileFieldMetadata,
    ProfilePronounOption,
    ProfileSettings,
    module_config,
)
from wybra.site import Site


async def setup_site(site: Site) -> None:
    site.provide_capability(
        ProfileCapability,
        SiteProfileCapability(
            site.capability_proxy(MediaCapability),
            TortoiseProfileRepository(site.capability_proxy(DatabaseCapability)),
        ),
    )


async def post_setup_site(site: Site) -> None:
    site.require_capability(AuthCapability)
    site.require_capability(DatabaseCapability)
    if ProfileSettings.load_settings(site.config).editing_enabled:
        site.require_capability(FormsCapability)
    capability = site.require_capability(ProfileCapability)
    if isinstance(capability, SiteProfileCapability):
        await capability.media.finalise_optional()
        if isinstance(capability.repository, TortoiseProfileRepository):
            await capability.repository.database.finalise_required()


__all__ = (
    "CountryChoice",
    "DEFAULT_EDITABLE_PROFILE_FIELDS",
    "NormalisedPhoneContact",
    "PROFILE_FIELD_METADATA",
    "ProfileCapability",
    "ProfileCapabilityError",
    "ProfileFieldMetadata",
    "ProfileImage",
    "ProfileInputError",
    "ProfilePronounOption",
    "ProfileSettings",
    "ProfileUser",
    "SiteProfileCapability",
    "SubdivisionChoice",
    "country_choices",
    "country_flag",
    "module_config",
    "normalise_phone_contact",
    "profile_picture_storage_key",
    "render_profile_bio",
    "post_setup_site",
    "setup_site",
    "subdivision_choices",
)
