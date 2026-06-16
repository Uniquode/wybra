"""User profile infrastructure."""

from wybra.media import MediaCapability
from wybra.profile.capabilities import (
    ProfileCapability,
    ProfileCapabilityError,
    ProfileImage,
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


__all__ = (
    "ProfileCapability",
    "ProfileCapabilityError",
    "ProfileImage",
    "ProfileUser",
    "SiteProfileCapability",
    "profile_picture_storage_key",
    "setup_site",
)
