"""Tuya devices."""
import logging
from typing import Optional, Tuple, Union

from zigpy.quirks import CustomCluster, CustomDevice
import zigpy.types as t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import OnOff, PowerConfiguration
from zigpy.zcl.clusters.hvac import Thermostat, UserInterface

from .. import Bus, EventableCluster, LocalDataCluster
from ..const import DOUBLE_PRESS, LONG_PRESS, SHORT_PRESS

TUYA_CLUSTER_ID = 0xEF00
TUYA_SET_DATA = 0x0000
TUYA_GET_DATA = 0x0001
TUYA_SET_DATA_RESPONSE = 0x0002

SWITCH_EVENT = "switch_event"
ATTR_ON_OFF = 0x0000
TUYA_CMD_BASE = 0x0100

_LOGGER = logging.getLogger(__name__)


class Data(t.List, item_type=t.uint8_t):
    """list of uint8_t."""

    @classmethod
    def from_value(cls, value):
        """Convert from a zigpy typed value to a tuya data payload."""
        # serialized in little-endian by zigpy
        data = cls(value.serialize())
        # we want big-endian, with length prepended
        data.append(len(data))
        data.reverse()
        return data

    def to_value(self, ztype):
        """Convert from a tuya data payload to a zigpy typed value."""
        # first uint8_t is the length of the remaining data
        # tuya data is in big endian whereas ztypes use little endian
        value, _ = ztype.deserialize(bytes(reversed(self[1:])))
        return value


class TuyaManufCluster(CustomCluster):
    """Tuya manufacturer specific cluster."""

    name = "Tuya Manufacturer Specicific"
    cluster_id = TUYA_CLUSTER_ID
    ep_attribute = "tuya_manufacturer"

    class Command(t.Struct):
        """Tuya manufacturer cluster command."""

        status: t.uint8_t
        tsn: t.uint8_t
        command_id: t.uint16_t
        function: t.uint8_t
        data: Data

    manufacturer_server_commands = {0x0000: ("set_data", (Command,), False)}

    manufacturer_client_commands = {
        0x0001: ("get_data", (Command,), True),
        0x0002: ("set_data_response", (Command,), True),
    }


class TuyaManufClusterAttributes(TuyaManufCluster):
    """Manufacturer specific cluster for Tuya converting attributes <-> commands."""

    def handle_cluster_request(self, tsn: int, command_id: int, args: Tuple) -> None:
        """Handle cluster request."""
        if command_id not in (0x0001, 0x0002):
            return super().handle_cluster_request(tsn, command_id, args)

        tuya_cmd = args[0].command_id
        tuya_data = args[0].data

        _LOGGER.debug(
            "[0x%04x:%s:0x%04x] Received value %s "
            "for attribute 0x%04x (command 0x%04x)",
            self.endpoint.device.nwk,
            self.endpoint.endpoint_id,
            self.cluster_id,
            repr(tuya_data[1:]),
            tuya_cmd,
            command_id,
        )

        if tuya_cmd not in self.attributes:
            return

        ztype = self.attributes[tuya_cmd][1]
        zvalue = tuya_data.to_value(ztype)
        self._update_attribute(tuya_cmd, zvalue)

    def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None
    ):
        """Ignore remote reads as the "get_data" command doesn't seem to do anything."""

        return super().read_attributes(
            attributes, allow_cache=True, only_cache=True, manufacturer=manufacturer
        )

    async def write_attributes(self, attributes, manufacturer=None):
        """Defer attributes writing to the set_data tuya command."""

        records = self._write_attr_records(attributes)

        for record in records:
            cmd_payload = TuyaManufCluster.Command()
            cmd_payload.status = 0
            cmd_payload.tsn = self.endpoint.device.application.get_sequence()
            cmd_payload.command_id = record.attrid
            cmd_payload.function = 0
            cmd_payload.data = Data.from_value(record.value.value)

            await super().command(
                TUYA_SET_DATA,
                cmd_payload,
                manufacturer=manufacturer,
                expect_reply=False,
                tsn=cmd_payload.tsn,
            )

        return (foundation.Status.SUCCESS,)


