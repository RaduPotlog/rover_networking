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

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uplink_manager.infrastructure.demo_router import FakeRouter
from uplink_manager.application.jobs import JobRunner
from uplink_manager.application.service import Timing, UplinkService
from uplink_manager.domain.invariants import UplinkSettings
from uplink_manager.presentation.api import create_app

FIX = Path(__file__).parent / 'fixtures'
AUTH = ('rover', 'secret')
POST = {'X-Requested-With': 'netui'}


@pytest.fixture
def client():
    router = FakeRouter(FIX / 'healthy.uci', {'Orange-Tekwill': '<REDACTED>', 'Cafe': 'password1'})
    svc = UplinkService(router, UplinkSettings(), JobRunner(),
                        Timing(verify_s=1, poll_s=0.01, settle_s=0))
    c = TestClient(create_app(svc, *AUTH))
    c.router = router
    return c


def wait(client, job_id):
    for _ in range(300):
        j = client.get(f'/api/jobs/{job_id}', auth=AUTH).json()
        if j['status'] != 'running':
            return j
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def test_health_is_public_everything_else_needs_login(client):
    assert client.get('/healthz').status_code == 200
    for path in ('/', '/api/status', '/api/scan', '/api/audit', '/api/jobs'):
        assert client.get(path).status_code == 401
        assert client.get(path, auth=('rover', 'wrong')).status_code == 401
    assert client.get('/', auth=AUTH).status_code == 200


def test_post_without_csrf_header_is_refused(client):
    r = client.post('/api/repair', auth=AUTH)
    assert r.status_code == 403


def test_switch_via_http(client):
    r = client.post('/api/switch', auth=AUTH, headers=POST,
                    json={'ssid': 'Cafe', 'encryption': 'psk2', 'key': 'password1', 'radio': 'radio0'})
    assert r.status_code == 200, r.text
    j = wait(client, r.json()['job'])
    assert j['status'] == 'succeeded', j
    assert 'password1' not in str(j)
    s = client.get('/api/status', auth=AUTH).json()
    assert s['healthy'] and s['runtime']['ssid'] == 'Cafe'


def test_switch_validation_errors(client):
    r = client.post('/api/switch', auth=AUTH, headers=POST,
                    json={'ssid': 'Cafe', 'encryption': 'psk2', 'key': 'short'})
    assert r.status_code == 400
    r = client.post('/api/switch', auth=AUTH, headers=POST,
                    json={'ssid': 'Cafe', 'encryption': 'psk2', 'key': 'password1',
                          'radio': 'radio0; reboot'})
    assert r.status_code == 422


def test_action_id_is_validated(client):
    r = client.post('/api/action', auth=AUTH, headers=POST, json={'id': 'adopt:x; reboot'})
    assert r.status_code == 422


def test_audit_and_scan(client):
    a = client.get('/api/audit', auth=AUTH).json()
    assert a['repair'] == [] and all(c['status'] == 'ok' for c in a['checks'])
    nets = client.get('/api/scan', auth=AUTH).json()
    assert {'Orange-Tekwill', 'Cafe'} <= {n['ssid'] for n in nets}
