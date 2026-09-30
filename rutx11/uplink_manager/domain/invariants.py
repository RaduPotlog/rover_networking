"""What a healthy RUTX11 uplink config looks like, and the uci commands that restore it.

The failure this guards against: joining a network through RutOS "Scan > Join" adds a NEW
STA wifi-iface + network interface (wan2, …) that no firewall zone lists. That leaves no
MASQUERADE, so LAN and AP clients lose internet, and sometimes the new interface lands in the lan zone,
which exposes the router's SSH/web UI to the foreign network. Everything is discovered by
content (the STA iface, the zone named in settings); only the logical names come from settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .uci import UciCmd, UciSection, UciSnapshot, set_words, uci_del_list, uci_delete, uci_set


@dataclass(frozen=True)
class UplinkSettings:
    # Logical (uci) network name of the Wi-Fi STA uplink, or 'auto'. RutOS "Scan > Join" names
    # new uplinks WWAN1, wan1, wan2, … so 'auto' finds it in the config (resolve_settings).
    uplink_iface: str = 'auto'
    uplink_zone: str = 'wwan'  # firewall zone that masquerades out of the uplink
    lan_zone: str = 'lan'  # zone of the client networks
    client_nets: tuple[str, ...] = ('lan', 'WWAN')  # wired LAN + the rover AP


@dataclass
class Action:
    """A fix the operator has to choose (adopt vs delete an orphan uplink)."""

    id: str
    label: str
    cmds: list[UciCmd]


@dataclass
class Check:
    id: str
    title: str
    status: str  # ok | warn | error
    detail: str = ''
    fix: list[UciCmd] = field(default_factory=list)  # applied by "Repair"
    actions: list[Action] = field(default_factory=list)  # operator choice, never automatic

    @property
    def ok(self) -> bool:
        return self.status == 'ok'


AUTO = 'auto'


def resolve_settings(snap: UciSnapshot, st: UplinkSettings) -> UplinkSettings:
    """Pin 'auto' to a concrete uplink network for this snapshot.

    Preference: an enabled STA whose network is in the uplink zone, then any enabled STA,
    then one in the zone, then the first. The remaining STAs are reported as extras.
    """
    if st.uplink_iface != AUTO:
        return st
    z = zone(snap, st.uplink_zone)
    zone_nets = set(z.words('network')) if z else set()
    stas = [s for s in snap.sections('wireless', 'wifi-iface') if _is_sta(s) and s.words('network')]
    if not stas:
        return st

    def rank(s: UciSection) -> tuple[bool, bool, bool]:
        in_zone = any(n in zone_nets for n in s.words('network'))
        return (not (_enabled(s) and in_zone), not _enabled(s), not in_zone)

    best = sorted(stas, key=rank)[0]  # stable: file order breaks ties
    nets = best.words('network')
    return replace(st, uplink_iface=next((n for n in nets if n in zone_nets), nets[0]))


def _is_sta(s: UciSection) -> bool:
    return s.type == 'wifi-iface' and s.get('mode') == 'sta'


def _enabled(s: UciSection) -> bool:
    return s.get('disabled', '0') != '1'


def uplink_sta(snap: UciSnapshot, st: UplinkSettings) -> UciSection | None:
    for s in snap.sections('wireless', 'wifi-iface'):
        if _is_sta(s) and st.uplink_iface in s.words('network'):
            return s
    return None


def orphan_stas(snap: UciSnapshot, st: UplinkSettings) -> list[UciSection]:
    return [s for s in snap.sections('wireless', 'wifi-iface')
            if _is_sta(s) and st.uplink_iface not in s.words('network')]


def network_ifaces(snap: UciSnapshot) -> set[str]:
    return {s.name for s in snap.sections('network', 'interface')}


def zone(snap: UciSnapshot, name: str) -> UciSection | None:
    for s in snap.sections('firewall', 'zone'):
        if s.get('name') == name:
            return s
    return None


def _zone_names(snap: UciSnapshot) -> set[str]:
    return {s.get('name') for s in snap.sections('firewall', 'zone')}


def _orphan_networks(snap: UciSnapshot, st: UplinkSettings, orphan: UciSection) -> list[str]:
    """Networks only this orphan STA uses: safe to delete together with it."""
    others = [s for s in snap.sections('wireless', 'wifi-iface') if s.name != orphan.name]
    used = {w for s in others for w in s.words('network')}
    keep = set(st.client_nets) | {st.uplink_iface, 'lan', 'loopback'}
    return [n for n in orphan.words('network') if n not in keep and n not in used]


def _forget_networks(snap: UciSnapshot, nets: list[str]) -> list[UciCmd]:
    """Delete network sections and every zone / mwan3 reference to them."""
    if not nets:
        return []
    cmds: list[UciCmd] = []
    for n in nets:
        if snap.section('network', n) is not None:
            cmds.append(uci_delete('network', n))
    for z in snap.sections('firewall', 'zone'):
        words = z.words('network')
        if any(n in words for n in nets):
            cmds += set_words('firewall', z, 'network', [w for w in words if w not in nets])
    cmds += _mwan3_forget(snap, set(nets))
    return cmds


def _mwan3_forget(snap: UciSnapshot, nets: set[str]) -> list[UciCmd]:
    if not snap.has_package('mwan3'):
        return []
    cmds: list[UciCmd] = []
    dead_members = []
    for s in snap.sections('mwan3', 'member'):
        if s.get('interface') in nets:
            dead_members.append(s.name)
            cmds.append(uci_delete('mwan3', s.name))
    for s in snap.sections('mwan3', 'policy'):
        for m in dead_members:
            if m in s.options.get('use_member', []):
                cmds.append(uci_del_list('mwan3', s.name, 'use_member', m))
    for s in snap.sections('mwan3', 'interface'):
        if s.name in nets:
            cmds.append(uci_delete('mwan3', s.name))
    return cmds


def plan_credentials(sta: UciSection, ssid: str, encryption: str, key: str | None,
                     radio: str | None) -> list[UciCmd]:
    """Rewrite the existing uplink STA in place: same section, same network, same zones."""
    cmds = [uci_set('wireless', sta.name, 'ssid', ssid),
            uci_set('wireless', sta.name, 'encryption', encryption)]
    if key is not None and encryption not in ('none', 'owe'):
        cmds.append(uci_set('wireless', sta.name, 'key', key))
    elif 'key' in sta.options:
        cmds.append(uci_delete('wireless', sta.name, 'key'))
    if 'bssid' in sta.options:  # pinned to the old AP otherwise
        cmds.append(uci_delete('wireless', sta.name, 'bssid'))
    if radio and sta.get('device') != radio:
        cmds.append(uci_set('wireless', sta.name, 'device', radio))
    if not _enabled(sta):
        cmds.append(uci_set('wireless', sta.name, 'disabled', '0'))
    return cmds


def plan_delete_orphan(snap: UciSnapshot, st: UplinkSettings, orphan: UciSection) -> list[UciCmd]:
    return [uci_delete('wireless', orphan.name)] + _forget_networks(
        snap, _orphan_networks(snap, st, orphan))


def plan_adopt_orphan(snap: UciSnapshot, st: UplinkSettings, orphan: UciSection) -> list[UciCmd]:
    """Move the orphan's credentials into the uplink STA, then delete the orphan."""
    sta = uplink_sta(snap, st)
    if sta is None:
        # No canonical STA left: re-point the orphan itself at the uplink network.
        return [uci_set('wireless', orphan.name, 'network', st.uplink_iface)] + _forget_networks(
            snap, _orphan_networks(snap, st, orphan))
    cmds = plan_credentials(sta, orphan.get('ssid', ''), orphan.get('encryption', 'none'),
                            orphan.get('key'), orphan.get('device'))
    return cmds + plan_delete_orphan(snap, st, orphan)


