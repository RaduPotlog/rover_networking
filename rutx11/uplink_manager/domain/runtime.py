"""Live uplink state as the router reports it, and what counts as "clients have internet"."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .invariants import Check


@dataclass
class UplinkRuntime:
    iface: str
    up: bool = False
    ipv4: str | None = None
    gateway: str | None = None
    device: str | None = None  # l3 device, e.g. wlan0-3
    ssid: str | None = None  # associated SSID (None when not associated)
    bssid: str | None = None
    signal: int | None = None
    channel: int | None = None
    internet: bool | None = None  # router pings out through the uplink
    masquerade: bool | None = None  # fw3 NAT rule present for the uplink device

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def healthy(self) -> bool:
        return bool(self.up and self.ipv4 and self.internet)


def runtime_checks(rt: UplinkRuntime) -> list[Check]:
    checks = [
        Check('rt_assoc', 'Wi-Fi association',
              'ok' if rt.ssid else 'error',
              f'associated to {rt.ssid!r}' + _extra(
                  f'{rt.signal} dBm' if rt.signal is not None else None,
                  f'channel {rt.channel}' if rt.channel else None) if rt.ssid
              else 'not associated: wrong password, out of range, or the network is down'),
        Check('rt_ip', f'Interface {rt.iface}',
              'ok' if rt.up and rt.ipv4 else 'error',
              f'up, {rt.ipv4}' + (f' via {rt.gateway}' if rt.gateway else '')
              + (f' on {rt.device}' if rt.device else '') if rt.up and rt.ipv4
              else 'no IPv4 address (DHCP failed or not associated)'),
        Check('rt_internet', 'Internet from the router',
              'ok' if rt.internet else 'error',
              'ping through the uplink works' if rt.internet else 'ping through the uplink fails'),
    ]
    if rt.device:
        checks.append(Check(
            'rt_nat', 'NAT for LAN / AP clients',
            'ok' if rt.masquerade else 'error',
            f'MASQUERADE active on {rt.device}' if rt.masquerade
            else f'no MASQUERADE on {rt.device}: LAN and AP clients have no internet'))
    return checks


def _extra(*parts: str | None) -> str:
    shown = [p for p in parts if p]
    return f' ({", ".join(shown)})' if shown else ''
