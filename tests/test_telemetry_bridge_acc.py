import unittest

from boxflat.telemetry_bridge import ACC_PHYSICS_RPM_OFFSET, ACC_STATIC_MAX_RPM_OFFSET, TelemetryBridge


class TestTelemetryBridgeACC(unittest.TestCase):
    def test_read_acc_rpm_data_reads_expected_offsets(self):
        bridge = TelemetryBridge.__new__(TelemetryBridge)

        physics = bytearray(1024)
        static = bytearray(1024)
        physics[ACC_PHYSICS_RPM_OFFSET : ACC_PHYSICS_RPM_OFFSET + 4] = int(4567).to_bytes(4, "little", signed=True)
        static[ACC_STATIC_MAX_RPM_OFFSET : ACC_STATIC_MAX_RPM_OFFSET + 4] = int(9123).to_bytes(4, "little", signed=True)

        rpm_data = TelemetryBridge._read_acc_rpm_data(bridge, {"physics": physics, "static": static})

        self.assertEqual(rpm_data, (4567, 9123))


if __name__ == "__main__":
    unittest.main()
