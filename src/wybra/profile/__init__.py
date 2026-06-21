"""User profile infrastructure."""

from wybra.auth import AuthCapability
from wybra.db import DatabaseCapability
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
from wybra.site import Site


async def setup_site(site: Site) -> None:
    site.provide_capability(
        ProfileCapability,
        SiteProfileCapability(site.capability_proxy(MediaCapability)),
    )


async def post_setup_site(site: Site) -> None:
    site.require_capability(AuthCapability)
    site.require_capability(DatabaseCapability)
    capability = site.require_capability(ProfileCapability)
    if isinstance(capability, SiteProfileCapability):
        capability.media.finalise_optional()


__all__ = (
    "ProfileCapability",
    "ProfileCapabilityError",
    "ProfileImage",
    "ProfileInputError",
    "ProfileUser",
    "SiteProfileCapability",
    "profile_picture_storage_key",
    "post_setup_site",
    "setup_site",
)
