"""转件 HTTP 接口集成测试：鉴权、校验状态码与幂等响应。"""

import os

os.environ.setdefault('AUTH_KEY', 'test-key-0123456789abcdef0123456789abcdef')
os.environ['DB_DRIVER'] = 'sqlite'

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

import src.config.settings as settings
from src.models.tables import cases, schema, transfers


@pytest.fixture()
def client(tmp_path):
    settings.ARTIFACTS = str(tmp_path)
    db = create_engine('sqlite://', connect_args={'check_same_thread': False},
                       poolclass=StaticPool)
    settings.engine = db
    settings.cache = _FakeCache()
    schema.create_all(db)
    from src.services import workflow
    workflow.engine = db
    workflow.cache = _FakeCache()
    from src.utils.time import utc_time
    with db.begin() as conn:
        for slug, title, batch in (('a', '批次甲', 'BATCH-A'), ('b', '批次乙', 'BATCH-B')):
            conn.execute(cases.insert().values(
                slug=slug, title=title, author='operator', created_at=utc_time(),
                payload={'batch_no': batch, 'run_bytes': 4096, 'fan_in': 2,
                         'records': [{'item_id': f'{slug.upper()}{n}',
                                      'accession_no': f'GJ-2026-{n}',
                                      'title': f'题名{n}'} for n in range(1, 4)]}))
    from src.main import app
    with TestClient(app) as test_client:
        yield test_client, db
    schema.drop_all(db)
    db.dispose()


class _FakeCache:
    def lpush(self, *a):
        return 1

    def ltrim(self, *a):
        return True

    def ping(self):
        return True

    def brpop(self, *a, **k):
        return None


def token(role):
    return jwt.encode({'sub': role, 'role': role, 'exp': 9_999_999_999},
                      settings.AUTH_KEY, algorithm='HS256')


def body(**over):
    data = {'handover_no': 'HJ-HTTP-01', 'from_case_id': 1, 'to_case_id': 2,
            'from_item_ids': ['A1'], 'to_item_ids': ['B1']}
    data.update(over)
    return {'payload': data}


def test_requires_authentication(client):
    test_client, _ = client
    response = test_client.post('/transfers', json=body())
    assert response.status_code == 401


def test_viewer_cannot_create_transfer(client):
    test_client, _ = client
    response = test_client.post('/transfers', json=body(),
                                headers={'Authorization': f'Bearer {token("viewer")}'})
    assert response.status_code == 403


def test_operator_create_then_idempotent_replay(client):
    test_client, db = client
    headers = {'Authorization': f'Bearer {token("operator")}'}
    first = test_client.post('/transfers', json=body(), headers=headers)
    assert first.status_code == 202
    assert first.json()['status'] == 'queued'
    # 同交接号重试：200 + 幂等标记，不新增记录。
    second = test_client.post('/transfers', json=body(), headers=headers)
    assert second.status_code == 200
    assert second.headers.get('x-idempotent-replay') == 'true'
    assert second.json()['id'] == first.json()['id']
    with db.connect() as conn:
        total = conn.execute(
            select(func.count()).select_from(transfers).where(
                transfers.c.handover_no == 'HJ-HTTP-01')).scalar_one()
    assert total == 1


def test_validation_error_returns_422_without_record(client):
    test_client, db = client
    headers = {'Authorization': f'Bearer {token("operator")}'}
    response = test_client.post(
        '/transfers', json=body(handover_no=' '), headers=headers)
    assert response.status_code == 422
    response = test_client.post(
        '/transfers', json=body(from_case_id=2, to_case_id=2), headers=headers)
    assert response.status_code == 422
    response = test_client.post(
        '/transfers', json=body(from_item_ids=[], to_item_ids=[]), headers=headers)
    assert response.status_code == 422
    with db.connect() as conn:
        assert conn.execute(select(func.count()).select_from(transfers)).scalar_one() == 0


def test_list_and_get_transfers(client):
    test_client, _ = client
    headers = {'Authorization': f'Bearer {token("operator")}'}
    test_client.post('/transfers', json=body(), headers=headers)
    listing = test_client.get('/transfers', headers=headers)
    assert listing.status_code == 200
    assert len(listing.json()) == 1
    identity = listing.json()[0]['id']
    detail = test_client.get(f'/transfers/{identity}', headers=headers)
    assert detail.status_code == 200
    assert detail.json()['handover_no'] == 'HJ-HTTP-01'
    missing = test_client.get('/transfers/9999', headers=headers)
    assert missing.status_code == 404
