# Copyright 2026 Mechatronics Academy
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Wi-Fi scan results, encryption mapping and credential validation (pure Python)."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# OpenWrt `wireless.<iface>.encryption` values this tool can join. Enterprise (802.1X) and WEP
# need more than an SSID + key and are reported as unsupported.
NEEDS_KEY = frozenset({'psk', 'psk2', 'psk-mixed', 'sae', 'sae-mixed'})
NO_KEY = frozenset({'none', 'owe'})
SUPPORTED = NEEDS_KEY | NO_KEY


@dataclass
class WifiNetwork:
    ssid: str
    bssid: str
    channel: int | None
    signal: int | None  # dBm
    encryption: str  # OpenWrt value, or 'unsupported'
    security: str  # human-readable, as the scan reported it
    radio: str  # wifi-device it was seen on (radio0 / radio1)
    aps: int = 1  # access points seen with this SSID + security (merge_scans)

    @property
    def band(self) -> str:
        if self.channel is None:
            return '?'
        return '2.4 GHz' if self.channel <= 14 else '5 GHz'

    def to_dict(self) -> dict:
        d = asdict(self)
        d['band'] = self.band
        d['needs_key'] = self.encryption in NEEDS_KEY
        return d


def encryption_from_ubus(enc: dict | None) -> tuple[str, str]:
    """Map `ubus call iwinfo scan` encryption object → (openwrt value, description)."""
    if not enc or not enc.get('enabled'):
        return 'none', 'Open'
    auth = [a.lower() for a in enc.get('authentication', [])]
    wpa = enc.get('wpa', [])
    desc = enc.get('description') or ('WPA%s %s' % ('/'.join(map(str, wpa)), '/'.join(auth).upper()))
    if enc.get('wep'):
        return 'unsupported', desc or 'WEP'
    if any(a in ('802.1x', 'eap') for a in auth):
        return 'unsupported', desc
    if 'owe' in auth:
        return 'owe', desc
    if 'sae' in auth:
        return ('sae-mixed' if 'psk' in auth else 'sae'), desc
    if 'psk' in auth:
        if 1 in wpa and 2 in wpa:
            return 'psk-mixed', desc
        if wpa == [1]:
            return 'psk', desc
        return 'psk2', desc
    return 'unsupported', desc


def encryption_from_text(text: str) -> str:
    """Map an `iwinfo <dev> scan` "Encryption:" string to an OpenWrt value."""
    t = text.strip().lower()
    if t in ('none', 'off', 'open', ''):
        return 'none'
    if 'wep' in t or '802.1x' in t or 'eap' in t:
        return 'unsupported'
    if 'owe' in t:
        return 'owe'
    if 'sae' in t:
        return 'sae-mixed' if 'psk' in t else 'sae'
    if 'psk' in t:
        if 'wpa/wpa2' in t or ('mixed' in t and 'wpa3' not in t):
            return 'psk-mixed'
        if 'wpa2' in t or 'wpa3' in t:
            return 'psk2'
        return 'psk'
    return 'unsupported'


def parse_ubus_scan(results: list[dict], radio: str) -> list[WifiNetwork]:
    nets = []
    for r in results:
        enc, desc = encryption_from_ubus(r.get('encryption'))
        nets.append(WifiNetwork(
            ssid=r.get('ssid', ''), bssid=(r.get('bssid') or '').upper(),
            channel=r.get('channel'), signal=r.get('signal'),
            encryption=enc, security=desc, radio=radio))
    return nets


_CELL = re.compile(r'Cell \d+ - Address:\s*([0-9A-Fa-f:]{17})')


def parse_iwinfo_scan(text: str, radio: str) -> list[WifiNetwork]:
    """Parse the plain `iwinfo <dev> scan` output (fallback when ubus iwinfo is unavailable)."""
    nets: list[WifiNetwork] = []
    cur: dict | None = None

    def flush():
        if cur is not None:
            nets.append(WifiNetwork(
                ssid=cur.get('ssid', ''), bssid=cur['bssid'], channel=cur.get('channel'),
                signal=cur.get('signal'), encryption=encryption_from_text(cur.get('enc', '')),
                security=cur.get('enc', ''), radio=radio))

    for line in text.splitlines():
        m = _CELL.search(line)
        if m:
            flush()
            cur = {'bssid': m.group(1).upper()}
            continue
        if cur is None:
            continue
        if (m := re.search(r'ESSID:\s*"(.*)"', line)):
            cur['ssid'] = m.group(1)
        if (m := re.search(r'Channel:\s*(\d+)', line)):
            cur['channel'] = int(m.group(1))
        if (m := re.search(r'Signal:\s*(-?\d+)\s*dBm', line)):
            cur['signal'] = int(m.group(1))
        if (m := re.search(r'Encryption:\s*(.*)$', line)):
            cur['enc'] = m.group(1).strip()
    flush()
    return nets


def merge_scans(nets: list[WifiNetwork]) -> list[WifiNetwork]:
    """One row per network and band (SSID + security + radio), strongest AP first; hidden
    SSIDs dropped.

    Campus networks show up once per access point; the uplink is never pinned to a BSSID,
    so the router roams to the best AP of the chosen band itself.
    """
    best: dict[tuple[str, str, str], WifiNetwork] = {}
    count: dict[tuple[str, str, str], int] = {}
    for n in nets:
        if not n.ssid:
            continue
        k = (n.ssid, n.encryption, n.radio)
        count[k] = count.get(k, 0) + 1
        if k not in best or (n.signal or -999) > (best[k].signal or -999):
            best[k] = n
    for k, n in best.items():
        n.aps = count[k]
    return sorted(best.values(), key=lambda n: -(n.signal if n.signal is not None else -999))


class CredentialError(ValueError):
    pass


def validate_credentials(ssid: str, encryption: str, key: str | None) -> None:
    """Reject what would break the uci batch or hostapd/wpa_supplicant config."""
    if not ssid or len(ssid.encode()) > 32:
        raise CredentialError('SSID must be 1-32 bytes')
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise CredentialError('SSID contains control characters')
    if encryption not in SUPPORTED:
        raise CredentialError(f'encryption {encryption!r} is not supported (WEP / 802.1X / enterprise)')
    if encryption in NEEDS_KEY:
        if key is None:
            raise CredentialError('this network needs a password')
        if encryption.startswith('sae') and encryption != 'sae-mixed':
            ok = 1 <= len(key) <= 128
        else:
            ok = 8 <= len(key) <= 63 or re.fullmatch(r'[0-9A-Fa-f]{64}', key) is not None
        if not ok:
            raise CredentialError('WPA password must be 8-63 characters (or 64 hex digits)')
        if any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise CredentialError('password must be printable ASCII')
