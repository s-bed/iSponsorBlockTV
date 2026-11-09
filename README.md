# iSponsorBlockTV

[![ghcr.io Pulls](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fipitio.github.io%2Fbackage%2Fdmunozv04%2FiSponsorBlockTV%2Fisponsorblocktv.json&query=downloads&logo=github&label=ghcr.io%20pulls&style=flat)](https://ghcr.io/dmunozv04/isponsorblocktv)
[![Docker Pulls](https://img.shields.io/docker/pulls/dmunozv04/isponsorblocktv?logo=docker&style=flat)](https://hub.docker.com/r/dmunozv04/isponsorblocktv/)
[![GitHub Release](https://img.shields.io/github/v/release/dmunozv04/isponsorblocktv?logo=GitHub&style=flat)](https://github.com/dmunozv04/iSponsorBlockTV/releases/latest)
[![GitHub Repo stars](https://img.shields.io/github/stars/dmunozv04/isponsorblocktv?style=flat)](https://github.com/dmunozv04/isponsorblocktv)

iSponsorBlockTV is a self-hosted application that connects to your YouTube for TV
app (see compatibility below) and automatically skips segments (like Sponsors
or intros) in YouTube videos using the [SponsorBlock](https://sponsor.ajay.app/)
API. It can also auto mute and press the "Skip Ad" button the moment it becomes
available on YouTube ads.

## Installation

Check the [wiki](https://github.com/dmunozv04/iSponsorBlockTV/wiki/Installation)

### Home Assistant custom integration

On Home Assistant 2026.10 or newer, use the custom integration to run
iSponsorBlockTV and configure each TV from the Home Assistant UI.

1. From this repository, run `python3 tools/build_home_assistant.py`. Extract
   `dist/isponsorblocktv-home-assistant.zip` into the Home Assistant configuration
   directory so it contains `custom_components/isponsorblocktv/manifest.json`.
2. Restart Home Assistant. In **Settings → Devices & services**, configure a
   discovered iSponsorBlockTV device or select **Add integration → iSponsorBlockTV**.
3. Choose **YouTube TV code** and enter the code shown under **Settings → Link
   with TV code** in the YouTube app on your TV. If you already use the standalone
   service, choose **Existing screen ID** and paste that TV's `screen_id` from
   the `devices` list in `config.json`. Add each TV individually. Once Home
   Assistant takes over a TV, remove that TV from the standalone service's
   configuration or stop the standalone service if all TVs have moved.
4. Open the TV's integration options to configure categories, ad handling, and
   other settings for that TV.

Automatic discovery uses local SSDP/DIAL and only offers TVs that expose a
YouTube screen ID. Manual pairing works without local discovery. The
**YouTube connected** sensor reflects the YouTube app connection. If YouTube
rejects a saved pairing, use **Settings → System → Repairs** to enter a fresh TV
code. **Reconfigure** on the TV's integration entry also lets you re-pair it.

The TV exposes connection and pairing status, playback and ad state, and
SponsorBlock skip activity. The skip count and last-skip timestamp cover the
current integration load. Additional video and audio sensors can be enabled
from the device page.

#### Releases and previews

Add the fork as a custom integration repository in HACS on Home Assistant
2026.10 or Marketplace on 2026.11 and newer. Stable updates use the latest
production release. Turn on the repository's **Pre-release** switch to get
previews. That switch applies to the repository, so check the branch named in
the release title before installing.

- Pushes to custom branches publish a prerelease such as `2.12.0-beta.23`.
  Dependabot branches are excluded.
- Merges and direct pushes to `main` publish a stable release such as `2.12.0`.
  After the first release, a conventional commit with `!` or a
  `BREAKING CHANGE:` footer bumps the major version. A `feat:` commit bumps the
  minor version. Other changes bump the patch version. The workflow uses the
  highest bump since the previous stable release. Use a conventional commit
  title when squash-merging to carry the intended bump onto `main`.
- Both workflows run the Home Assistant tests before publishing. The release
  includes `isponsorblocktv.zip` for Marketplace and
  `isponsorblocktv-home-assistant.zip` for manual installation.

Use the production workflow for stable Home Assistant releases on this fork.
Marketplace selects stable releases from the repository's shared GitHub
release list. The workflows use GitHub's built-in token by default. If GitHub
denies publishing a preview that changes workflow files, set the optional
`HA_RELEASE_TOKEN` Actions secret for trusted branches to a repository-scoped
token with **Contents** and **Workflows** write access.

## Compatibility

Legend: ✅ = Working, ❌ = Not working, ❔ = Not tested

Open an issue/pull request if you have tested a device that isn't listed here.

| Device             | Status |
|:-------------------|:------:|
| Apple TV           |   ✅*   |
| Samsung TV (Tizen) |   ✅    |
| LG TV (WebOS)      |   ✅    |
| Android TV         |   ✅    |
| Chromecast         |   ✅    |
| Google TV          |   ✅    |
| Roku               |   ✅    |
| Fire TV            |   ✅    |
| CCwGTV             |   ✅    |
| Nintendo Switch    |   ✅    |
| Xbox One/Series    |   ✅    |
| Playstation 4/5    |   ✅    |

*Ad muting won't work when using AirPlay to send the audio to another speaker.

** Shorts aren't fully supported due to limitations on YouTube's side.
A single short can be seen by either selecting the "Disconnect" option in the
 warning shown
or by long pressing the thumbnail to open the menu and clicking play from there

## Usage

Run iSponsorBlockTV on a computer that has network access. It doesn't need to
be on the same network as the device, only access to youtube.com is required.

Auto discovery will require the computer to be on the same network as the device
during setup.
The device can also be manually added to iSponsorBlockTV with a YouTube TV code.
This code can be found in the settings page of your YouTube TV application.

## Libraries used

- [pyytlounge](https://github.com/FabioGNR/pyytlounge) Used to interact with the
  device
- asyncio and [aiohttp](https://github.com/aio-libs/aiohttp)
- [async-cache](https://github.com/iamsinghrajat/async-cache)
- [Textual](https://github.com/textualize/textual/) Used for the amazing new
  graphical configurator
- [ssdp](https://github.com/codingjoe/ssdp) Used for auto discovery

## Projects using this project

- [Home Assistant Addon](https://github.com/bertybuttface/addons/tree/main/isponsorblocktv)

## Contributing

1. Fork it (<https://github.com/dmunozv04/iSponsorBlockTV/fork>)
2. Create your feature branch (`git checkout -b my-new-feature`)
3. Commit your changes (`git commit -am 'Add some feature'`)
4. Push to the branch (`git push origin my-new-feature`)
5. Create a new Pull Request

For Home Assistant tests, install the test dependencies in a Python 3.14 virtual
environment and run the suite from the repository root. The tests unpack the
same integration ZIP that Home Assistant installs.

```sh
python -m pip install -r tests/requirements.txt
python -m pytest -p homeassistant --asyncio-mode=auto tests
```

## Contributors

[![Contributors](https://contrib.rocks/image?repo=dmunozv04/iSponsorBlockTV)](https://github.com/dmunozv04/iSponsorBlockTV/graphs/contributors)

Made with [contrib.rocks](https://contrib.rocks).

## License

[![GNU GPLv3](https://www.gnu.org/graphics/gplv3-127x51.png)](https://www.gnu.org/licenses/gpl-3.0.en.html)
