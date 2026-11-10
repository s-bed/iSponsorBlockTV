import asyncio
import logging
from signal import SIGINT, SIGTERM, signal

import aiohttp

from . import api_helpers, ytlounge
from .debug_helpers import AiohttpTracer
from .playback import PlaybackScheduler


class DeviceListener:
    def __init__(self, api_helper, config, device, debug: bool, web_session):
        self.name = device.name
        self.logger = logging.getLogger(f"iSponsorBlockTV-{device.name}")
        self.lounge_controller = ytlounge.YtLoungeApi(
            device.screen_id, config, api_helper, self.logger, web_session=web_session
        )
        self.playback = PlaybackScheduler(
            api_helper, self.lounge_controller, device.offset, self.logger
        )

    # Ensures that we have a valid auth token
    async def refresh_auth_loop(self):
        while True:
            await asyncio.sleep(60 * 60 * 24)  # Refresh every 24 hours
            try:
                await self.lounge_controller.refresh_auth()
            except Exception as err:  # noqa: BLE001
                self.logger.debug(
                    "Could not refresh YouTube authorization (%s)", type(err).__name__
                )

    async def loop(self):
        lounge_controller = self.lounge_controller
        while True:
            try:
                if not lounge_controller.linked():
                    await lounge_controller.refresh_auth()
                if await lounge_controller.is_available() and await lounge_controller.connect():
                    self.logger.info(
                        "Connected to device %s (%s)", lounge_controller.screen_name, self.name
                    )
                    await lounge_controller.run_subscription(self.playback)
            except Exception as err:  # noqa: BLE001
                self.logger.debug("YouTube Lounge connection failed (%s)", type(err).__name__)
            finally:
                await self.cancel()
            await asyncio.sleep(10)

    async def cancel(self):
        await self.lounge_controller.stop_subscription()
        await self.playback.cancel()
        try:
            await self.lounge_controller.disconnect()
        except Exception as err:  # noqa: BLE001
            self.logger.debug("YouTube Lounge disconnect failed (%s)", type(err).__name__)
        self.lounge_controller.reset_playback()


async def finish(devices, web_session, tcp_connector):
    await asyncio.gather(*(device.cancel() for device in devices), return_exceptions=True)
    await web_session.close()
    await tcp_connector.close()


def handle_signal(signum, frame):
    raise KeyboardInterrupt()


async def main_async(config, debug, http_tracing):
    loop = asyncio.get_running_loop()
    tasks = []  # Save the tasks so the interpreter doesn't garbage collect them
    devices = []  # Save the devices to close them later
    if debug:
        loop.set_debug(True)

    tcp_connector = aiohttp.TCPConnector(ttl_dns_cache=300)

    # Configure session with tracing if enabled
    if http_tracing:
        root_logger = logging.getLogger("aiohttp_trace")
        tracer = AiohttpTracer(root_logger)
        trace_config = aiohttp.TraceConfig()
        trace_config.on_request_start.append(tracer.on_request_start)
        trace_config.on_response_chunk_received.append(tracer.on_response_chunk_received)
        trace_config.on_request_end.append(tracer.on_request_end)
        trace_config.on_request_exception.append(tracer.on_request_exception)
        web_session = aiohttp.ClientSession(
            trust_env=config.use_proxy, connector=tcp_connector, trace_configs=[trace_config]
        )
    else:
        web_session = aiohttp.ClientSession(trust_env=config.use_proxy, connector=tcp_connector)

    api_helper = api_helpers.ApiHelper(config, web_session)
    for i in config.devices:
        device = DeviceListener(api_helper, config, i, debug, web_session)
        devices.append(device)
        tasks.append(loop.create_task(device.loop()))
        tasks.append(loop.create_task(device.refresh_auth_loop()))
    signal(SIGTERM, handle_signal)
    signal(SIGINT, handle_signal)
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("Cancelling tasks and exiting...")
    finally:
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await api_helper.cancel_channel_lookups()
        await finish(devices, web_session, tcp_connector)
        print("Exited")


def main(config, debug, http_tracing):
    asyncio.run(main_async(config, debug, http_tracing))
