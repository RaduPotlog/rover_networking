"""In-memory router for `rover-netui --demo` and the tests: uci state from a `uci -X show` dump
plus a tiny model of association / DHCP / NAT. Nothing leaves the process."""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from ..application.ports import RouterError, RouterGateway
from ..domain.invariants import UplinkSettings, resolve_settings, uplink_sta, zone
from ..domain.runtime import UplinkRuntime
from ..domain.uci import UciCmd, parse_uci_show
from ..domain.wifi import WifiNetwork

# SSID → the only key that associates ('' = open). Fixture keys are all '<REDACTED>'.
DEMO_NETWORKS = {'Orange-Tekwill': '<REDACTED>', 'Tekwill': '<REDACTED>',
                 'Field Hotspot': '<REDACTED>', 'Cafe': 'password1', 'Guest': ''}

_DEMO_SCAN = [
    ('Orange-Tekwill', 6, -58, 'psk2', 'WPA2 PSK (CCMP)', 'radio0'),
    ('Tekwill', 36, -63, 'psk2', 'WPA2 PSK (CCMP)', 'radio1'),
    ('Field Hotspot', 44, -71, 'sae-mixed', 'mixed WPA2/WPA3 PSK/SAE (CCMP)', 'radio1'),
    ('Cafe', 1, -77, 'psk2', 'WPA2 PSK (CCMP)', 'radio0'),
    ('Guest', 11, -69, 'none', 'Open', 'radio0'),
    ('Corp', 11, -66, 'unsupported', 'WPA2 802.1X (CCMP)', 'radio0'),
]


def parse_batch(batch: str) -> list[UciCmd]:
    """Inverse of UciCmd.render, enough for the commands the domain emits."""
    cmds = []
    for line in batch.splitlines():
        op, rest = line.split(' ', 1)
        if '=' in rest:
            path, raw = rest.split('=', 1)
            value = shlex.split(raw)[0] if raw else ''
        else:
            path, value = rest, None
        parts = path.split('.')
        cmds.append(UciCmd(op, parts[0], parts[1], parts[2] if len(parts) > 2 else None, value))
    return cmds


class FakeRouter(RouterGateway):
    def __init__(self, fixture: Path, networks: dict[str, str] | None = None,
                 settings: UplinkSettings = UplinkSettings(), delay_s: float = 0.0):
        self.snap = parse_uci_show(Path(fixture).read_text())
        self.st = settings
        self.networks = networks if networks is not None else dict(DEMO_NETWORKS)
        self.delay_s = delay_s  # --demo slows apply/scan down a little so the job log is visible
        self.backups: dict[str, object] = {}
        self.done: set[str] = set()
        self.log: list[str] = []
        self.pending = ''
        self.fail_apply = False

    def show(self, packages):
        snap = self.snap.copy()
        snap.packages = {k: v for k, v in snap.packages.items() if k in packages}
        return snap

    def pending_changes(self, packages):
        return self.pending

    def scan(self):
        time.sleep(self.delay_s / 3)
        known = [WifiNetwork(s, '02:00:00:00:00:%02X' % i, ch, sig, enc, desc, radio)
                 for i, (s, ch, sig, enc, desc, radio) in enumerate(_DEMO_SCAN)]
        extra = [WifiNetwork(s, '02:00:00:00:01:%02X' % i, 6, -60, 'psk2' if k else 'none',
                             'WPA2 PSK' if k else 'Open', 'radio0')
                 for i, (s, k) in enumerate(self.networks.items())
                 if s not in {n.ssid for n in known}]
        return known + extra

    def runtime(self, iface):
        st = resolve_settings(self.snap, self.st)
        sta = uplink_sta(self.snap, st)
        rt = UplinkRuntime(iface)
        if sta is None or sta.get('disabled') == '1' or iface not in sta.words('network'):
            return rt
        ssid, key = sta.get('ssid'), sta.get('key', '')
        if ssid not in self.networks or (self.networks[ssid] and self.networks[ssid] != key):
            return rt
        rt.ssid, rt.up, rt.ipv4, rt.gateway = ssid, True, '10.0.0.5', '10.0.0.1'
        rt.device, rt.internet, rt.signal, rt.channel = 'wlan0-3', True, -60, 6
        z = zone(self.snap, st.uplink_zone)
        rt.masquerade = bool(z and iface in z.words('network') and z.get('masq') == '1')
        return rt

    def begin(self, tx, packages, watchdog_s):
        self.backups[tx] = self.snap.copy()
        self.log.append(f'begin {tx}')

    def apply(self, tx, batch, packages):
        time.sleep(self.delay_s)
        if self.fail_apply:
            raise RouterError('uci: Invalid argument')
        self.snap = self.snap.apply(parse_batch(batch))
        self.log.append(f'apply {tx} {",".join(packages)}')

    def confirm(self, tx):
        self.done.add(tx)
        self.log.append(f'confirm {tx}')

    def rollback(self, tx):
        if tx in self.done:
            return
        self.done.add(tx)
        self.snap = self.backups[tx]
        self.log.append(f'rollback {tx}')
