"""FTMS Fitness Machine Control Point client (the write side).

Owns the *stateful* side of the FTMS protocol for one connected bike:

- Subscribes to indications on the Fitness Machine Control Point
  characteristic (``0x2AD9``).
- Issues opcodes (Request Control, Start, Set Target Power, Stop), each
  awaited against its corresponding response indication using the request
  opcode as a correlation key.
- Tracks ``is_controlling`` and ``last_target_watts`` so a reconnect after a
  drop can restore where the game left off.
- Clamps target wattage to configurable ``[min, max]`` bounds before any
  write reaches the trainer. Per project memory ``trainer-control-aggressive-
  defaults``, the *defaults* are permissive (0-800W, no auto-bailouts) but
  the clamping primitive lives here so the safety-conscious can tune via
  CLI flags.
- Publishes ``control_acquired`` / ``control_released`` / ``target_power_set``
  on the shared ``EventBus`` so the engine can light up ERG UI and observe
  the actual (post-clamp) wattage applied.

Pure protocol mechanics here — no policy. Disconnect bailouts, ramping, and
cadence-based safety belong above this layer in the CLI runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from typing import TYPE_CHECKING

from ..events import ControlAcquiredData, ControlReleasedData, DeviceKind, TargetPowerSetData
from ..ws_server import EventBus

if TYPE_CHECKING:
    from bleak import BleakClient

log = logging.getLogger(__name__)

CONTROL_POINT_CHAR_UUID = "00002ad9-0000-1000-8000-00805f9b34fb"

# Op codes per Bluetooth SIG FTMS v1.0 §4.16.2.
OP_REQUEST_CONTROL = 0x00
OP_RESET = 0x01
OP_SET_TARGET_POWER = 0x05
OP_START = 0x07
OP_STOP = 0x08

# Indication shape: [0x80, request_op_code, result_code, ...params].
RESPONSE_OPCODE = 0x80
RESULT_SUCCESS = 0x01

# How long to wait for the trainer's response indication. Trainers usually
# respond in <100ms; 3s is comfortably permissive without hanging the source.
INDICATION_TIMEOUT_S = 3.0


class FtmsControl:
    """Bind a Bleak client + a device identity to the FTMS control protocol."""

    def __init__(
        self,
        client: BleakClient,
        bus: EventBus,
        *,
        device_kind: DeviceKind,
        device_name: str,
        max_watts: int,
        min_watts: int,
    ) -> None:
        self._client = client
        self._bus = bus
        self._device_kind = device_kind
        self._device_name = device_name
        self._max_watts = max_watts
        self._min_watts = min_watts
        self._is_controlling = False
        self._last_target_watts: int | None = None
        # request_opcode -> future awaiting the result_code byte
        self._pending: dict[int, asyncio.Future[int]] = {}

    @property
    def is_controlling(self) -> bool:
        return self._is_controlling

    @property
    def last_target_watts(self) -> int | None:
        return self._last_target_watts

    # --- BLE-side glue ---------------------------------------------------

    async def attach(self) -> None:
        """Subscribe to Control Point indications. Call once per connect."""
        await self._client.start_notify(CONTROL_POINT_CHAR_UUID, self._on_indication)

    def _on_indication(self, _sender: object, data: bytearray) -> None:
        if len(data) < 3 or data[0] != RESPONSE_OPCODE:
            return
        request_opcode = data[1]
        result_code = data[2]
        future = self._pending.pop(request_opcode, None)
        if future is not None and not future.done():
            future.set_result(result_code)

    async def _send_opcode(self, opcode: int, payload: bytes = b"") -> int:
        """Write opcode+payload to the Control Point and await the matching
        indication's result code. Raises asyncio.TimeoutError if the trainer
        doesn't respond within ``INDICATION_TIMEOUT_S``."""
        future: asyncio.Future[int] = asyncio.get_event_loop().create_future()
        self._pending[opcode] = future
        try:
            await self._client.write_gatt_char(
                CONTROL_POINT_CHAR_UUID,
                bytes([opcode]) + payload,
                response=True,
            )
            return await asyncio.wait_for(future, timeout=INDICATION_TIMEOUT_S)
        finally:
            self._pending.pop(opcode, None)

    # --- protocol commands ----------------------------------------------

    async def request_control_and_start(self) -> bool:
        """Claim control + enter the active workout state. Publishes
        ``control_acquired`` on success and restores ``last_target_watts``
        (handy for reconnect-after-drop). Logs and returns False on failure.
        """
        try:
            result = await self._send_opcode(OP_REQUEST_CONTROL)
            if result != RESULT_SUCCESS:
                log.warning(
                    "%s: Request Control rejected (result=0x%02x)",
                    self._device_name,
                    result,
                )
                return False
            result = await self._send_opcode(OP_START)
            if result != RESULT_SUCCESS:
                log.warning(
                    "%s: Start rejected (result=0x%02x)",
                    self._device_name,
                    result,
                )
                return False
        except TimeoutError:
            log.warning("%s: timed out waiting for control indication", self._device_name)
            return False
        except Exception:  # noqa: BLE001 — control claim is best-effort
            log.exception("%s: error claiming control", self._device_name)
            return False
        self._is_controlling = True
        await self._publish_control_acquired()
        # Restore previous target if we're reconnecting after a drop. Don't
        # blow up if the restore itself fails — the rider is here, not the
        # last value.
        if self._last_target_watts is not None:
            with contextlib.suppress(Exception):
                await self.set_target_power(self._last_target_watts, _restoring=True)
        return True

    async def set_target_power(self, watts: int, *, _restoring: bool = False) -> None:
        """Clamp + write Set Target Power. Publishes ``target_power_set`` with
        the clamped value on success or the requested value + reason on
        rejection. Pre-condition: control claimed."""
        if not self._is_controlling:
            await self._publish_target_set(int(watts), accepted=False, reason="not controlling")
            return
        clamped = max(self._min_watts, min(int(watts), self._max_watts))
        payload = struct.pack("<h", clamped)
        try:
            result = await self._send_opcode(OP_SET_TARGET_POWER, payload)
        except TimeoutError:
            await self._publish_target_set(clamped, accepted=False, reason="indication timeout")
            return
        except Exception as exc:  # noqa: BLE001 — surface as a typed event
            await self._publish_target_set(clamped, accepted=False, reason=f"ble error: {exc}")
            return
        if result != RESULT_SUCCESS:
            await self._publish_target_set(
                clamped,
                accepted=False,
                reason=f"trainer rejected (0x{result:02x})",
            )
            return
        self._last_target_watts = clamped
        # Mark restored writes so the engine can distinguish them in logs.
        await self._publish_target_set(
            clamped, accepted=True, reason="restored" if _restoring else ""
        )

    async def stop(self) -> None:
        """Issue Stop (best-effort) and release controlling state."""
        if not self._is_controlling:
            return
        with contextlib.suppress(Exception):
            # Stop opcode takes a sub-parameter: 0x01 = stop, 0x02 = pause.
            await self._send_opcode(OP_STOP, b"\x01")
        self._is_controlling = False
        await self._publish_control_released("stop")

    async def release(self, reason: str = "released") -> None:
        """Explicit release with a custom reason — used by disconnect bailouts
        and the ``release_control`` command. Idempotent."""
        if self._is_controlling:
            with contextlib.suppress(Exception):
                await self._send_opcode(OP_STOP, b"\x01")
            self._is_controlling = False
        await self._publish_control_released(reason)

    # --- event publishing -----------------------------------------------

    async def _publish_control_acquired(self) -> None:
        await self._bus.publish(
            type_="control_acquired",
            data=ControlAcquiredData(kind=self._device_kind, name=self._device_name),
            device_kind=self._device_kind,
        )

    async def _publish_control_released(self, reason: str) -> None:
        await self._bus.publish(
            type_="control_released",
            data=ControlReleasedData(
                kind=self._device_kind,
                name=self._device_name,
                reason=reason,
            ),
            device_kind=self._device_kind,
        )

    async def _publish_target_set(
        self,
        watts: int,
        *,
        accepted: bool,
        reason: str = "",
    ) -> None:
        await self._bus.publish(
            type_="target_power_set",
            data=TargetPowerSetData(watts=watts, accepted=accepted, reason=reason),
            device_kind=self._device_kind,
        )
