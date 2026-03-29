# Copyright (c) 2025, Tomasz Pakuła Using Arch BTW

from __future__ import annotations

import json
import math
import os
import socket
from threading import Event, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from boxflat.connection_manager import MozaConnectionManager

PERCENT_SCALE = 100
DEFAULT_TELEMETRY_PORT = 27194
DEFAULT_TELEMETRY_ENABLED = True
TELEMETRY_SHUTDOWN_TIMEOUT = 1
NO_PACKET_HINT_SECONDS = 10


class TelemetryBridge:
    def __init__(self, connection_manager: "MozaConnectionManager", port: int = DEFAULT_TELEMETRY_PORT, enabled: bool = DEFAULT_TELEMETRY_ENABLED) -> None:
        self._cm = connection_manager
        self._shutdown = Event()
        self._last_mask = -1
        self._enabled = enabled

        self._host = "127.0.0.1"
        self._port = port
        self._debug = os.environ.get("BOXFLAT_TELEMETRY_DEBUG", "").lower() in ("1", "true", "yes", "on")

        if self._enabled:
            self._thread = Thread(target=self._worker, daemon=True)
            self._thread.start()
        else:
            self._thread = None


    def shutdown(self) -> None:
        self._shutdown.set()
        # Bridge loop uses a 1s socket timeout, so bounded join is sufficient here.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=TELEMETRY_SHUTDOWN_TIMEOUT)


    def _worker(self) -> None:
        has_received_packet = False
        hinted_no_packets = False
        waited_for_packets = 0

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            try:
                sock.bind((self._host, self._port))
            except OSError as e:
                print(f"Telemetry bridge disabled: {e}")
                return

            sock.settimeout(1)
            print(f"Telemetry bridge listening on udp://{self._host}:{self._port}")
            print(
                "Telemetry bridge expects UDP JSON packets from a game adapter. "
                "On Windows, Pit House can use game-specific plugins directly; Boxflat relies on this bridge input. "
                "Set BOXFLAT_TELEMETRY_DEBUG=1 for packet diagnostics."
            )

            while not self._shutdown.is_set():
                try:
                    payload, source = sock.recvfrom(4096)
                except socket.timeout:
                    if not has_received_packet:
                        waited_for_packets += 1
                        if not hinted_no_packets and waited_for_packets >= NO_PACKET_HINT_SECONDS:
                            print(
                                f"Telemetry bridge has not received packets on udp://{self._host}:{self._port} yet. "
                                "Most games (including ACC) need an external telemetry adapter that forwards JSON to this port "
                                "instead of sending directly like Pit House plugins on Windows."
                            )
                            hinted_no_packets = True
                    continue
                except OSError:
                    break

                if not has_received_packet:
                    print(f"Telemetry bridge received first packet from {source[0]}:{source[1]}")
                    has_received_packet = True

                source_text = f"{source[0]}:{source[1]}"
                mask = self._packet_to_mask(payload)
                if mask is None:
                    self._debug_log(f"dropped packet from {source_text}")
                    continue
                if mask == self._last_mask:
                    self._debug_log(f"ignored duplicate mask {mask} from {source_text}")
                    continue

                self._last_mask = mask
                # Wheel command payload is two bytes (LSB/MSB), while dash accepts full int mask.
                self._cm.set_setting([mask & 255, mask >> 8], "wheel-send-rpm-telemetry")
                self._cm.set_setting(mask, "dash-send-telemetry")
                self._debug_log(f"forwarded mask {mask} from {source_text}")


    def _packet_to_mask(self, payload: bytes) -> int | None:
        try:
            data = json.loads(payload.decode())
        except UnicodeDecodeError:
            self._debug_log("ignored packet: invalid UTF-8 payload")
            return None
        except json.JSONDecodeError:
            self._debug_log("ignored packet: invalid JSON payload")
            return None

        if not isinstance(data, dict):
            self._debug_log("ignored packet: JSON root is not an object")
            return None

        direct_mask = self._first_number(data, "rpm_led_mask", "rpmMask", "rpm-mask", "led_mask")
        if direct_mask is not None:
            if not math.isfinite(direct_mask):
                self._debug_log("ignored packet: rpm_led_mask is non-finite")
                return None
            try:
                return max(0, min(1023, int(direct_mask)))
            except (ValueError, OverflowError):
                self._debug_log("ignored packet: invalid rpm_led_mask value")
                return None

        ratio = self._first_number(data, "rpm_percent", "rpmPercent")
        if ratio is not None:
            if ratio < 0 or ratio > PERCENT_SCALE:
                return None
            ratio = ratio / PERCENT_SCALE
        else:
            ratio = self._first_number(data, "rpm_ratio", "rpmRatio")
        if ratio is None:
            rpm = self._first_number(data, "rpm", "engine_rpm", "engineRpm", "current_rpm", "currentRpm")
            max_rpm = self._first_number(data, "max_rpm", "maxRpm", "maxRPM", "rpm_max", "redline")
            if rpm is None or max_rpm is None or max_rpm <= 0:
                self._debug_log("ignored packet: missing or invalid rpm/max_rpm")
                return None
            ratio = rpm / max_rpm

        if not math.isfinite(ratio):
            self._debug_log("ignored packet: non-finite rpm ratio")
            return None
        if ratio < 0 or ratio > 1:
            self._debug_log("ignored packet: rpm ratio outside 0.0-1.0")
            return None
        lit_leds = int(round(ratio * 10))
        return (1 << lit_leds) - 1 if lit_leds > 0 else 0


    def _debug_log(self, message: str) -> None:
        if self._debug:
            print(f"Telemetry bridge: {message}")


    def _first_number(self, data: dict, *keys: str) -> float | None:
        for key in keys:
            if key not in data:
                continue

            value = data[key]
            if isinstance(value, bool):
                continue

            if isinstance(value, int | float):
                return value
        return None


def get_telemetry_bridge_port(default: int = DEFAULT_TELEMETRY_PORT) -> int:
    env_port = os.environ.get("BOXFLAT_TELEMETRY_PORT")
    if env_port is None:
        return default

    try:
        return int(env_port)
    except ValueError:
        print(f"Invalid BOXFLAT_TELEMETRY_PORT value '{env_port}', falling back to {default}")
        return default
