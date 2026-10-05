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

"""UPLINK_IFACE=auto on the real RUTX11 layout (fixtures rebuilt from the 2026-09-28 backup)."""

from pathlib import Path

from uplink_manager.domain.invariants import (
    UplinkSettings, audit_config, plan_action, plan_switch, repair_plan, resolve_settings,
    uplink_sta, zone)
from uplink_manager.domain.uci import parse_uci_show

FIX = Path(__file__).parent / 'fixtures'
AUTO = UplinkSettings()


def load(name):
    return parse_uci_show((FIX / name).read_text())


def errors(snap, st):
    return {c.id for c in audit_config(snap, st) if c.status == 'error'}


def test_default_is_auto():
    assert AUTO.uplink_iface == 'auto'


def test_real_config_resolves_to_wwan1_and_is_clean():
    snap = load('rutos_20260928.uci')
    st = resolve_settings(snap, AUTO)
    assert st.uplink_iface == 'WWAN1'
    assert errors(snap, st) == set()
    assert repair_plan(audit_config(snap, st)) == []
    # RutOS' stock failover entries for wan / mob1s1a1 are a warning, not something to delete.
    warns = {c.id for c in audit_config(snap, st) if c.status == 'warn'}
    assert 'mwan3_dangling' in warns


def test_switch_keeps_the_rutos_section_names():
    snap = load('rutos_20260928.uci')
    st = resolve_settings(snap, AUTO)
    after = snap.apply(plan_switch(snap, st, 'Orange-Tekwill', 'psk2', 'password1', 'radio0'))
    sta = uplink_sta(after, resolve_settings(after, AUTO))
    assert sta.name == '2' and sta.get('network') == 'WWAN1' and sta.get('device') == 'radio0'
    assert zone(after, 'wwan').get('network') == 'WWAN1'
    assert errors(after, resolve_settings(after, AUTO)) == set()


def test_rutos_join_picks_the_enabled_new_uplink_and_repairs_nat():
    snap = load('rutos_join_wan1.uci')
    st = resolve_settings(snap, AUTO)
    assert st.uplink_iface == 'wan1'  # enabled beats "already in the zone but disabled"
    errs = errors(snap, st)
    assert {'zone_net:wwan', 'zone_net:lan', 'orphan:2'} <= errs

    after = snap.apply(repair_plan(audit_config(snap, st)))
    st2 = resolve_settings(after, AUTO)
    assert st2.uplink_iface == 'wan1'
    assert zone(after, 'wwan').words('network') == ['WWAN1', 'wan1']
    assert zone(after, 'lan').get('network') == 'lan WWAN'  # no more exposure to the uplink
    assert errors(after, st2) == {'orphan:2'}  # the old STA: operator's choice

    final = after.apply(plan_action(after, st2, 'delete:2'))
    st3 = resolve_settings(final, AUTO)
    assert errors(final, st3) == set()
    assert final.section('network', 'WWAN1') is None
    assert final.section('mwan3', 'WWAN1_member_mwan') is None
    assert 'WWAN1_member_mwan' not in final.section('mwan3', 'mwan_default').options['use_member']
    assert zone(final, 'wwan').get('network') == 'wan1'


def test_rutos_join_adopt_old_uplink_back():
    snap = load('rutos_join_wan1.uci')
    st = resolve_settings(snap, AUTO)
    final = snap.apply(plan_action(snap, st, 'adopt:2'))
    st2 = resolve_settings(final, AUTO)
    sta = uplink_sta(final, st2)
    assert st2.uplink_iface == 'wan1' and sta.get('ssid') == 'Tekwill'
    assert sta.get('device') == 'radio1'
    assert errors(final, st2) == set()


def test_no_sta_at_all():
    snap = load('rutos_20260928.uci')
    snap.packages['wireless'].pop('2')
    st = resolve_settings(snap, AUTO)
    assert st.uplink_iface == 'auto'
    checks = {c.id: c for c in audit_config(snap, st)}
    assert checks['uplink_sta'].status == 'error' and 'uplink_network' not in checks


def test_live_router_20260929_is_clean():
    """The real config as captured read-only on 2026-09-29 (after the manual fixes)."""
    snap = load('live_20260929.uci')
    st = resolve_settings(snap, AUTO)
    assert st.uplink_iface == 'wan1'
    assert errors(snap, st) == set()
    assert repair_plan(audit_config(snap, st)) == []
    sta = uplink_sta(snap, st)
    after = snap.apply(plan_switch(snap, st, 'Tekwill', 'psk2', 'password1', 'radio0'))
    assert {c.pkg for c in plan_switch(snap, st, 'Tekwill', 'psk2', 'password1', 'radio0')} == {'wireless'}
    assert uplink_sta(after, resolve_settings(after, AUTO)).name == sta.name


def test_switch_to_5ghz_enables_the_disabled_radio():
    snap = load('live_20260929.uci')
    assert snap.section('wireless', 'radio1').get('disabled') == '1'
    st = resolve_settings(snap, AUTO)
    cmds = plan_switch(snap, st, 'WiFi-5G', 'psk2', 'password1', 'radio1')
    after = snap.apply(cmds)
    assert uplink_sta(after, st).get('device') == 'radio1'
    assert after.section('wireless', 'radio1').get('disabled') == '0'
    assert errors(after, resolve_settings(after, AUTO)) == set()
