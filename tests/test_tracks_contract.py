"""Exact response guard frozen from the untouched v1 implementation."""
from pathlib import Path
import json
import sys
import pytest
from urllib.parse import urlencode
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))
from track_eval import contract_app, login
CASES = json.loads((PROJECT / 'tests/fixtures/tracks_response_contract.json').read_text(encoding='utf-8'))

@pytest.fixture(scope='module')
def app(tmp_path_factory):
    return contract_app(tmp_path_factory.mktemp('track-response-contract'))

@pytest.mark.parametrize('case', CASES, ids=[case['name'] for case in CASES])
def test_exact_historical_track_contract(app, case):
    with app.test_client() as client:
        if case['username']:
            login(client, case['username'])
        response = client.get('/api/tracks?' + urlencode(case['query']))
        assert response.status_code == case['status']
        assert response.json == case['response']
