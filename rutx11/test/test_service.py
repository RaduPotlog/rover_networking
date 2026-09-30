from pathlib import Path

import pytest

from uplink_manager.infrastructure.demo_router import FakeRouter
from uplink_manager.application.jobs import Busy, JobRunner
from uplink_manager.application.service import Timing, UplinkService
from uplink_manager.domain.invariants import UplinkSettings, audit_config, uplink_sta
from uplink_manager.domain.wifi import CredentialError

FIX = Path(__file__).parent / 'fixtures'
ST = UplinkSettings(uplink_iface='wan1')  # fixed name: these tests exercise the orphan paths


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t


def make(fixture='healthy.uci', networks=None):
    router = FakeRouter(FIX / fixture, networks, settings=ST)
    clock = Clock()
    svc = UplinkService(router, ST, JobRunner(), Timing(verify_s=30, poll_s=3, settle_s=0),
                        sleep=clock.sleep, clock=clock.now)
    return router, svc


def run(job):
    # JobRunner runs in a thread; the fake is instant, so wait for the thread.
    import time
    for _ in range(500):
        if job.status != 'running':
            return job
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def errors(router):
    return [c for c in audit_config(router.snap, ST) if c.status == 'error']


def test_status_healthy():
    _, svc = make()
    s = svc.status()
    assert s['healthy'] and s['configured']['ssid'] == 'Orange-Tekwill'


def test_switch_happy_path_keeps_change():
    router, svc = make(networks={'Orange-Tekwill': '<REDACTED>', 'Cafe': 'password1'})
    job = run(svc.start_switch('Cafe', 'psk2', 'password1', 'radio0'))
    assert job.status == 'succeeded', job.to_dict()
    assert uplink_sta(router.snap, ST).get('ssid') == 'Cafe'
    assert router.log[-1].startswith('confirm')
    assert not any('password1' in m for _, m in job.log)  # key never logged


def test_wrong_password_rolls_back():
    router, svc = make(networks={'Orange-Tekwill': '<REDACTED>', 'Cafe': 'password1'})
    job = run(svc.start_switch('Cafe', 'psk2', 'wrongpass', 'radio0'))
    assert job.status == 'failed'
    assert 'did not associate' in job.result
    assert uplink_sta(router.snap, ST).get('ssid') == 'Orange-Tekwill'
    assert any(line.startswith('rollback') for line in router.log)


def test_apply_error_rolls_back():
    router, svc = make(networks={'Orange-Tekwill': '<REDACTED>', 'Cafe': 'password1'})
    router.fail_apply = True
    job = run(svc.start_switch('Cafe', 'psk2', 'password1', 'radio0'))
    assert job.status == 'failed' and router.log[-1].startswith('rollback')


def test_pending_rutos_changes_refuse():
    router, svc = make(networks={'Orange-Tekwill': '<REDACTED>', 'Cafe': 'password1'})
    router.pending = "wireless.cfg093579.ssid='x'"
    job = run(svc.start_switch('Cafe', 'psk2', 'password1', 'radio0'))
    assert job.status == 'failed' and 'uncommitted' in job.result
    assert not router.log


def test_bad_credentials_rejected_before_job():
    _, svc = make()
    with pytest.raises(CredentialError):
        svc.start_switch('Cafe', 'psk2', 'short', 'radio0')


def test_switch_repairs_broken_router():
    router, svc = make('after_rutos_join.uci',
                       networks={'Orange-Tekwill': '<REDACTED>', 'Cafe': 'password1'})
    job = run(svc.start_switch('Cafe', 'psk2', 'password1', 'radio0'))
    assert job.status == 'succeeded', job.to_dict()
    assert errors(router) == []
    assert svc.status()['healthy']


def test_adopt_orphan_from_rutos_join():
    router, svc = make('after_rutos_join.uci', networks={'Field Hotspot': '<REDACTED>'})
    assert not svc.status()['healthy']
    job = run(svc.start_action('adopt:cfg0c4f2a'))
    assert job.status == 'succeeded', job.to_dict()
    assert uplink_sta(router.snap, ST).get('ssid') == 'Field Hotspot'
    assert errors(router) == [] and svc.status()['healthy']


def test_repair_without_uplink_does_not_require_internet():
    # Uplink out of range: firewall repairs still apply, orphans stay for the operator.
    router, svc = make('after_rutos_join.uci', networks={})
    job = run(svc.start_repair())
    assert job.status == 'succeeded', job.to_dict()
    assert zone_net(router, 'wwan') == 'wan1' and zone_net(router, 'lan') == 'lan WWAN'


def test_repair_on_healthy_router_is_a_noop():
    router, svc = make()
    job = run(svc.start_repair())
    assert job.status == 'succeeded' and job.result == 'nothing to repair'
    assert router.log == []


def test_single_flight():
    import threading
    router, svc = make()
    gate = threading.Event()
    svc.jobs.start('block', lambda job: gate.wait(5) and 'ok')
    with pytest.raises(Busy):
        svc.start_repair()
    gate.set()


def test_audit_payload_redacts_keys():
    _, svc = make('after_rutos_join.uci', networks={})
    a = svc.audit()
    text = repr(a)
    assert '<REDACTED>' not in text and '********' in text  # adopt preview carries the key


def zone_net(router, name):
    from uplink_manager.domain.invariants import zone
    return zone(router.snap, name).get('network')
