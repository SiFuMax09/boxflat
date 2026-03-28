# Copyright (c) 2025, Tomasz Pakuła Using Arch BTW

from __future__ import annotations

import json
import os
import socket
from threading import Event, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from boxflat.connection_manager import MozaConnectionManager


class TelemetryBridge:
    def __init__(self, connection_manager: "MozaConnectionManager") -> None:
        self._cm = connection_manager
        self._shutdown = Event()
        self._last_mask = -1

        self._host = "127.0.0.1"
        self._port = int(os.environ.get("BOXFLAT_TELEMETRY_PORT", "27194"))

        self._thread = Thread(target=self._worker, daemon=True)
        self._thread.start()


    def shutdown(self, *_) -> None:
        self._shutdown.set()


    def _worker(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self._host, self._port))
        except OSError as e:
            print(f"Telemetry bridge disabled: {e}")
            sock.close()
            return

        sock.settimeout(1)
        print(f"Telemetry bridge listening on udp://{self._host}:{self._port}")

        while not self._shutdown.is_set():
            try:
                payload, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            mask = self._packet_to_mask(payload)
            if mask is None or mask == self._last_mask:
                continue

            self._last_mask = mask
            self._cm.set_setting([mask & 255, mask >> 8], "wheel-send-rpm-telemetry")
            self._cm.set_setting(mask, "dash-send-telemetry")

        sock.close()


    def _packet_to_mask(self, payload: bytes) -> int | None:
        try:
            data = json.loads(payload.decode(errors="ignore"))
        except json.JSONDecodeError:
            return None

        if not isinstance(data, dict):
            return None

        direct_mask = self._first_number(data, "rpm_led_mask", "rpmMask", "rpm-mask", "led_mask")
        if direct_mask is not None:
            return max(0, min(1023, int(direct_mask)))

        ratio = self._first_number(data, "rpm_percent", "rpmPercent", "rpm_ratio", "rpmRatio")
        if ratio is None:
            rpm = self._first_number(data, "rpm", "engine_rpm", "engineRpm", "current_rpm", "currentRpm")
            max_rpm = self._first_number(data, "max_rpm", "maxRpm", "maxRPM", "rpm_max", "redline")
            if rpm is None or max_rpm is None or max_rpm <= 0:
                return None
            ratio = rpm / max_rpm
        elif ratio > 1:
            ratio = ratio / 100

        ratio = max(0, min(1, ratio))
        lit_leds = int(round(ratio * 10))
        return (1 << lit_leds) - 1 if lit_leds > 0 else 0


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
