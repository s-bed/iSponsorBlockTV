"""Read the YouTube screen ID from a DIAL device without launching the app."""

from asyncio import IncompleteReadError, timeout
from dataclasses import dataclass, field
from ipaddress import ip_address
from xml.etree.ElementTree import ParseError, TreeBuilder, XMLParser, fromstring

from aiohttp import ClientError, ClientSession
from homeassistant.helpers.service_info.ssdp import ATTR_UPNP_FRIENDLY_NAME, SsdpServiceInfo
from yarl import URL

from .core.pairing import is_valid_screen_id

_MAX_APP_XML_BYTES = 64 * 1024


class _NoDoctype(TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ParseError("DIAL application XML must not contain a DTD")


@dataclass(frozen=True)
class DialScreen:
    """A screen ID seen on a local DIAL device."""

    screen_id: str = field(repr=False)
    name: str | None
    udn: str | None


def _dial_url(raw_url: str | None, responder: str) -> URL | None:
    if not raw_url:
        return None
    try:
        url = URL(raw_url)
        source = ip_address(responder)
        host = ip_address(url.host or "")
        # Reading the port checks that yarl can parse it before the request.
        port = url.port
    except ValueError:
        return None
    if (
        url.scheme not in {"http", "https"}
        or url.raw_user is not None
        or url.raw_password is not None
        or port is None
        or not 1 <= port <= 65535
        or host != source
        or source.is_loopback
        or source.is_unspecified
        or source.is_multicast
    ):
        return None
    return url


def dial_udn(discovery: SsdpServiceInfo) -> str | None:
    """Normalize the device identifier from the SSDP advertisement."""
    udn = (discovery.ssdp_udn or discovery.ssdp_usn.split("::", maxsplit=1)[0]).lower()
    return udn if udn.startswith("uuid:") else None


async def async_probe_dial(session: ClientSession, discovery: SsdpServiceInfo) -> DialScreen | None:
    """Use only URLs pointing to the SSDP responder and never follow redirects."""
    remote_addr = discovery.ssdp_headers.get("_remote_addr")
    if not isinstance(remote_addr, tuple) or not remote_addr or not isinstance(remote_addr[0], str):
        return None
    responder = remote_addr[0]
    if (location := _dial_url(discovery.ssdp_location, responder)) is None:
        return None

    try:
        async with timeout(5):
            async with session.get(location, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                application_url = _dial_url(response.headers.get("Application-URL"), responder)
            if application_url is None:
                return None
            async with session.get(
                application_url / "YouTube",
                headers={"Origin": "https://www.youtube.com"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return None
                try:
                    await response.content.readexactly(_MAX_APP_XML_BYTES + 1)
                except IncompleteReadError as error:
                    app_xml = error.partial
                else:
                    return None
            service = fromstring(app_xml, parser=XMLParser(target=_NoDoctype()))
    except (ClientError, TimeoutError, ValueError, ParseError):
        return None

    screen_id = service.findtext("{*}additionalData/{*}screenId", "").strip()
    if not is_valid_screen_id(screen_id):
        return None
    name = discovery.upnp.get(ATTR_UPNP_FRIENDLY_NAME)
    return DialScreen(
        screen_id,
        (name.strip() or None) if isinstance(name, str) else None,
        dial_udn(discovery),
    )
