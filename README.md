# iSponsorBlockTV

[![GitHub Release](https://img.shields.io/github/v/release/s-bed/iSponsorBlockTV?logo=github)](https://github.com/s-bed/iSponsorBlockTV/releases/latest)

iSponsorBlockTV is a custom integration for Home Assistant that connects to the
YouTube app on your TV. It can skip sponsor segments and intros with
[SponsorBlock](https://sponsor.ajay.app/), mute ads, skip ads when YouTube allows
it, interrupt short videos, and block selected channels. Playback and skip
activity are available as Home Assistant entities.

<!-- ha-marketplace:exclude-start -->

## Home Assistant custom integration

Requires Home Assistant 2026.10 or newer. Use one of these installation methods.

- **Marketplace (2026.11 or newer)**. Open **Settings → Marketplace → Add from
  link** and enter `https://github.com/s-bed/iSponsorBlockTV`. Connect GitHub if
  prompted, then open iSponsorBlockTV in the Marketplace and install it. See the
  [Marketplace documentation](https://www.home-assistant.io/integrations/marketplace/#custom-repositories).
- **HACS (2026.10)**. Open HACS, select **⋮ → Custom repositories**, and add
  `https://github.com/s-bed/iSponsorBlockTV` with repository type **Integration**.
  Open iSponsorBlockTV in HACS and select **Download**. See the
  [HACS documentation](https://www.hacs.xyz/docs/faq/custom_repositories/).
- **Manual**. Download `isponsorblocktv-home-assistant.zip` from the
  [latest release](https://github.com/s-bed/iSponsorBlockTV/releases/latest).
  Extract it into your Home Assistant configuration directory so it contains
  `custom_components/isponsorblocktv/manifest.json`.

<!-- ha-marketplace:exclude-end -->

## Set up a TV

1. Restart Home Assistant after installing the integration.
2. In **Settings → Devices & services**, configure a discovered iSponsorBlockTV
   device or select **Add integration → iSponsorBlockTV**.
3. For manual pairing, choose **YouTube TV code**. In the YouTube app on your TV,
   open **Settings → Link with TV code**, then enter the displayed code in Home
   Assistant. Add each TV individually.
4. Select **Configure** on the TV's integration entry to choose SponsorBlock
   categories, ad handling, and other playback settings for that TV.

Home Assistant needs internet access to reach YouTube and SponsorBlock. For
automatic discovery, the TV must be on the same local network and expose its
YouTube screen ID through SSDP/DIAL. You can also pair using a TV code without
local discovery.

If you are moving a TV from an earlier iSponsorBlockTV installation, you can
choose **Existing screen ID** instead of entering a TV code. Copy the TV's
`screen_id` from the `devices` list in the old `config.json`. Once you have paired
the TV in Home Assistant, stop the old service from managing that TV.

The **YouTube connected** sensor reflects the YouTube app connection. If YouTube
rejects a saved pairing, use **Settings → System → Repairs** to enter a fresh
TV code. **Reconfigure** on the TV's integration entry also lets you re-pair it.

## Playback and entities

The TV exposes connection and pairing status, playback and ad state, and
SponsorBlock skip activity. **Lifetime SponsorBlock skips** and **Last
SponsorBlock skip** are saved per TV across restarts and reloads, independently
of Home Assistant Recorder. Totals start at zero when upgrading from the
session-only counter. Additional video and audio sensors can be enabled from the
device page.

Each TV has an **Interrupt short videos** switch in the **Configuration**
section of its Home Assistant device page. It is on by default. Turn it off to
stop Shorts interruptions on that TV without reconnecting. The switch and the
TV's playback options use the same setting.

Once YouTube reports that a video is playing and gives its duration,
iSponsorBlockTV sends a Back command to dismiss videos shorter than the
configured threshold (30 seconds by default). This also applies to regular
videos under the threshold. Videos at or above the threshold and ads are
excluded.

YouTube can show the Shorts connection warning before it reports a video's
duration. With **Interrupt short videos** on, iSponsorBlockTV sends Back once
if it reconnects within 60 seconds of YouTube reporting a screen-initiated user
disconnect. This can also happen when you manually disconnect from the TV for
another reason.

**Last YouTube Shorts disruption** records when YouTube last acknowledged a
Back command from either of these rules. The timestamp is saved per TV across
Home Assistant restarts and remains available while the YouTube app is offline.

Home Assistant's
[Android TV Remote](https://www.home-assistant.io/integrations/androidtv_remote/#data-updates)
uses the TV's local remote service and can navigate YouTube without creating a
Lounge session. Its API lacks the playback duration needed for this rule.

### Block YouTube channels

To add **Global channel settings**, select **Add integration → iSponsorBlockTV →
Global channel settings**. Open **Configure** on that entry and set a shared
YouTube Data API key. Search by channel name or enter a channel ID. You can select
multiple results in one search. The integration saves the channel IDs so rules
survive renames.

Each TV uses the global list by default. To give a TV its own list, choose
**Channel blocklist for this TV** in that TV's options and turn on
**Override global blocklist**.
An empty override disables channel blocking for that TV. Turn the switch off
to return to the global list while keeping the TV's saved list.

The integration sends Back after the YouTube app reports a video from a blocked
channel, so playback can start briefly. The shared key also handles existing
SponsorBlock channel exclusions. If your TVs had different keys before the
upgrade, enter the key to use in **Global channel settings**.

### Playback troubleshooting

In **Settings → Devices & services → iSponsorBlockTV**, open the integration menu
and select **Enable debug logging**. Reproduce the playback or disconnect issue,
then select **Disable debug logging** to download the Home Assistant log. Search
for `custom_components.isponsorblocktv` and your TV's integration name.

The log shows what playback state YouTube reported after a `skipAd` or `seekTo`
request and whether YouTube acknowledged it. An acknowledgment confirms the
request was accepted. The following playback events show what the TV did.

For a disconnect, look for `Sending YouTube Lounge keepalive` and the following
`getNowPlaying` acknowledgment. The integration sends this request after about
a minute without a Lounge command while the TV is connected.

For a Shorts interruption, find `YouTube Shorts Back result` near the **Last
YouTube Shorts disruption** timestamp. The trigger shows which rule sent Back.
For the duration rule, the nearby playback event shows what duration YouTube
reported, the duration the integration used, and the ad state around the update.

### Keep live states without history

Use Home Assistant's [Recorder exclusions](https://www.home-assistant.io/integrations/recorder/#configure-filter)
to stop storing state changes from iSponsorBlockTV. In **Tools → Template**,
render this template to get the current entity IDs for your TVs.

```jinja
recorder:
  exclude:
    entities: {{ integration_entities('isponsorblocktv') | sort | to_json }}
```

Merge the rendered list into `recorder.exclude.entities` in `configuration.yaml`
and restart Home Assistant. Refresh the list after enabling, adding, or renaming
an entity. Current sensor values and lifetime skip totals remain available.

## Releases and previews

Marketplace and HACS offer stable releases by default. To receive previews,
enable and turn on the **Pre-release** switch for the repository's device. Check
the branch in the release title before installing, since the switch applies to
the whole repository. See the [Marketplace](https://www.home-assistant.io/integrations/marketplace/#keeping-installations-up-to-date)
and [HACS](https://www.hacs.xyz/docs/use/entities/switch/) guides.

- Pushes to custom branches publish a prerelease such as `2.12.0-beta.23`.
  Dependabot branches are excluded.
- Merges and direct pushes to `main` publish a stable release such as `2.12.0`.
  After the first release, a conventional commit with `!` or a
  `BREAKING CHANGE:` footer bumps the major version. A `feat:` commit bumps the
  minor version. Other changes bump the patch version. The workflow uses the
  highest bump since the previous stable release. Use a conventional commit
  title when squash-merging to carry the intended bump onto `main`.
- Both workflows run the Home Assistant tests before publishing. Each release
  includes `isponsorblocktv.zip` for Marketplace and HACS and
  `isponsorblocktv-home-assistant.zip` for manual installation.

## Compatibility

The original iSponsorBlockTV project reported that the YouTube TV connection
worked on the devices below. Home Assistant discovers devices that expose a
YouTube screen ID over DIAL. Try pairing with a YouTube TV code if your device
doesn't appear in discovery. Open an issue or pull request if you have tested a
device that isn't listed here.

| Device                    | Status |
| :------------------------ | :----: |
| Apple TV                  |  ✅*   |
| Samsung TV (Tizen)        |   ✅   |
| LG TV (webOS)             |   ✅   |
| Android TV                |   ✅   |
| Chromecast                |   ✅   |
| Google TV                 |   ✅   |
| Roku                      |   ✅   |
| Fire TV                   |   ✅   |
| Chromecast with Google TV |   ✅   |
| Nintendo Switch           |   ✅   |
| Xbox One/Series           |   ✅   |
| PlayStation 4/5           |   ✅   |

\* Ad muting does not work when Apple TV uses AirPlay to send its audio to
another speaker.

## Contributing

Open issues and pull requests in the [Home Assistant integration repository](https://github.com/s-bed/iSponsorBlockTV).
Install the test dependencies in a Python 3.14 virtual environment and run the
suite from the repository root.

```sh
python -m pip install -r tests/requirements.txt
python -m pytest
```

Release workflows use GitHub's built-in token. If GitHub declines to publish a
preview because the branch changes workflow files, maintainers can set the
optional `HA_RELEASE_TOKEN` Actions secret to a repository-scoped token with
write access to **Contents** and **Workflows**.

## Contributors

This integration builds on the original [iSponsorBlockTV project](https://github.com/dmunozv04/iSponsorBlockTV).

[![Contributors](https://contrib.rocks/image?repo=dmunozv04/iSponsorBlockTV)](https://github.com/dmunozv04/iSponsorBlockTV/graphs/contributors)

Made with [contrib.rocks](https://contrib.rocks).

## License

[![GNU GPLv3](https://www.gnu.org/graphics/gplv3-127x51.png)](https://www.gnu.org/licenses/gpl-3.0.en.html)