def _zone_desired(snap: UciSnapshot, st: UplinkSettings, z: UciSection) -> list[str]:
    ifaces = network_ifaces(snap)
    orphan_nets = {n for o in orphan_stas(snap, st) for n in o.words('network')}
    cur = z.words('network')
    name = z.get('name')
    # Dangling names are only pruned from the two zones this tool owns; RutOS zones such as
    # `wan` may list modem interfaces that come and go, so those are left alone.
    ours = name in (st.uplink_zone, st.lan_zone)
    keep = [w for w in cur if w in ifaces or not ours]
    if name == st.uplink_zone:
        keep = [w for w in keep if w not in st.client_nets]
        if st.uplink_iface in ifaces and st.uplink_iface not in keep:
            keep.append(st.uplink_iface)
    else:
        keep = [w for w in keep if w != st.uplink_iface and w not in orphan_nets]
        if name == st.lan_zone:
            keep += [n for n in st.client_nets if n in ifaces and n not in keep]
        else:
            keep = [w for w in keep if w not in st.client_nets]
    return keep


def audit_config(snap: UciSnapshot, st: UplinkSettings) -> list[Check]:
    checks: list[Check] = []
    ifaces = network_ifaces(snap)

    # 1. Exactly one STA uplink, on the uplink network.
    sta = uplink_sta(snap, st)
    orphans = orphan_stas(snap, st)
    if sta is None and not orphans:
        where = 'at all' if st.uplink_iface == AUTO else f'on network {st.uplink_iface!r}'
        checks.append(Check('uplink_sta', 'Wi-Fi uplink interface', 'error',
                            f'No Wi-Fi client (STA) interface {where}. Join a network once in '
                            'RutOS, then use this page to switch networks.'))
    elif sta is not None:
        fix = [] if _enabled(sta) or any(_enabled(o) for o in orphans) else [
            uci_set('wireless', sta.name, 'disabled', '0')]
        checks.append(Check(
            'uplink_sta', 'Wi-Fi uplink interface', 'ok' if _enabled(sta) else 'error',
            f'{sta.name} on {sta.get("device")} → {st.uplink_iface}, SSID {sta.get("ssid")!r}'
            + ('' if _enabled(sta) else ' (disabled)'), fix=fix))
    if sta is not None and sta.get('device'):
        radio = snap.section('wireless', sta.get('device'))
        if radio is not None and radio.get('disabled') == '1':
            checks.append(Check('uplink_radio', f'Radio {radio.name}', 'error',
                                f'the uplink is on {radio.name}, which is disabled',
                                fix=[uci_set('wireless', radio.name, 'disabled', '0')]))
    if st.uplink_iface != AUTO and st.uplink_iface not in ifaces:
        checks.append(Check('uplink_network', f'Network interface {st.uplink_iface}', 'error',
                            f'network.{st.uplink_iface} does not exist'))

    for o in orphans:
        nets = ' '.join(o.words('network')) or '(none)'
        actions = [Action(f'adopt:{o.name}',
                          f'Use {o.get("ssid")!r} as the uplink (move it into {st.uplink_iface})',
                          plan_adopt_orphan(snap, st, o)),
                   Action(f'delete:{o.name}', f'Delete this extra interface',
                          plan_delete_orphan(snap, st, o))]
        checks.append(Check(
            f'orphan:{o.name}', 'Extra Wi-Fi client interface', 'error',
            f'{o.name} (SSID {o.get("ssid")!r}, network {nets}, '
            f'{"enabled" if _enabled(o) else "disabled"}) is a second Wi-Fi client next to the '
            f'uplink {st.uplink_iface}; RutOS "Scan > Join" leaves these behind. Only one can be '
            'the uplink: switch to its network or delete it.',
            actions=actions))

    # 2-4. Zones: uplink zone lists the uplink, client nets live in the lan zone, nothing dangles.
    uz = zone(snap, st.uplink_zone)
    lz = zone(snap, st.lan_zone)
    if uz is None:
        checks.append(Check('zone_uplink', f'Firewall zone {st.uplink_zone}', 'error',
                            'zone missing; set UPLINK_ZONE or create it in RutOS'))
    if lz is None:
        checks.append(Check('zone_lan', f'Firewall zone {st.lan_zone}', 'error',
                            'zone missing; set LAN_ZONE'))
    for z in snap.sections('firewall', 'zone'):
        name = z.get('name')
        cur = z.words('network')
        want = _zone_desired(snap, st, z)
        ok = cur == want
        detail = f'network={" ".join(cur) or "(empty)"}'
        if not ok:
            detail += f' → {" ".join(want) or "(empty)"}'
        checks.append(Check(f'zone_net:{name}', f'Zone {name}: interfaces',
                            'ok' if ok else 'error', detail,
                            fix=[] if ok else set_words('firewall', z, 'network', want)))

    if uz is not None:
        fix = []
        if uz.get('masq') != '1':
            fix.append(uci_set('firewall', uz.name, 'masq', '1'))
        if uz.get('mtu_fix') != '1':
            fix.append(uci_set('firewall', uz.name, 'mtu_fix', '1'))
        if (uz.get('input') or '').upper() == 'ACCEPT':
            fix.append(uci_set('firewall', uz.name, 'input', 'REJECT'))
        checks.append(Check('zone_uplink_opts', f'Zone {st.uplink_zone}: NAT',
                            'ok' if not fix else 'error',
                            f'masq={uz.get("masq")} mtu_fix={uz.get("mtu_fix")} input={uz.get("input")}',
                            fix=fix))
    if lz is not None:
        bad = lz.get('masq') == '1'
        checks.append(Check('zone_lan_opts', f'Zone {st.lan_zone}: no NAT inside the LAN',
                            'error' if bad else 'ok', f'masq={lz.get("masq")}',
                            fix=[uci_set('firewall', lz.name, 'masq', '0')] if bad else []))

    # 5. Forwarding lan → uplink, and no forwarding to zones that no longer exist.
    zones = _zone_names(snap)
    fwds = snap.sections('firewall', 'forwarding')
    if uz is not None and lz is not None:
        match = [f for f in fwds if f.get('src') == st.lan_zone and f.get('dest') == st.uplink_zone]
        if not match:
            name = 'netui_lan_uplink'
            fix = [uci_set('firewall', name, None, 'forwarding'),
                   uci_set('firewall', name, 'src', st.lan_zone),
                   uci_set('firewall', name, 'dest', st.uplink_zone)]
            checks.append(Check('forwarding', f'Forwarding {st.lan_zone} → {st.uplink_zone}',
                                'error', 'missing: clients cannot reach the internet', fix=fix))
        elif not any(_enabled_fw(f) for f in match):
            checks.append(Check('forwarding', f'Forwarding {st.lan_zone} → {st.uplink_zone}',
                                'error', 'present but disabled',
                                fix=[uci_set('firewall', match[0].name, 'enabled', '1')]))
        else:
            checks.append(Check('forwarding', f'Forwarding {st.lan_zone} → {st.uplink_zone}', 'ok'))
    for f in fwds:
        if f.get('src') not in zones or f.get('dest') not in zones:
            checks.append(Check(f'fwd_dangling:{f.name}', 'Stale forwarding', 'error',
                                f'{f.get("src")} → {f.get("dest")}: zone does not exist',
                                fix=[uci_delete('firewall', f.name)]))
    for r in snap.sections('firewall', 'rule') + snap.sections('firewall', 'redirect'):
        refs = [r.get(k) for k in ('src', 'dest') if r.get(k) not in (None, '*')]
        missing = [z for z in refs if z not in zones]
        if missing:
            label = r.get('name') or r.name
            checks.append(Check(f'rule_dangling:{r.name}', f'Firewall {r.type} {label!r}', 'warn',
                                f'refers to missing zone(s) {", ".join(missing)}; fw3 skips it. '
                                'Fix it in RutOS (not changed automatically).'))

    # 6. mwan3 failover leftovers.
    if snap.has_package('mwan3'):
        dead = {s.name for s in snap.sections('mwan3', 'interface') if s.name not in ifaces}
        dead |= {s.get('interface') for s in snap.sections('mwan3', 'member')
                 if s.get('interface') not in ifaces}
        dead.discard(None)
        if dead:
            # RutOS ships entries for wan / mob1s*a1 even where those interfaces don't exist;
            # report only. Entries of an extra STA go with Adopt/Delete (_forget_networks).
            checks.append(Check('mwan3_dangling', 'Failover (mwan3) entries', 'warn',
                                f'refer to interface(s) that do not exist: {", ".join(sorted(dead))} '
                                '(harmless while failover for them is disabled)'))
        mw_ifaces = {s.name for s in snap.sections('mwan3', 'interface')}
        if mw_ifaces and st.uplink_iface not in mw_ifaces:
            checks.append(Check('mwan3_uplink', 'Failover (mwan3)', 'warn',
                                f'{st.uplink_iface} is not an mwan3 interface; if RutOS failover '
                                'is in use, add it under Network > Failover.'))

    # DHCP pools on the client networks (report only).
    for n in st.client_nets:
        pools = [d for d in snap.sections('dhcp', 'dhcp') if d.get('interface') == n]
        if snap.has_package('dhcp') and not any(d.get('ignore') != '1' for d in pools):
            checks.append(Check(f'dhcp:{n}', f'DHCP on {n}', 'warn',
                                'no active DHCP pool; new clients get no address'))
    return checks


