from __future__ import annotations

from enum import Enum


class HypervoltLockState(Enum):
    UNLOCKED = 0
    PENDING_LOCK = 1
    LOCKED = 2


class HypervoltChargeMode(Enum):
    BOOST = 0
    ECO = 1
    SUPER_ECO = 2


class HypervoltReleaseState(Enum):
    # Ready to charge, waiting for schedule/solar, or car has finished charging
    DEFAULT = 0

    # User cancelled the charge via Hypervolt
    RELEASED = 1


class HypervoltDeviceState:
    """Class to hold current state of Hypervolt charger"""

    def __init__(self, charger_id):
        self.charger_id = charger_id
        self.is_charging = None
        self.session_id = None
        self.session_watthours = None
        self.session_currency_spent = None
        self.session_carbon_saved_grams = None
        self.max_current_milliamps = None
        self.current_session_current_milliamps = None
        self.current_session_ct_current = None
        self.current_session_ct_power = None
        self.current_session_voltage = None
        self.led_brightness = None
        self.lock_state: HypervoltLockState = None
        self.charge_mode: HypervoltChargeMode = None
        self.release_state: HypervoltReleaseState = None
