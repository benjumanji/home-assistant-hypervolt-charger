from __future__ import annotations

from copy import deepcopy
import datetime
import json
import logging
import random
import asyncio
import aiohttp

import websockets

from homeassistant.exceptions import HomeAssistantError

from .hypervolt_device_state import (
    HypervoltActivationMode,
    HypervoltChargeMode,
    HypervoltDeviceState,
    HypervoltLockState,
    HypervoltReleaseState,
    HypervoltScheduleInterval,
    HypervoltDayOfWeek,
)
from .timestamped_queue import TimestampedQueue
from .utils import get_days_from_days_of_week

_LOGGER = logging.getLogger(__name__)

MAX_STORED_SENT_MESSAGES = 20


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class HypervoltApiClient:
    def __init__(self, version, username, password, charger_id=None):
        """Set charger_id if known, or None during config, to allow chargers to be enumerated after login()"""
        self.version = version
        self.username = username
        self.password = password
        self.charger_id = charger_id

        self.websocket_sync: websockets.client.WebSocketClientProtocol = None
        self.websocket_session_in_progress: websockets.client.WebSocketClientProtocol = None

        # Store a list of messages sent to the sync websocket
        self.websocket_sync_sent_messages = []

        self.unload_requested = False
        self.session_total_energy_snapshots_queue = TimestampedQueue(1000)

    async def unload(self):
        """Call to close any websockets and cancel any work. This object cannot be used again"""

        _LOGGER.debug("Unload enter")

        self.unload_requested = True

        if self.websocket_sync:
            await self.websocket_sync.close()
        if self.websocket_session_in_progress:
            await self.websocket_session_in_progress.close()

    async def login(self, session: aiohttp.ClientSession) -> str:
        """Attempt to log in using credentials from config.
        Raises InvalidAuth or CannotConnect on failure.
        Returns the access token as a string on success."""

        try:
            session.headers["user-agent"] = self.get_user_agent()

            async with session.post(
                "https://auth.hypervolt.co.uk/oauth/token",
                data={
                    "audience": "https://api.hypervolt.co.uk",
                    "client_id": "1BfAeDNKfu7mfPWCm6XTsZeM2QYhhju2",  # Mimic mobile app. If Hypervolt are reading this: Please give the HA community our own Client ID and allow us to support the HA OAuth2 login flow
                    "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
                    "realm": "Username-Password-Authentication",
                    "scope": "openid profile email offline_access",
                    "username": self.username,
                    "password": self.password,
                },
            ) as response:
                if response.status >= 200 and response.status < 300:
                    _LOGGER.info("HypervoltApiClient logged in!")

                    response_text = await response.text()
                    access_token = json.loads(response_text)["access_token"]
                    session.headers["authorization"] = f"Bearer {access_token}"

                    # TODO: Handle refreshing the token

                    return access_token

                elif response.status >= 400 and response.status < 500:
                    _LOGGER.error(
                        f"Authentication error when trying to log in, status code: {response.status}"
                    )
                    raise InvalidAuth
                else:
                    response_text = await response.text()
                    _LOGGER.error(
                        f"Error: unable to login, status: {response.status}, {response_text}",
                    )
                    raise CannotConnect

        except InvalidAuth as exc:
            await session.close()
            raise InvalidAuth from exc
        except Exception as exc:
            await session.close()
            raise CannotConnect from exc

    async def get_chargers(self, session):
        """Returns an array like: [{"charger_id": 123, "created": "yyyy-MM-ddTHH:mm:ss.sssZ"}]
        Raises InvalidAuth"""
        async with session.get(
            "https://api.hypervolt.co.uk/charger/by-owner"
        ) as response:
            if response.status == 200:
                response_text = await response.text()
                return json.loads(response_text)["chargers"]

            elif response.status >= 400 and response.status < 500:
                _LOGGER.error(
                    "Could not get chargers, status code: %d", response.status
                )
                raise InvalidAuth

    async def v2_update_state_from_schedule(
        self, session: aiohttp.ClientSession, state: HypervoltDeviceState
    ) -> HypervoltDeviceState:
        """V2 charger only. Use API to update the state. Raise exception on error."""

        async with session.get(
            f"https://api.hypervolt.co.uk/charger/by-id/{self.charger_id}/schedule"
        ) as response:
            if response.status == 200:
                response_text = await response.text()
                _LOGGER.debug(f"Hypervolt charger schedule: {response_text}")

                jres = json.loads(response_text)
                if jres["enabled"]:
                    state.activation_mode = HypervoltActivationMode.SCHEDULE
                else:
                    state.activation_mode = HypervoltActivationMode.PLUG_AND_CHARGE

                if jres["type"]:
                    state.schedule_type = jres["type"]

                if jres["tz"]:
                    state.schedule_tz = jres["tz"]

                state.schedule_intervals = []
                for interval in jres["intervals"]:
                    start = interval[0]
                    end = interval[1]

                    state.schedule_intervals.append(
                        HypervoltScheduleInterval(
                            datetime.time(
                                start["hours"], start["minutes"], start["seconds"]
                            ),
                            datetime.time(end["hours"], end["minutes"], end["seconds"]),
                        )
                    )
                # Copy to schedule_intervals_to_apply
                state.schedule_intervals_to_apply = deepcopy(state.schedule_intervals)

            elif response.status == 401:
                _LOGGER.warning("Update_state_from_schedule, unauthorised")
                raise InvalidAuth
            else:
                _LOGGER.error(
                    "Update_state_from_schedule, error from API, status: %d",
                    response.status,
                )
                raise CannotConnect

        return state

    async def notify_on_hypervolt_sync_websocket(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        get_state_callback,
        on_state_updated_callback,
    ):
        """Open websocket to /sync endpoint and notify on updates. This function blocks indefinitely"""

        await self.notify_on_websocket(
            f"notify_on_websocket sync, {asyncio.current_task().get_name()},",
            f"wss://api.hypervolt.co.uk/ws/charger/{self.charger_id}/sync",
            session,
            access_token,
            get_state_callback,
            self.on_sync_websocket_connected,
            self.on_sync_websocket_message,
            on_state_updated_callback,
            self.on_sync_websocket_closed,
        )

    async def on_sync_websocket_message(
        self, message: str, get_state_callback, on_state_updated_callback
    ):
        """Handle messages coming back from the /sync websocket"""
        try:
            # Example messages:
            # {"jsonrpc":"2.0","id":"0","result":[{"brightness":0.25},{"lock_state":"unlocked"},{"release_state":"default"},{"max_current":32000},{"ct_flags":1},{"solar_mode":"boost"},{"features":["super_eco"]},{"random_start":true}]}
            # or
            # {"method":"sync.apply","params":[{"brightness":0.25}]}
            # or
            # {"jsonrpc":"2.0","id":"1","error":{"code":409,"error":"Concurrent modifications invalidated this request","data":null}}
            # or
            # {"jsonrpc":"2.0","id":0,"result":{"authenticated":true}}
            # So we need to handle both result and params structures and they can be an array or an object
            msg = json.loads(message)
            result = None
            method = None
            if "result" in msg:
                result = msg["result"]
            elif "params" in msg:
                method = msg.get("method", "")
                result = msg["params"]

            if not method and "id" in msg:
                # If this is a message response, find the method from the sent message
                msg_id = msg.get("id")
                for sent_message in self.websocket_sync_sent_messages:
                    if sent_message.get("id") == msg_id:
                        method = sent_message.get("method", "")
                        break

            state = get_state_callback()

            if method == "login":
                await self.on_message_login(result)
            elif method == "sync.snapshot" or method == "sync.apply":
                self.on_message_sync_snapshot(result, state)

                if on_state_updated_callback:
                    on_state_updated_callback(state)
            elif method == "get.session":
                self.on_message_session(result, state)

                if on_state_updated_callback:
                    on_state_updated_callback(state)
            # This appears to be an inconsistency with HV's naming: we get schedules (plural) but set schedule (singular).
            # In both cases, we get or set an array of sessions.
            elif method in ("schedules.get", "schedule.set"):
                self.on_message_schedule(result, state)

                if on_state_updated_callback:
                    on_state_updated_callback(state)
            else:
                _LOGGER.debug(
                    "On_sync_websocket_message_callback ignored message: %s",
                    message,
                )
        except Exception as exc:
            _LOGGER.warning(f"On_sync_websocket_message_callback error ${exc}")

    async def on_sync_websocket_connected(self, websocket, access_token):
        self.websocket_sync = websocket
        await self.send_sync_login_request(access_token)

    async def on_sync_websocket_closed(self):
        _LOGGER.debug("on_sync_websocket_closed")
        self.websocket_sync = None

    async def notify_on_websocket(
        self,
        log_prefix: str,
        url: str,
        session: aiohttp.ClientSession,
        access_token: str,
        get_state_callback,
        on_connected_callback,
        on_message_callback,
        on_state_updated_callback,
        on_closed_callback,
    ):
        """Open websocket to url and block forever. Handle reconnections and back-off
        Used as a common function for multiple websocket endpoints"""

        try:
            _LOGGER.debug(f"{log_prefix} enter")

            task_cancelled = False

            # If the connection is closed, we retry with an exponential back-off delay
            backoff_seconds = self.get_intial_backoff_delay_secs()

            async for websocket in websockets.connect(
                url,
                origin="https://hypervolt.co.uk",
                host="api.hypervolt.co.uk",
                user_agent_header=self.get_user_agent(),
            ):
                try:
                    _LOGGER.info(f"{log_prefix} connected")

                    # Calling asyncio.task.cancel between enter and connecting doesn't raise CancelledError nor set current_task().cancelled(), so we
                    # need a secondary check, via the HypervoltApiClient
                    if self.unload_requested:
                        _LOGGER.debug(f"{log_prefix} unload from connection loop")
                        raise asyncio.CancelledError

                    if on_connected_callback:
                        await on_connected_callback(websocket, access_token)

                    # From: https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html#using-a-connection,
                    # The iterator exits normally when the connection is closed with close code 1000 (OK) or 1001 (going away)
                    # or without a close code. It raises a ConnectionClosedError when the connection is closed with any other code.
                    msg_count = 0
                    async for message in websocket:
                        _LOGGER.debug(f"{log_prefix} recv: {message}")

                        # Calling asyncio.task.cancel between enter and connecting doesn't raise CancelledError nor set current_task().cancelled(), so we
                        # need a secondary check, via the HypervoltApiClient
                        if self.unload_requested:
                            _LOGGER.debug(f"{log_prefix} unload from message loop")
                            raise asyncio.CancelledError

                        # Pass message onto handler, also passing the callback to inform the caller of the updated state
                        if on_message_callback:
                            await on_message_callback(
                                message, get_state_callback, on_state_updated_callback
                            )

                        msg_count += 1
                        if msg_count > 3:
                            # If we get this far, we assume our connection is good and we can reset the back-off
                            backoff_seconds = self.get_intial_backoff_delay_secs()

                    _LOGGER.warning(
                        f"{log_prefix} iterator exited. Socket closed, code: {websocket.close_code}, reason: {websocket.close_reason}"
                    )

                except websockets.ConnectionClosedOK:
                    _LOGGER.warning(
                        f"{log_prefix} ConnectionClosedOK, code: {websocket.close_code}, reason: {websocket.close_reason}"
                    )
                    continue
                except websockets.ConnectionClosedError:
                    _LOGGER.warning(
                        f"{log_prefix} ConnectionClosedError, code: {websocket.close_code}, reason: {websocket.close_reason}"
                    )
                    continue
                except websockets.ConnectionClosed:
                    _LOGGER.warning(
                        f"{log_prefix} ConnectionClosed, code: {websocket.close_code}, reason: {websocket.close_reason}"
                    )
                    continue
                except asyncio.CancelledError as exc:
                    # Re-raise to break websocket loop
                    _LOGGER.debug(f"{log_prefix} cancelled (websocket loop)")
                    task_cancelled = True
                    raise exc
                except Exception as exc:
                    _LOGGER.warning(f"{log_prefix} exception ${exc}")
                    continue

                finally:
                    if on_closed_callback:
                        await on_closed_callback()

                    if self.unload_requested:
                        _LOGGER.debug(f"{log_prefix} unload from finally")
                        raise asyncio.CancelledError

                    if not task_cancelled:
                        # Apply back off here if we're not cancelled
                        _LOGGER.debug(
                            f"{log_prefix} backing off {backoff_seconds} seconds before reconnection attempt"
                        )
                        await asyncio.sleep(backoff_seconds)

                        # Increase back-off for next time
                        backoff_seconds = self.increase_backoff_delay(backoff_seconds)

                        # Attempt a re-login
                        access_token = await self.login(session)

        except asyncio.CancelledError as exc:
            _LOGGER.debug(f"{log_prefix} cancelled (main try/catch)")
        except Exception as exc:
            _LOGGER.error(f"{log_prefix} notify_on_hypervolt_sync_push error: {exc}")
        finally:
            _LOGGER.debug(f"{log_prefix} exit")

    def get_intial_backoff_delay_secs(self):
        """Get initial random delay for exponential back-off"""
        return random.randint(3, 12)

    def increase_backoff_delay(self, delay_secs: int):
        """Return increased back-off delay for next time, up to a max of 5 minutes"""
        return min(300, int(delay_secs * 1.7))

    async def notify_on_hypervolt_session_in_progress_websocket(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        get_state_callback,
        on_state_updated_callback,
    ):
        """Open websocket to /session/in-progress endpoint and notify on updates. This function blocks indefinitely"""

        await self.notify_on_websocket(
            f"notify_on_websocket session/in-progress, {asyncio.current_task().get_name()},",
            f"wss://api.hypervolt.co.uk/ws/charger/{self.charger_id}/session/in-progress",
            session,
            access_token,
            get_state_callback,
            self.on_session_in_progress_websocket_connected,
            self.on_session_in_progress_websocket_message,
            on_state_updated_callback,
            self.on_session_in_progress_websocket_closed,
        )

    async def on_session_in_progress_websocket_connected(
        self, websocket, access_token: str
    ):
        self.websocket_session_in_progress = websocket

    async def on_session_in_progress_websocket_message(
        self, message, get_state_callback, on_state_updated_callback
    ):
        try:
            # Example messages:
            # {"charging":false,"session":240,"milli_amps":32000,"true_milli_amps":0,"watt_hours":2371,"ccy_spent":34,"carbon_saved_grams":1036,"ct_current":0,"ct_power":0,"voltage":0}

            jmsg = json.loads(message)
            state = get_state_callback()

            self.on_message_session(jmsg, state)

            if on_state_updated_callback:
                on_state_updated_callback(state)

        except Exception as exc:
            _LOGGER.error(
                f"on_session_in_progress_websocket_message_callback message: {message}, error: {exc}"
            )

    async def on_session_in_progress_websocket_closed(self):
        _LOGGER.debug("on_session_in_progress_websocket_closed")
        self.websocket_session_in_progress = None

    async def send_message_to_sync(self, message: dict):
        if self.websocket_sync:
            if "jsonrpc" not in message:
                message["jsonrpc"] = "2.0"

            # Strip out token from logged message
            loggable_message = deepcopy(message)
            if "params" in loggable_message and "token" in loggable_message["params"]:
                loggable_message["params"]["token"] = "********"

            _LOGGER.debug(f"Send_message_to_sync: {json.dumps(loggable_message)}")

            jmessage = json.dumps(message)

            # Store in list and trim to max size
            self.websocket_sync_sent_messages.append(message)
            while len(self.websocket_sync_sent_messages) > MAX_STORED_SENT_MESSAGES:
                self.websocket_sync_sent_messages.pop(0)

            await self.websocket_sync.send(jmessage)
        else:
            _LOGGER.error(
                "Send_message_to_sync cannot send because websocket_sync is not set"
            )

    async def send_sync_login_request(self, access_token: str) -> bool:
        """Log in via websocket. Returns true if the sync websocket is ready, false otherwise"""
        if self.websocket_sync:
            message = {
                "id": self.get_next_message_id(),
                "method": "login",
                "params": {
                    "token": access_token,
                    "version": 2,
                },
            }
            await self.send_message_to_sync(message)
            return True
        else:
            return False

    async def send_sync_snapshot_request(self) -> bool:
        """Ask for a snapshot of the /sync state. Returns true if the sync websocket is ready, false otherwise"""
        if self.websocket_sync:
            message = {
                "id": self.get_next_message_id(),
                "method": "sync.snapshot",
            }
            await self.send_message_to_sync(message)
            return True
        else:
            return False

    async def send_sync_schedule_request(self) -> bool:
        """Ask for a copy of the schedule. Returns true if the sync websocket is ready, false otherwise"""
        if self.websocket_sync:
            message = {
                "id": self.get_next_message_id(),
                "method": "schedules.get",
            }
            await self.send_message_to_sync(message)
            return True
        else:
            return False

    async def send_sync_plugncharge_request(self) -> bool:
        """Ask for a copy of the plug and charge status. Returns true if the sync websocket is ready, false otherwise"""
        if self.websocket_sync:
            message = {
                "id": self.get_next_message_id(),
                "method": "plugncharge.get",
            }
            await self.send_message_to_sync(message)
            return True
        else:
            return False

    async def set_led_brightness(self, value: float):
        """Set the LED brightness, in the range [0.0, 1.0]"""
        message = {
            "id": self.get_next_message_id(),
            "method": "sync.apply",
            "params": {"brightness": value / 100},
        }
        await self.send_message_to_sync(message)

    async def set_max_current_milliamps(self, value: int):
        """Set the Max Current Limit, in the range [6, 32]"""
        message = {
            "id": self.get_next_message_id(),
            "method": "sync.apply",
            "params": {"max_current": value},
        }
        await self.send_message_to_sync(message)

    async def set_charge_mode(self, charge_mode: HypervoltChargeMode):
        """Set the charge mode from the passed in enum class"""
        message = {
            "id": self.get_next_message_id(),
            "method": "sync.apply",
            "params": {"solar_mode": charge_mode.name.lower()},
        }
        await self.send_message_to_sync(message)

    async def set_charging(self, charging: bool):
        """Set the charge state"""
        message = {
            "id": self.get_next_message_id(),
            "method": "sync.apply",
            "params": {"release": not charging},
        }
        await self.send_message_to_sync(message)

    def get_next_message_id(self) -> str:
        timestamp = datetime.datetime.utcnow().timestamp()
        return f"{int(timestamp * 1000000)}"

    async def set_lock_state(self, session: aiohttp.ClientSession, lock: bool):
        """Set the lock state"""
        message = {
            "id": self.get_next_message_id(),
            "method": "sync.apply",
            "params": {"is_locked": lock},
        }
        await self.send_message_to_sync(message)

    async def set_schedule(
        self,
        session: aiohttp.ClientSession,
        activation_mode: HypervoltActivationMode,
        schedule_intervals: list[HypervoltScheduleInterval],
        schedule_type,
        schedule_tz,
    ) -> HypervoltDeviceState:
        """Use API to update the state. Raise exception on error.

        Schedule_type and schedule_tz should have been obtained via by getting the schedule first
        I've only seen type of: "restricted" so not sure what other values are valid.
        """

        if self.get_charger_major_version() == 2:
            schedule_intervals_to_push = []
            for schedule_interval in schedule_intervals:
                schedule_intervals_to_push.append(
                    [
                        {
                            "hours": schedule_interval.start_time.hour,
                            "minutes": schedule_interval.start_time.minute,
                            "seconds": schedule_interval.start_time.second,
                        },
                        {
                            "hours": schedule_interval.end_time.hour,
                            "minutes": schedule_interval.end_time.minute,
                            "seconds": schedule_interval.end_time.second,
                        },
                    ]
                )

            # Use defaults for type and tz if not passed-in
            schedule_data = {
                "type": schedule_type if schedule_type else "restricted",
                "tz": schedule_tz if schedule_tz else "Europe/London",
                "enabled": activation_mode == HypervoltActivationMode.SCHEDULE,
                "intervals": schedule_intervals_to_push,
            }

            _LOGGER.debug("Set schedule: %s", schedule_data)

            async with session.put(
                url=f"https://api.hypervolt.co.uk/charger/by-id/{self.charger_id}/schedule",
                data=json.dumps(schedule_data),
                headers={"content-type": "application/json"},
            ) as response:
                if response.status == 200:
                    _LOGGER.debug(f"Schedule set")
                elif response.status == 401:
                    _LOGGER.warning("Set_schedule, unauthorised")
                    raise InvalidAuth
                else:
                    _LOGGER.error(
                        "Set_schedule, error from API, status: %d",
                        response.status,
                    )
                    raise CannotConnect
        else:
            # Version 3

            sessions = []
            for schedule_interval in schedule_intervals:
                session = {
                    "session_type": "recurring",
                    "start_time": schedule_interval.start_time.strftime("%H:%M"),
                    "end_time": schedule_interval.end_time.strftime("%H:%M"),
                    "mode": schedule_interval.charge_mode.name.lower(),
                    "days": get_days_from_days_of_week(schedule_interval.days_of_week),
                }
                sessions.append(session)

            message = {
                "id": self.get_next_message_id(),
                "method": "schedule.set",
                "params": {
                    "enabled": activation_mode == HypervoltActivationMode.SCHEDULE,
                    "is_default": False,
                    "type": "hypervolt",
                    "sessions": sessions,
                },
            }
            await self.send_message_to_sync(message)

    def get_user_agent(self) -> str:
        return f"home-assistant-hypervolt-charger/{self.version}"

    def get_charger_major_version(self) -> int:
        """Get the major version of the charger from the charger_id"""

        try:
            # Convert charger_id from decimal to hex and check how many bytes it corresponds to
            # Round up to the nearest even number of bytes
            charger_id_hex = hex(int(self.charger_id))[2:]
            num_id_bytes = (len(charger_id_hex) + 1) // 2 * 2

            if num_id_bytes == 12:
                return 2
            elif num_id_bytes == 16:
                return 3
            else:
                _LOGGER.warning(f"Unknown charger version from id: {self.charger_id}")
                # Take a guess
                return 3
        except Exception:
            _LOGGER.warning(
                f"Error parsing id to get charger version: {self.charger_id}"
            )
            # Take a guess
            return 3

    async def on_message_login(self, result: dict):
        """Handle a message from the /sync websocket that is a login response"""
        if result and "authenticated" in result and result["authenticated"]:
            # Get the various states, depending on what the charger supports
            await self.send_sync_snapshot_request()

            if self.get_charger_major_version() >= 3:
                # Version 3 chargers support schedules and plug and charge
                # Version 2 returns "schedules.get not allowed" and "plugncharge.get not allowed" for these
                await self.send_sync_schedule_request()
                await self.send_sync_plugncharge_request()
        else:
            raise InvalidAuth

    def on_message_sync_snapshot(self, result: list, state: HypervoltDeviceState):
        """Handle an update in the device state from the /sync websocket"""
        for item in result:
            # Only update state if properties are present, other leave state as-is
            if "brightness" in item:
                state.led_brightness = item["brightness"]
            if "lock_state" in item:
                state.lock_state = HypervoltLockState[item["lock_state"].upper()]
            if "max_current" in item:
                state.max_current_milliamps = item["max_current"]
            if "solar_mode" in item:
                state.charge_mode = HypervoltChargeMode[item["solar_mode"].upper()]
            if "release_state" in item:
                state.release_state = HypervoltReleaseState[
                    item["release_state"].upper()
                ]

    def on_message_session(self, result: dict, state: HypervoltDeviceState):
        """Handle an update to the current charging session"""

        prev_session_id = state.session_id

        # Only update state if properties are present, otherwise leave state as-is
        if "charging" in result:
            state.is_charging = result.get("charging")
        if "session" in result:
            state.session_id = result["session"]
        if "watt_hours" in result:
            state.session_watthours = result["watt_hours"]
        if "ccy_spent" in result:
            state.session_currency_spent = result["ccy_spent"]
        if "carbon_saved_grams" in result:
            state.session_carbon_saved_grams = result["carbon_saved_grams"]

        if "true_milli_amps" in result:
            state.current_session_current_milliamps = result["true_milli_amps"]
        if "ct_current" in result:
            state.current_session_ct_current = result["ct_current"]
        if "ct_power" in result:
            state.current_session_ct_power = result["ct_power"]
        if "voltage" in result:
            state.current_session_voltage = result["voltage"]

        # Calculate derived field: session_watthours_total_increasing
        if (
            not prev_session_id
            or not state.session_id
            or state.session_id == prev_session_id
        ):
            # Calculate the max seen session_watthours for this session
            # Only do this if we're currently charging. This is to avoid the situation where
            # HA restarts while not charging and we calculate a new value that is lower
            # than the max we saw last session. Thus, on a restart of HA when not charging
            # the value remains Unknown until the next charging session
            if state.session_watthours and state.is_charging:
                state.session_watthours_total_increasing = max(
                    state.session_watthours_total_increasing
                    if state.session_watthours_total_increasing
                    else 0,
                    state.session_watthours,
                )

                # Enqueue this energy value to help calculate the charger power
                self.session_total_energy_snapshots_queue.add(
                    state.session_watthours_total_increasing
                )
        else:
            _LOGGER.debug(
                "on_session_in_progress_websocket_message_callback new charging session detected"
            )

            # This is a new session, reset the value
            state.session_watthours_total_increasing = 0

        # Calculate derived field: current_session_power
        window_size_ms = 5 * 60 * 1000  # 5 minutes
        if state.is_charging and state.session_watthours_total_increasing:
            oldest_energy_value = (
                self.session_total_energy_snapshots_queue.head_element()
            )
            age_ms = oldest_energy_value.age_ms()
            if age_ms > 10000:  # 10 seconds
                energy_diff_wh = (
                    state.session_watthours_total_increasing - oldest_energy_value.value
                )
                state.current_session_power = int(
                    energy_diff_wh / (age_ms / 1000.0 / 3600.0)
                )
            else:
                # Not enough data points to update the power. Keep current value
                pass

        else:
            state.current_session_power = 0

        # Trim queue down to window size
        self.session_total_energy_snapshots_queue.delete_old_elements(window_size_ms)

    def on_message_schedule(self, result: dict, state: HypervoltDeviceState):
        """V3 only. Handle an update to the schedule."""
        applied = result.get("applied", None)
        if applied:
            if "enabled" in applied:
                schedule_enabled = applied["enabled"]
                state.activation_mode = (
                    HypervoltActivationMode.SCHEDULE
                    if schedule_enabled
                    else HypervoltActivationMode.PLUG_AND_CHARGE
                )
            if "sessions" in applied:
                state.schedule_intervals = []
                for session in applied["sessions"]:
                    days_of_week = 0
                    for day in session.get("days", []):
                        days_of_week |= HypervoltDayOfWeek[day.upper()].value

                    state.schedule_intervals.append(
                        HypervoltScheduleInterval(
                            datetime.time.fromisoformat(session["start_time"]),
                            datetime.time.fromisoformat(session["end_time"]),
                            HypervoltChargeMode[session["mode"].upper()],
                            days_of_week,
                        )
                    )

                # Copy to schedule_intervals_to_apply
                state.schedule_intervals_to_apply = deepcopy(state.schedule_intervals)

    async def v3_set_schedule_enabled(self, schedule_enabled: bool):
        """V3 only. Set the schedule enabled/disabled"""
        message = {
            "id": self.get_next_message_id(),
            "method": "schedule.set",
            "params": {"enabled": schedule_enabled},
        }
        await self.send_message_to_sync(message)