def _enabled_fw(s: UciSection) -> bool:
    return s.get('enabled', '1') != '0'


def repair_plan(checks: list[Check]) -> list[UciCmd]:
    return [c for chk in checks for c in chk.fix]


def plan_switch(snap: UciSnapshot, st: UplinkSettings, ssid: str, encryption: str,
                key: str | None, radio: str | None) -> list[UciCmd]:
    """In-place uplink change + delete every orphan STA + all automatic config repairs."""
    sta = uplink_sta(snap, st)
    if sta is None:
        raise ValueError(f'no Wi-Fi uplink interface on network {st.uplink_iface!r}')
    cmds = plan_credentials(sta, ssid, encryption, key, radio)
    after = snap.apply(cmds)
    for o in orphan_stas(after, st):  # one at a time: each rewrites the zones it touches
        step = plan_delete_orphan(after, st, o)
        cmds += step
        after = after.apply(step)
    return cmds + repair_plan(audit_config(after, st))


def plan_action(snap: UciSnapshot, st: UplinkSettings, action_id: str) -> list[UciCmd]:
    """Recompute an orphan action on a fresh snapshot, followed by the repairs it leaves."""
    kind, _, name = action_id.partition(':')
    orphan = snap.section('wireless', name)
    if orphan is None or name not in {o.name for o in orphan_stas(snap, st)}:
        raise ValueError(f'no extra Wi-Fi client interface {name!r}')
    if kind == 'adopt':
        cmds = plan_adopt_orphan(snap, st, orphan)
    elif kind == 'delete':
        cmds = plan_delete_orphan(snap, st, orphan)
    else:
        raise ValueError(f'unknown action {action_id!r}')
    return cmds + repair_plan(audit_config(snap.apply(cmds), st))
