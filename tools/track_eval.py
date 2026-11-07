"""Frozen historical-track HTTP evaluation for the approved autoresearch run.

The --freeze command is used once on the untouched baseline, before run init.
Normal execution only reads tracked evaluation files and writes ignored outputs.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode

PROJECT = Path(__file__).resolve().parents[1]
TEMP_ROOT = PROJECT / '.cache' / 'tmp'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(PROJECT / 'src'))
from minepath import create_app

CLOCK = 1800000000.0
FIXTURE = Path('evidence/simulated/sim-20261028-v1/positions_100000.csv')
PERF_CONTRACT = PROJECT / 'tests/fixtures/tracks_perf_contract.json'
RESPONSE_CONTRACT = PROJECT / 'tests/fixtures/tracks_response_contract.json'
QUERY = {'user_id': 'E001', 'start': 0, 'end': CLOCK + 1, 'limit': 10000}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def login(client, username):
    csrf = client.get('/api/bootstrap').json['csrf']
    response = client.post('/api/login', json={'username': username, 'password': 'Demo@2026!'}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 200, response.json


def contract_app(directory):
    """Small deterministic database with cross-team and effective-time edge cases."""
    app = create_app(PROJECT, Path(directory) / 'contract.sqlite3', clock=lambda: CLOCK, testing=True)
    svc = app.extensions['service']
    with svc.tx(True) as db:
        db.execute("UPDATE memberships SET valid_to=1030 WHERE user_id='E001'")
        db.execute("INSERT INTO memberships(user_id,team_id,valid_from) VALUES('E001','T02',1030)")
        for sid, begin, end in [('track-s1', 990, 1100), ('track-s2', 1050, 1100), ('track-s3', 1100, 1120), ('track-empty', 1200, 1300)]:
            db.execute('INSERT INTO work_sessions(id,user_id,started_at,ended_at,purpose) VALUES(?,?,?,?,?)', (sid, 'E001', begin, end, 'frozen synthetic contract'))
        assignments = [
            ('track-s1', 'A', 1000, 1020),
            ('track-s1', 'B', 1030, 1040),
            ('track-s1', 'C', 1040, 1080),
            ('track-s1', 'B', 1050, 1060),
            ('track-s1', 'A', 1070, 1075),
            ('track-s1', 'A', 1050, 1055),  # Equal start: preserve original SQLite tie order.
            ('track-s2', 'A', 1050, None),
        ]
        for sid, area, begin, end in assignments:
            db.execute('INSERT INTO session_areas(session_id,area_id,valid_from,valid_to,recorded_at,actor_id) VALUES(?,?,?,?,?,?)', (sid, area, begin, end, 2000, 'ADMIN'))
        moments = [999, 1000, 1019.999, 1020, 1025, 1029.999, 1030, 1039.999, 1040, 1049.999, 1050, 1054.999, 1055, 1059.999, 1060, 1069.999, 1070, 1074.999, 1075, 1079.999, 1080]
        samples = [('track-s1', t) for t in moments] + [('track-s2', 1050), ('track-s2', 1090), ('track-s2', 1095), ('track-s3', 1105)]
        for number, (sid, at) in enumerate(samples):
            team = 'T01' if at < 1030 else 'T02'
            uncalibrated = sid == 'track-s3'
            db.execute('INSERT INTO observations(point_id,user_id,session_id,device_id,observed_at,received_at,x,y,lat,lng,accuracy,source,team_id,area_id,area_version_id,assessment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (f'contract-{number:02}', 'E001', sid, 'D001', at, at + 10, None if uncalibrated else 30 + number, None if uncalibrated else 50, 31 if uncalibrated else None, 121 if uncalibrated else None, 55 if uncalibrated else 3, 'browser' if uncalibrated else 'simulation', team, 'A', 'A-v1', 'uncertain' if uncalibrated else 'late_review'))
    return app


def contract_cases():
    full = {'user_id': 'E001', 'start': 0, 'end': 2000}
    cases = []
    for username in ('admin', 'employee01', 'employee02', 'leader01', 'leader02', 'leader03', None):
        cases.append({'name': (username or 'anonymous') + '-full', 'username': username, 'query': full})
    for title, options in [
        ('before-assignments', {'start': 990, 'end': 1000}),
        ('assignment-gap', {'start': 1020, 'end': 1030}),
        ('team-cutoff', {'start': 1030, 'end': 1040}),
        ('overlapping-intervals', {'start': 1040, 'end': 1081}),
        ('identical-starts', {'start': 1050, 'end': 1056}),
        ('no-assignment', {'start': 1100, 'end': 1200}),
        ('empty-session', {'start': 1200, 'end': 1300}),
        ('nonexistent-user', {'user_id': 'unknown'}),
        ('limit-one', {'limit': 1}),
        ('limit-three', {'limit': 3}),
        ('zero-limit-clamped', {'limit': 0}),
        ('max-limit', {'limit': 50000}),
        ('duplicate-time-cursor', {'after_time': 1050, 'after_id': 11, 'limit': 2}),
        ('duplicate-time-second-cursor', {'after_time': 1050, 'after_id': 22, 'limit': 2}),
        ('invalid-range', {'start': 1100, 'end': 1000}),
        ('invalid-time', {'start': 'NaN'}),
        ('invalid-cursor', {'after_id': 'bad'}),
    ]:
        cases.append({'name': title, 'username': 'admin', 'query': {**full, **options}})
    for username in ('leader01', 'leader02', 'employee02'):
        cases.append({'name': username + '-pagination', 'username': username, 'query': {**full, 'limit': 2, 'after_time': 1029.999, 'after_id': 0}})
    return cases


def freeze_responses():
    app = contract_app(tempfile.mkdtemp(prefix='track-contract-freeze-', dir=TEMP_ROOT))
    output = []
    for case in contract_cases():
        with app.test_client() as client:
            if case['username']:
                login(client, case['username'])
            response = client.get('/api/tracks?' + urlencode(case['query']))
            output.append({**case, 'status': response.status_code, 'response': response.json})
    RESPONSE_CONTRACT.parent.mkdir(parents=True, exist_ok=True)
    RESPONSE_CONTRACT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')


def measure(freeze=False):
    import requests
    from waitress import create_server
    if freeze and (PERF_CONTRACT.exists() or RESPONSE_CONTRACT.exists()):
        raise SystemExit('Frozen contracts already exist; they will not be overwritten.')
    fixture = PROJECT / FIXTURE
    fixture_sha = hashlib.sha256(fixture.read_bytes()).hexdigest()
    expected = None if freeze else json.loads(PERF_CONTRACT.read_text(encoding='utf-8'))
    if expected:
        assert expected['fixture_sha256'] == fixture_sha
        assert expected['query'] == QUERY
        assert expected['clock'] == CLOCK
    output = PROJECT / '.cache/autoresearch-tracks/measurements'
    output.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='track-http-', dir=TEMP_ROOT))
    app = create_app(PROJECT, run_dir / 'bench.sqlite3', clock=lambda: CLOCK)
    svc = app.extensions['service']
    assert svc.import_simulation(fixture, 'ADMIN') == 100000
    with svc.tx() as db:
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0] == 100000
    server = create_server(app, host='127.0.0.1', port=0, threads=4)
    threading.Thread(target=server.run, daemon=True).start()
    base = f'http://127.0.0.1:{server.socket.getsockname()[1]}/api'
    client = requests.Session()
    try:
        csrf = client.get(base + '/bootstrap', timeout=10).json()['csrf']
        logged = client.post(base + '/login', json={'username': 'admin', 'password': 'Demo@2026!'}, headers={'X-CSRF-Token': csrf}, timeout=10)
        logged.raise_for_status()
        durations = []
        for number in range(34):  # Three warm-ups, then exactly 31 measurements.
            before = time.perf_counter_ns()
            response = client.get(base + '/tracks', params=QUERY, timeout=30)
            response.raise_for_status()
            payload = response.json()
            elapsed = (time.perf_counter_ns() - before) / 1e6
            assert len(payload['points']) == 3334
            digest = fingerprint(payload)
            if expected:
                assert digest == expected['response_sha256'], 'Track response changed'
            if number >= 3:
                durations.append(elapsed)
        statements = []
        original = svc.connect
        def traced_connection():
            db = original()
            db.set_trace_callback(statements.append)
            return db
        svc.connect = traced_connection
        probe = client.get(base + '/tracks', params=QUERY, timeout=30)
        probe.raise_for_status()
        assert fingerprint(probe.json()) == digest
        svc.connect = original
    finally:
        client.close()
        server.close()
    result = {
        'median_ms': round(statistics.median(durations), 6),
        'p95_ms': round(sorted(durations)[29], 6),
        'durations_ms': durations,
        'measured_requests': 31,
        'warmup_requests': 3,
        'point_count': 3334,
        'total_observations': 100000,
        'query': QUERY,
        'response_sha256': digest,
        'fixture_sha256': fixture_sha,
        'sql_statements_per_query': len(statements),
        'web_sha256': hashlib.sha256((PROJECT / 'src/minepath/web.py').read_bytes()).hexdigest(),
        'python': platform.python_version(),
        'platform': platform.platform(),
        'logical_cpus': os.cpu_count(),
        'notice': 'Localhost HTTP + JSON decoding; warm cache; fixed synthetic data and clock; no SSE or concurrent upload in this isolated query metric. Not a field test or absolute positioning accuracy.',
    }
    (output / (str(time.time_ns()) + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if freeze:
        PERF_CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        PERF_CONTRACT.write_text(json.dumps({'fixture_sha256': fixture_sha, 'response_sha256': digest, 'expected_points': 3334, 'total_observations': 100000, 'clock': CLOCK, 'query': QUERY}, indent=2), encoding='utf-8')
        freeze_responses()
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--freeze', action='store_true')
    measure(parser.parse_args().freeze)
