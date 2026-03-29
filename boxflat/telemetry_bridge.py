# Copyright (c) 2025, Tomasz Pakuła Using Arch BTW

from __future__ import annotations

import json
import math
import mmap
import os
import socket
import struct
import sys
import time
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from boxflat.connection_manager import MozaConnectionManager

PERCENT_SCALE = 100
DEFAULT_TELEMETRY_PORT = 27194
DEFAULT_TELEMETRY_ENABLED = True
TELEMETRY_SHUTDOWN_TIMEOUT = 1
NO_PACKET_HINT_SECONDS = 10
ACC_SHARED_MEMORY_POLL_SECONDS = 0.05
ACC_PHYSICS_RPM_OFFSET = 20
ACC_STATIC_MAX_RPM_OFFSET = 416
ACC_WINDOWS_PHYSICS_MAP = "Local\\acpmf_physics"
ACC_WINDOWS_STATIC_MAP = "Local\\acpmf_static"
ACC_WINDOWS_MAP_SIZE = 4096
ACC_LINUX_PHYSICS_PATHS = (
    "/dev/shm/acpmf_physics",
    "/dev/shm/Local\\acpmf_physics",
    "/dev/shm/Local_acpmf_physics",
)
ACC_LINUX_STATIC_PATHS = (
    "/dev/shm/acpmf_static",
    "/dev/shm/Local\\acpmf_static",
    "/dev/shm/Local_acpmf_static",
)


class TelemetryBridge:
    def __init__(self, connection_manager: "MozaConnectionManager", port: int = DEFAULT_TELEMETRY_PORT, enabled: bool = DEFAULT_TELEMETRY_ENABLED) -> None:
        self._cm = connection_manager
        self._shutdown = Event()
        self._has_received_input = Event()
        self._last_mask = -1
        self._mask_lock = Lock()
        self._enabled = enabled

        self._host = "127.0.0.1"
        self._port = port
        self._debug = os.environ.get("BOXFLAT_TELEMETRY_DEBUG", "").lower() in ("1", "true", "yes", "on")

        if self._enabled:
            self._thread = Thread(target=self._worker, daemon=True)
            self._thread.start()
            self._acc_thread = Thread(target=self._acc_worker, daemon=True)
            self._acc_thread.start()
        else:
            self._thread = None
            self._acc_thread = None


    def shutdown(self) -> None:
        self._shutdown.set()
        # Bridge loop uses a 1s socket timeout, so bounded join is sufficient here.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=TELEMETRY_SHUTDOWN_TIMEOUT)
        if self._acc_thread and self._acc_thread.is_alive():
            self._acc_thread.join(timeout=TELEMETRY_SHUTDOWN_TIMEOUT)


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
                "Telemetry bridge accepts UDP JSON packets from adapters and also reads ACC shared memory when available. "
                "Set BOXFLAT_TELEMETRY_DEBUG=1 for packet diagnostics."
            )

            while not self._shutdown.is_set():
                try:
                    payload, source = sock.recvfrom(4096)
                except socket.timeout:
                    if not self._has_received_input.is_set():
                        waited_for_packets += 1
                        if not hinted_no_packets and waited_for_packets >= NO_PACKET_HINT_SECONDS:
                            print(
                                f"Telemetry bridge has not received packets on udp://{self._host}:{self._port} yet. "
                                "For ACC, ensure shared memory is enabled/running. "
                                "For other games, forward telemetry JSON to this port."
                            )
                            hinted_no_packets = True
                    continue
                except OSError:
                    break

                if not has_received_packet:
                    print(f"Telemetry bridge received first packet from {source[0]}:{source[1]}")
                    has_received_packet = True
                    self._has_received_input.set()

                source_text = f"{source[0]}:{source[1]}"
                mask = self._packet_to_mask(payload)
                if mask is None:
                    self._debug_log(f"dropped packet from {source_text}")
                    continue
                self._forward_mask(mask, source_text)


    def _acc_worker(self) -> None:
        maps: dict | None = None
        has_announced = False
        while not self._shutdown.is_set():
            if maps is None:
                maps = self._open_acc_maps()
                if maps is None:
                    time.sleep(1)
                    continue
                if not has_announced:
                    print("Telemetry bridge ACC source enabled (shared memory).")
                    has_announced = True

            rpm_data = self._read_acc_rpm_data(maps)
            if rpm_data is None:
                self._close_acc_maps(maps)
                maps = None
                continue

            rpm, max_rpm = rpm_data
            if max_rpm > 0:
                ratio = rpm / max_rpm
                if 0 <= ratio <= 1 and math.isfinite(ratio):
                    lit_leds = int(round(ratio * 10))
                    mask = (1 << lit_leds) - 1 if lit_leds > 0 else 0
                    self._has_received_input.set()
                    self._forward_mask(mask, "acc-shm")
            time.sleep(ACC_SHARED_MEMORY_POLL_SECONDS)

        if maps is not None:
            self._close_acc_maps(maps)


    def _open_acc_maps(self) -> dict | None:
        if sys.platform == "win32":
            physics = self._open_windows_map(ACC_WINDOWS_PHYSICS_MAP)
            static = self._open_windows_map(ACC_WINDOWS_STATIC_MAP)
            if physics is None or static is None:
                for mm in (physics, static):
                    if mm is not None:
                        mm.close()
                return None
            return {"physics": physics, "static": static, "files": []}

        physics_file, physics = self._open_linux_map(ACC_LINUX_PHYSICS_PATHS)
        static_file, static = self._open_linux_map(ACC_LINUX_STATIC_PATHS)
        if physics is None or static is None:
            for mm in (physics, static):
                if mm is not None:
                    mm.close()
            for handle in (physics_file, static_file):
                if handle is not None:
                    handle.close()
            return None
        return {"physics": physics, "static": static, "files": [physics_file, static_file]}


    def _open_windows_map(self, name: str) -> mmap.mmap | None:
        try:
            return mmap.mmap(-1, ACC_WINDOWS_MAP_SIZE, tagname=name, access=mmap.ACCESS_READ)
        except (OSError, TypeError, ValueError):
            return None


    def _open_linux_map(self, paths: tuple[str, ...]) -> tuple[BinaryIO | None, mmap.mmap | None]:
        for path in paths:
            try:
                file_handle = open(path, "rb")
            except OSError:
                continue

            try:
                mm = mmap.mmap(file_handle.fileno(), 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError):
                file_handle.close()
                continue

            return file_handle, mm
        return None, None


    def _close_acc_maps(self, maps: dict) -> None:
        for mm in (maps["physics"], maps["static"]):
            try:
                mm.close()
            except OSError:
                pass
        for file_handle in maps["files"]:
            try:
                file_handle.close()
            except OSError:
                pass


    @staticmethod
    def _read_acc_rpm_data(maps: dict) -> tuple[int, int] | None:
        try:
            physics = maps["physics"]
            static = maps["static"]
            rpm = struct.unpack_from("<i", physics, ACC_PHYSICS_RPM_OFFSET)[0]
            max_rpm = struct.unpack_from("<i", static, ACC_STATIC_MAX_RPM_OFFSET)[0]
        except (OSError, ValueError, struct.error):
            return None
        return rpm, max_rpm


    def _forward_mask(self, mask: int, source_text: str) -> None:
        with self._mask_lock:
            if mask == self._last_mask:
                self._debug_log(f"ignored duplicate mask {mask} from {source_text}")
                return

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
