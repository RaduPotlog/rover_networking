from pathlib import Path

import pytest

from uplink_manager.domain.invariants import (
    UplinkSettings, audit_config, plan_action, plan_switch, repair_plan, uplink_sta)
from uplink_manager.domain.uci import parse_uci_show, quote, render_batch, uci_set
from uplink_manager.domain.wifi import (
    CredentialError, encryption_from_ubus, merge_scans, parse_iwinfo_scan, validate_credentials)

FIX = Path(__file__).parent / 'fixtures'
ST = UplinkSettings(uplink_iface='wan1')  # fixed name: these tests exercise the orphan paths


def load(name):
    return parse_uci_show((FIX / name).read_text())


def errors(checks):
    return [c for c in checks if c.status == 'error']


def test_parse_option_list_and_quotes():
    snap = parse_uci_show("firewall.2=zone\nfirewall.2.network='lan' 'WWAN'\n"
                          "wireless.x=wifi-iface\nwireless.x.ssid='it'\\''s here'\n")
    assert snap.section('firewall', '2').words('network') == ['lan', 'WWAN']
    assert snap.section('wireless', 'x').get('ssid') == "it's here"


def test_quote_round_trip():
    value = "a'b \"c\" $x"
    snap = parse_uci_show(f'wireless.x=wifi-iface\nwireless.x.key={quote(value)}\n')
    assert snap.section('wireless', 'x').get('key') == value


def test_redaction():
    line = render_batch([uci_set('wireless', 'x', 'key', 'secret123')], redact=True)
    assert 'secret123' not in line


def test_healthy_config_is_clean():
    checks = audit_config(load('healthy.uci'), ST)
    assert errors(checks) == []
    assert repair_plan(checks) == []


def test_broken_config_detects_every_problem():
    ids = {c.id for c in errors(audit_config(load('after_rutos_join.uci'), ST))}
    assert {'uplink_sta', 'orphan:cfg0c4f2a', 'zone_net:lan', 'zone_net:wwan', 'forwarding',
            'fwd_dangling:cfg11aa00'} <= ids


def test_repair_converges_and_never_uses_add_list_on_zones():
    snap = load('after_rutos_join.uci')
    plan = repair_plan(audit_config(snap, ST))
    assert not any(c.op in ('add_list', 'del_list') and c.pkg == 'firewall' for c in plan)
    after = snap.apply(plan)
    remaining = {c.id for c in errors(audit_config(after, ST))}
    # Only the operator's choice is left: which Wi-Fi to keep.
    assert remaining == {'uplink_sta', 'orphan:cfg0c4f2a'}
    assert after.section('firewall', '6').get('network') == 'wan1'
    assert after.section('firewall', '2').get('network') == 'lan WWAN'
    # Stock-looking failover leftovers are reported, never deleted by Repair.
    assert 'gone_m1' in after.section('mwan3', 'mwan_default').options['use_member']


def test_adopt_orphan_moves_credentials_into_wan1():
    snap = load('after_rutos_join.uci')
    after = snap.apply(plan_action(snap, ST, 'adopt:cfg0c4f2a'))
    sta = uplink_sta(after, ST)
    assert sta.name == 'cfg093579'
    assert sta.get('ssid') == 'Field Hotspot' and sta.get('encryption') == 'sae-mixed'
    assert sta.get('device') == 'radio1' and sta.get('disabled', '0') == '0'
    assert 'bssid' not in sta.options
    assert after.section('wireless', 'cfg0c4f2a') is None
    assert after.section('network', 'wan2') is None
    assert after.section('mwan3', 'wan2') is None
    assert errors(audit_config(after, ST)) == []


def test_delete_orphan_keeps_old_uplink():
    snap = load('after_rutos_join.uci')
    after = snap.apply(plan_action(snap, ST, 'delete:cfg0c4f2a'))
    assert uplink_sta(after, ST).get('ssid') == 'Orange-Tekwill'
    assert errors(audit_config(after, ST)) == []


def test_switch_in_place_keeps_section_and_zones():
    snap = load('healthy.uci')
    cmds = plan_switch(snap, ST, 'Cafe', 'psk2', 'password1', 'radio0')
    after = snap.apply(cmds)
    sta = uplink_sta(after, ST)
    assert sta.name == 'cfg093579' and sta.get('network') == 'wan1'
    assert sta.get('ssid') == 'Cafe' and sta.get('key') == 'password1'
    assert 'bssid' not in sta.options
    assert {c.pkg for c in cmds} == {'wireless'}  # firewall untouched on a healthy router
    assert errors(audit_config(after, ST)) == []


def test_switch_on_broken_router_repairs_everything():
    snap = load('after_rutos_join.uci')
    after = snap.apply(plan_switch(snap, ST, 'Open Net', 'none', None, 'radio0'))
    assert errors(audit_config(after, ST)) == []
    assert 'key' not in uplink_sta(after, ST).options


def test_iwinfo_text_scan():
    nets = merge_scans(parse_iwinfo_scan((FIX / 'iwinfo_scan.txt').read_text(), 'radio0'))
    by = {n.ssid: n for n in nets}
    assert by['Orange-Tekwill'].encryption == 'psk2' and by['Orange-Tekwill'].channel == 6
    assert by['Field Hotspot'].encryption == 'sae-mixed' and by['Field Hotspot'].band == '5 GHz'
    assert by['Corp'].encryption == 'unsupported'
    assert nets[0].ssid == 'Orange-Tekwill'  # strongest first


@pytest.mark.parametrize('enc,expected', [
    (None, 'none'),
    ({'enabled': True, 'wpa': [2], 'authentication': ['psk']}, 'psk2'),
    ({'enabled': True, 'wpa': [1, 2], 'authentication': ['psk']}, 'psk-mixed'),
    ({'enabled': True, 'wpa': [3], 'authentication': ['sae']}, 'sae'),
    ({'enabled': True, 'wpa': [2, 3], 'authentication': ['psk', 'sae']}, 'sae-mixed'),
    ({'enabled': True, 'wpa': [2], 'authentication': ['802.1x']}, 'unsupported'),
])
def test_ubus_encryption(enc, expected):
    assert encryption_from_ubus(enc)[0] == expected


@pytest.mark.parametrize('ssid,enc,key', [
    ('', 'psk2', 'password1'),
    ('x' * 33, 'psk2', 'password1'),
    ('ok', 'psk2', 'short'),
    ('ok', 'psk2', None),
    ('ok', 'psk2', 'pass\nword1'),
    ('ok', 'unsupported', 'password1'),
])
def test_bad_credentials(ssid, enc, key):
    with pytest.raises(CredentialError):
        validate_credentials(ssid, enc, key)


def test_good_credentials():
    validate_credentials('Orange-Tekwill', 'psk2', "it's a pass")
    validate_credentials('Open', 'none', None)
    validate_credentials('Hex', 'psk2', 'a' * 64)


def test_scan_merges_access_points_of_one_network():
    from uplink_manager.domain.wifi import WifiNetwork
    raw = [WifiNetwork('Tekwill', f'AA:00:00:00:00:0{i}', ch, sig, 'psk2', 'WPA2 PSK', 'radio0')
           for i, (ch, sig) in enumerate([(1, -55), (11, -62), (1, -68)])]
    raw.append(WifiNetwork('Orange-Tekwill', 'BB:00:00:00:00:01', 2, -64, 'none', 'Open', 'radio0'))
    nets = merge_scans(raw)
    assert [(n.ssid, n.aps, n.signal, n.channel) for n in nets] == [
        ('Tekwill', 3, -55, 1), ('Orange-Tekwill', 1, -64, 2)]
