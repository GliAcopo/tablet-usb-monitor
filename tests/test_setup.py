"""Pure parts of scripts/setup.py: USB classification from a fake sysfs, ADB
state parsing (no serial leaves the function), the installer button finder
and the suggested start line."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tabs9_setup', REPO_ROOT / 'scripts/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def fake_usb(root, name, vendor, product, interfaces, device_class='00', speed='480'):
    d = root / name
    d.mkdir()
    (d / 'idVendor').write_text(vendor + '\n')
    (d / 'idProduct').write_text(product + '\n')
    (d / 'bDeviceClass').write_text(device_class + '\n')
    (d / 'speed').write_text(speed + '\n')
    (d / 'busnum').write_text('3\n')
    (d / 'devnum').write_text('12\n')
    (d / 'product').write_text('Thing\n')
    for i, (cls, sub, proto) in enumerate(interfaces):
        iface = d / f'{name}:1.{i}'
        iface.mkdir()
        (iface / 'bInterfaceClass').write_text(cls + '\n')
        (iface / 'bInterfaceSubClass').write_text(sub + '\n')
        (iface / 'bInterfaceProtocol').write_text(proto + '\n')


class UsbClassification(unittest.TestCase):
    def test_adb_interface_and_known_vendor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_usb(root, '3-2', '12d1', '107e', [('ff', 'ff', '00'), ('08', '06', '50'), ('ff', '42', '01')])
            fake_usb(root, '3-3', '046d', 'c53f', [('03', '01', '01')])          # a mouse receiver
            fake_usb(root, '3-4', '04e8', '6860', [('06', '01', '01')])          # Samsung, MTP only
            fake_usb(root, 'usb3', '1d6b', '0003', [], device_class='09')        # root hub
            devices = {d['product']: d for d in setup.usb_devices(root)}
        self.assertEqual(set(devices), {'107e', 'c53f', '6860'})
        self.assertTrue(devices['107e']['adb'] and devices['107e']['android'] and devices['107e']['mtp'])
        self.assertFalse(devices['c53f']['android'])
        self.assertTrue(devices['6860']['android'] and not devices['6860']['adb'])


class AdbStates(unittest.TestCase):
    def test_parses_states_without_serials(self):
        output = ('List of devices attached\n'
                  'TGPYD22718200258       unauthorized usb:3-2 transport_id:1\n'
                  'R52X1234ABC            device usb:3-4 product:gts9u model:SM_X916B device:gts9u transport_id:2\n')
        states = setup.adb_states(output)
        self.assertEqual([s[0] for s in states], ['unauthorized', 'device'])
        self.assertNotIn('TGPYD22718200258', repr(states))
        self.assertNotIn('R52X1234ABC', repr(states))
        self.assertIn('model:SM_X916B', states[1][1])

    def test_empty(self):
        self.assertEqual(setup.adb_states('List of devices attached\n\n'), [])


class InstallerButton(unittest.TestCase):
    DUMP = ('<?xml version="1.0"?><hierarchy>'
            '<node text="Avviso" resource-id="android:id/alertTitle" class="android.widget.TextView" '
            'package="com.android.packageinstaller" bounds="[417,692][987,762]" />'
            '<node text="ANNULLA" resource-id="android:id/button2" class="android.widget.Button" '
            'package="com.android.packageinstaller" bounds="[401,1068][693,1148]" />'
            '<node text="CONTINUA" resource-id="android:id/button1" class="android.widget.Button" '
            'package="com.android.packageinstaller" bounds="[711,1068][1003,1148]" />'
            '</hierarchy>')

    def test_finds_positive_button_centre(self):
        self.assertEqual(setup.installer_confirm_button(self.DUMP), ('CONTINUA', 857, 1108))

    def test_ignores_other_apps_buttons(self):
        dump = self.DUMP.replace('package="com.android.packageinstaller" bounds="[711', 'package="com.bank.app" bounds="[711')
        self.assertIsNone(setup.installer_confirm_button(dump))

    def test_empty_dump(self):
        self.assertIsNone(setup.installer_confirm_button(''))


class SuggestedCommand(unittest.TestCase):
    def test_eink_huawei(self):
        facts = {'manufacturer': 'HUAWEI', 'panel': (1872, 1404), 'refresh': 40.0}
        self.assertEqual(setup.suggested_command(facts),
                         './tabs9 start --profile light --resolution 1872x1404 --pen-button off')

    def test_samsung_120hz(self):
        facts = {'manufacturer': 'samsung', 'panel': (2960, 1848), 'refresh': 120.0}
        self.assertEqual(setup.suggested_command(facts),
                         './tabs9 start --profile balanced --resolution 2960x1848')

    def test_nothing_known(self):
        self.assertEqual(setup.suggested_command({}), './tabs9 start --profile balanced --pen-button off')


if __name__ == '__main__':
    unittest.main()