class TuyaOnOff(CustomCluster, OnOff):
    """Tuya On/Off cluster for On/Off device."""

    def __init__(self, *args, **kwargs):
        """Init."""
        super().__init__(*args, **kwargs)
        self.endpoint.device.switch_bus.add_listener(self)

    def switch_event(self, channel, state):
        """Switch event."""
        _LOGGER.debug(
            "%s - Received switch event message, channel: %d, state: %d",
            self.endpoint.device.ieee,
            channel,
            state,
        )
        self._update_attribute(ATTR_ON_OFF, state)

    def command(
        self,
        command_id: Union[foundation.Command, int, t.uint8_t],
        *args,
        manufacturer: Optional[Union[int, t.uint16_t]] = None,
        expect_reply: bool = True,
        tsn: Optional[Union[int, t.uint8_t]] = None,
    ):
        """Override the default Cluster command."""

        if command_id in (0x0000, 0x0001):
            cmd_payload = TuyaManufCluster.Command()
            cmd_payload.status = 0
            cmd_payload.tsn = 0
            cmd_payload.command_id = TUYA_CMD_BASE + self.endpoint.endpoint_id
            cmd_payload.function = 0
            cmd_payload.data = [1, command_id]

            return self.endpoint.tuya_manufacturer.command(
                TUYA_SET_DATA, cmd_payload, expect_reply=True
            )

        return foundation.Status.UNSUP_CLUSTER_COMMAND


class TuyaManufacturerClusterOnOff(TuyaManufCluster):
    """Manufacturer Specific Cluster of On/Off device."""

    def handle_cluster_request(
        self, tsn: int, command_id: int, args: Tuple[TuyaManufCluster.Command]
    ) -> None:
        """Handle cluster request."""

        tuya_payload = args[0]
        if command_id in (0x0002, 0x0001):
            self.endpoint.device.switch_bus.listener_event(
                SWITCH_EVENT,
                tuya_payload.command_id - TUYA_CMD_BASE,
                tuya_payload.data[1],
            )


class TuyaSwitch(CustomDevice):
    """Tuya switch device."""

    def __init__(self, *args, **kwargs):
        """Init device."""
        self.switch_bus = Bus()
        super().__init__(*args, **kwargs)


class TuyaThermostatCluster(LocalDataCluster, Thermostat):
    """Thermostat cluster for Tuya thermostats."""

    _CONSTANT_ATTRIBUTES = {0x001B: Thermostat.ControlSequenceOfOperation.Heating_Only}

    def __init__(self, *args, **kwargs):
        """Init."""
        super().__init__(*args, **kwargs)
        self.endpoint.device.thermostat_bus.add_listener(self)

    def temperature_change(self, attr, value):
        """Local or target temperature change from device."""
        self._update_attribute(self.attridx[attr], value)

    def state_change(self, value):
        """State update from device."""
        if value == 0:
            mode = self.RunningMode.Off
            state = self.RunningState.Idle
        else:
            mode = self.RunningMode.Heat
            state = self.RunningState.Heat_State_On
        self._update_attribute(self.attridx["running_mode"], mode)
        self._update_attribute(self.attridx["running_state"], state)

    # pylint: disable=R0201
    def map_attribute(self, attribute, value):
        """Map standardized attribute value to dict of manufacturer values."""
        return {}

    async def write_attributes(self, attributes, manufacturer=None):
        """Implement writeable attributes."""

        records = self._write_attr_records(attributes)

        if not records:
            return (foundation.Status.SUCCESS,)

        manufacturer_attrs = {}
        for record in records:
            attr_name = self.attributes[record.attrid][0]
            new_attrs = self.map_attribute(attr_name, record.value.value)

            _LOGGER.debug(
                "[0x%04x:%s:0x%04x] Mapping standard %s (0x%04x) "
                "with value %s to custom %s",
                self.endpoint.device.nwk,
                self.endpoint.endpoint_id,
                self.cluster_id,
                attr_name,
                record.attrid,
                repr(record.value.value),
                repr(new_attrs),
            )

            manufacturer_attrs.update(new_attrs)

        if not manufacturer_attrs:
            return (foundation.Status.FAILURE,)

        await self.endpoint.tuya_manufacturer.write_attributes(
            manufacturer_attrs, manufacturer=manufacturer
        )

        return (foundation.Status.SUCCESS,)

    # pylint: disable=W0236
    async def command(
        self,
        command_id: Union[foundation.Command, int, t.uint8_t],
        *args,
        manufacturer: Optional[Union[int, t.uint16_t]] = None,
        expect_reply: bool = True,
        tsn: Optional[Union[int, t.uint8_t]] = None,
    ):
        """Implement thermostat commands."""

        if command_id != 0x0000:
            return foundation.Status.UNSUP_CLUSTER_COMMAND

        mode, offset = args
        if mode not in (self.SetpointMode.Heat, self.SetpointMode.Both):
            return foundation.Status.INVALID_VALUE

        attrid = self.attridx["occupied_heating_setpoint"]

        success, _ = await self.read_attributes((attrid,), manufacturer=manufacturer)
        try:
            current = success[attrid]
        except KeyError:
            return foundation.Status.FAILURE

        # offset is given in decidegrees, see Zigbee cluster specification
        return await self.write_attributes(
            {"occupied_heating_setpoint": current + offset * 10},
            manufacturer=manufacturer,
        )


