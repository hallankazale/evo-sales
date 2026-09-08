from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_create_lead_rejects_short_name():
    response = client.post('/api/leads', json={
        'company_name': 'A',
        'segment': 'teste',
        'city': 'Campo Verde',
        'contact': '',
        'source': 'manual',
    })
    assert response.status_code == 422
