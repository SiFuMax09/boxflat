# Copyright (c) 2025, Tomasz Pakuła Using Arch BTW

from boxflat.connection_manager import MozaConnectionManager
from boxflat.settings_handler import SettingsHandler
from boxflat.telemetry_bridge import DEFAULT_TELEMETRY_ENABLED, DEFAULT_TELEMETRY_PORT
from boxflat.panels import SettingsPanel
from boxflat.widgets import *
from boxflat.bitwise import *
from threading import Thread, Event
from gi.repository import Gtk, Gio
from os import remove, environ
from os.path import join, expanduser
from shutil import copy2

# Portal stuff for flatpak autostart
if environ["BOXFLAT_FLATPAK_EDITION"] == "true":
    import gi
    gi.require_version("Xdp", "1.0")
    gi.require_version("XdpGtk4", "1.0")
    from gi.repository import Xdp, XdpGtk4


class OtherSettings(SettingsPanel):
    def __init__(self, button_callback,
        cm: MozaConnectionManager,
        hid_handler,
        settings: SettingsHandler,
        version: str,
        application: Adw.Application,
        data_path: str
    ):
        self._version = version
        self._settings = settings
        self._brake_row = None
        self._data_path = data_path

        super().__init__("Other", button_callback, connection_manager=cm, hid_handler=hid_handler)
        self._application = application


    def prepare_ui(self):
        self.add_preferences_group("Other settings")
        self._add_row(BoxflatSwitchRow("Base Bluetooth", "For the mobile app"))
        self._current_row.reverse_values()
        self._current_row.set_expression("*85")
        self._current_row.set_reverse_expression("/85")
        self._current_row.subscribe(self._cm.set_setting, "main-set-ble-mode")
        self._cm.subscribe("main-get-ble-mode", self._current_row.set_value)
        self._cm.subscribe_connected("base-limit", self._current_row.set_active)

        self._add_row(BoxflatSwitchRow("Base FH5 compatibility mode", "Please, disable this on Linux"))
        self._current_row.subscribe(self._cm.set_setting, "main-set-compat-mode")
        self._cm.subscribe("main-get-compat-mode", self._current_row.set_value)
        self._cm.subscribe_connected("main-get-compat-mode", self._current_row.set_present, +1)

        self._add_row(BoxflatSwitchRow("Pedals FH5 compatibility mode", "Please, disable this on Linux"))
        self._current_row.subscribe(self._cm.set_setting, "pedals-compat-mode")
        self._cm.subscribe("pedals-compat-mode", self._current_row.set_value)
        self._cm.subscribe_connected("pedals-compat-mode", self._current_row.set_present, +1)


        self.add_preferences_group("Application settings")

        self._add_row(BoxflatSwitchRow("Load default preset on application startup"))
        self._current_row.set_value(self._settings.read_setting("default-preset-on-startup") or 0)
        self._current_row.subscribe(self._settings.write_setting, "default-preset-on-startup")

        brake_row = BoxflatSwitchRow("Enable Brake Calibration", "Do it at your own risk")
        self._brake_row = brake_row
        self._add_row(brake_row)

        self._register_event("brake-calibration-enabled")
        brake_row.subscribe(self._settings.write_setting, "brake-calibration-enabled")
        brake_row.subscribe(lambda v: self._dispatch("brake-calibration-enabled", v))
        brake_row.set_value(self._settings.read_setting("brake-calibration-enabled"))

        fix_row = BoxflatSwitchRow("Enable Moza detection fixes", "Not needed with Proton 10+")
        self._fix_row = fix_row
        self._add_row(fix_row)

        self._register_event("moza-detection-fix-enabled")
        fix_row.subscribe(self._settings.write_setting, "moza-detection-fix-enabled")
        fix_row.subscribe(lambda v: self._dispatch("moza-detection-fix-enabled", v))
        fix_row.subscribe(self._hid_handler.set_detection_fix_enabled)
        fix_row.set_value(self._settings.read_setting("moza-detection-fix-enabled"))

        self._add_row(BoxflatSwitchRow("Enable telemetry bridge", "External game telemetry input"))
        telemetry_enabled = self._read_setting_default("telemetry-bridge-enabled", DEFAULT_TELEMETRY_ENABLED)
        self._current_row.set_value(telemetry_enabled)
        self._current_row.subscribe(self._settings.write_setting, "telemetry-bridge-enabled")
        self._current_row.subscribe(lambda *_: self._reload_telemetry_bridge())

        telemetry_port = Adw.EntryRow()
        telemetry_port.set_title("Telemetry bridge UDP port")
        telemetry_port_value = self._read_setting_default("telemetry-bridge-port", DEFAULT_TELEMETRY_PORT)
        telemetry_port.set_text(str(telemetry_port_value))
        telemetry_port.connect("notify::has-focus", lambda widget, _pspec: self._validate_telemetry_port(widget))
        telemetry_port.connect("apply", lambda widget: self._save_telemetry_port(widget))
        self._add_row(telemetry_port)

        # Autostart and background stuff
        hidden = BoxflatSwitchRow("Start hidden")
        hidden.set_value(self._settings.read_setting("autostart-hidden") or 0)
        hidden.subscribe(self._settings.write_setting, "autostart-hidden")
        hidden.set_active(False)

        if self._settings.read_setting("background") == None:
            self._settings.write_setting(1, "background")

        background = BoxflatSwitchRow("Run in the background")
        startup = BoxflatSwitchRow("Run on startup")

        background.subscribe(lambda v: hidden.set_active(v + startup.get_value(), offset=-1))
        startup.subscribe(lambda v: hidden.set_active(v + background.get_value(), offset=-1))

        background.set_value(self._settings.read_setting("background") or 0)
        background.subscribe(self._settings.write_setting, "background")
        background.subscribe(lambda v: self._application.hold() if v else self._application.release())

        startup.subscribe(self._handle_autostart)
        startup.subscribe(self._settings.write_setting, "autostart")
        startup.set_value(self._settings.read_setting("autostart") or 0, mute=False)


        self.add_preferences_group("Background settings")
        self._add_row(background)
        self._add_row(startup)
        self._add_row(hidden)


    def get_brake_valibration_enabled(self) -> int:
        return self._settings.read_setting("brake-calibration-enabled") or 0


    def enable_custom_commands(self):
        self.add_preferences_group("Custom command")
        self._command = Adw.EntryRow()
        self._command.set_title("Command name")

        self._value = Adw.EntryRow()
        self._value.set_title("Value")

        commands_url = self._version.removesuffix("-flatpak")
        #commands_url = "https://raw.githubusercontent.com/Lawstorant/boxflat/refs/heads/main/data/serial.yml"
        commands_url = f"https://raw.githubusercontent.com/Lawstorant/boxflat/refs/tags/{commands_url}/data/serial.yml"
        #commands_url = f"https://github.com/Lawstorant/boxflat/blob/{commands_url}/data/serial.yml"

        read = BoxflatButtonRow("Execute command")
        read.add_button("Read", self._read_custom)
        read.add_button("Write", self._write_custom)
        read.add_button("Database", self.open_url, commands_url)

        self._add_row(self._command)
        self._add_row(self._value)
        self._add_row(read)


    def _read_custom(self, *args):
        out = self._cm.get_setting(self._command.get_text())
        if out == -1:
            out = "Error/Command not found"
        self._value.set_text(str(out))


    def _write_custom(self, *args):
        com = self._command.get_text()
        val = eval(self._value.get_text())
        self._cm.set_setting(val, com)


    def _handle_autostart(self, enabled: int) -> None:
        if environ["BOXFLAT_FLATPAK_EDITION"] == "true":
            self._autostart_flatpak(enabled)
            return

        self._autostart_native(enabled)


    def _autostart_native(self, enabled: bool) -> None:
        autostart_path = expanduser("~/.config/autostart/boxflat.desktop")
        if enabled:
            copy2(join(self._data_path, "autostart.desktop"), autostart_path)
        else:
            try:
                remove(autostart_path)
            except:
                pass


    def _autostart_flatpak(self, enabled: bool) -> None:
        Xdp.Portal().request_background(
            None,
            "Run Boxflat on startup",
            ["boxflat", "--autostart"],
            Xdp.BackgroundFlags.AUTOSTART if enabled else Xdp.BackgroundFlags.NONE,
            None,
            lambda p, t: p.request_background_finish(t)
        )


    def _validate_telemetry_port(self, row: Adw.EntryRow):
        if row.has_focus():
            return

        valid, _ = self._get_valid_telemetry_port(row.get_text())
        row.remove_css_class("error")
        if not valid and row.get_text() != "":
            row.add_css_class("error")


    def _save_telemetry_port(self, row: Adw.EntryRow) -> None:
        valid, result = self._get_valid_telemetry_port(row.get_text())
        if not valid:
            row.add_css_class("error")
            self.show_toast(result, 2)
            return

        row.remove_css_class("error")
        self._settings.write_setting(result, "telemetry-bridge-port")
        self._reload_telemetry_bridge()


    def _get_valid_telemetry_port(self, value: str) -> tuple[bool, int | str]:
        if value == "":
            return False, "Telemetry bridge port must not be empty"

        try:
            port = int(value)
        except ValueError:
            return False, "Telemetry bridge port must be a number"

        if port < 1 or port > 65535:
            return False, "Telemetry bridge port must be in range 1-65535"

        return True, port


    def _reload_telemetry_bridge(self) -> None:
        if not self._application:
            return

        enabled = self._read_setting_default("telemetry-bridge-enabled", DEFAULT_TELEMETRY_ENABLED)
        port = self._read_setting_default("telemetry-bridge-port", DEFAULT_TELEMETRY_PORT)

        if hasattr(self._application, "reload_telemetry_bridge"):
            self._application.reload_telemetry_bridge(enabled, port)


    def _read_setting_default(self, key: str, default):
        value = self._settings.read_setting(key)
        if value is None:
            return default
        return value