class TuyaUserInterfaceCluster(LocalDataCluster, UserInterface):
    """HVAC User interface cluster for tuya thermostats."""

    def __init__(self, *args, **kwargs):
        """Init."""
        super().__init__(*args, **kwargs)
        self.endpoint.device.ui_bus.add_listener(self)

    def child_lock_change(self, mode):
        """Change of child lock setting."""
        if mode == 0:
            lockout = self.KeypadLockout.No_lockout
        else:
            lockout = self.KeypadLockout.Level_1_lockout

        self._update_attribute(self.attridx["keypad_lockout"], lockout)

    async def write_attributes(self, attributes, manufacturer=None):
        """Defer the keypad_lockout attribute to child_lock."""

        records = self._write_attr_records(attributes)

        for record in records:
            if record.attrid == self.attridx["keypad_lockout"]:
                lock = 0 if record.value.value == self.KeypadLockout.No_lockout else 1
                return await self.endpoint.tuya_manufacturer.write_attributes(
                    {self._CHILD_LOCK_ATTR: lock}, manufacturer=manufacturer
                )

        return (foundation.Status.FAILURE,)


class TuyaPowerConfigurationCluster(LocalDataCluster, PowerConfiguration):
    """PowerConfiguration cluster for battery-operated thermostats."""

    def __init__(self, *args, **kwargs):
        """Init."""
        super().__init__(*args, **kwargs)
        self.endpoint.device.battery_bus.add_listener(self)

    def battery_change(self, value):
        """Change of reported battery percentage remaining."""
        self._update_attribute(self.attridx["battery_percentage_remaining"], value * 2)


class TuyaThermostat(CustomDevice):
    """Generic Tuya thermostat device."""

    def __init__(self, *args, **kwargs):
        """Init device."""
        self.thermostat_bus = Bus()
        self.ui_bus = Bus()
        self.battery_bus = Bus()
        super().__init__(*args, **kwargs)


class TuyaSmartRemoteOnOffCluster(EventableCluster):
    """TuyaSmartRemoteOnOffCluster: this cluster manipulates messages from the remote control and converts them to command_ids."""

    cluster_id = 0x0006
    name = "TS004X_cluster"
    ep_attribute = "TS004X_cluster"

    server_commands = {
        0x00: (SHORT_PRESS, (), False),
        0x01: (DOUBLE_PRESS, (), False),
        0x02: (LONG_PRESS, (), False),
    }


class TuyaSmartRemote(CustomDevice):
    """Tuya scene x-channel remote device."""

    def __init__(self, *args, **kwargs):
        """Init."""
        self.last_code = -1
        super().__init__(*args, **kwargs)

    def handle_message(self, profile, cluster, src_ep, dst_ep, message):
        """Handle a device message."""
        if (
            profile == 260
            and cluster == 6
            and len(message) == 4
            and message[0] == 0x01
            and message[2] == 0xFD
        ):
            # use the 4th byte as command_id
            new_message = bytearray(4)
            new_message[0] = message[0]
            new_message[1] = message[1]
            new_message[2] = message[3]
            new_message[3] = 0
            message = type(message)(new_message)

        if self.last_code != message[1]:
            self.last_code = message[1]
            super().handle_message(profile, cluster, src_ep, dst_ep, message)
        else:
            _LOGGER.debug("TS004X: not handling duplicate frame")
