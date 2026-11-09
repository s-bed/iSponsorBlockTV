"""Configuration for the iSponsorBlockTV integration."""

from .core.constants import SponsorBlock_api

DOMAIN = "isponsorblocktv"
CONF_SCREEN_ID = "screen_id"
CONF_DIAL_UDN = "dial_udn"
DIAL_SERVICE = "urn:dial-multiscreen-org:service:dial:1"

DEFAULT_OPTIONS = {
    "skip_categories": ["sponsor"],
    "minimum_skip_length": 1,
    "offset": 0,
    "mute_ads": False,
    "skip_ads": False,
    "disturb_short_videos": True,
    "short_video_duration_threshold": 30,
    "auto_play": True,
    "skip_count_tracking": True,
    "join_name": "iSponsorBlockTV",
    "sponsorblock_api_url": SponsorBlock_api,
    "apikey": "",
    "channel_whitelist": [],
    "use_proxy": False,
}
