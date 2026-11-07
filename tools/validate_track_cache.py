"""Supplemental cache consistency checks; do not alter the frozen run guard."""
from pathlib import Path
import concurrent.futures,json,sys,tempfile,time
from unittest.mock import patch
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'tools'))
from track_eval import contract_app,login,fingerprint
app=contract_app(tempfile.mkdtemp(prefix='track-cache-check-',dir=str(root / '.cache/tmp')))
svc=app.extensions['service'];admin=app.test_client();login(admin,'admin')
url='/api/tracks?user_id=E001&start=0&end=2000'
checks=[]
r=admin.get(url);assert r.headers['X-Track-Cache']=='miss'
initial=r.json;r=admin.get(url);assert r.headers['X-Track-Cache']=='hit' and r.json==initial
checks.append('repeated response matches all original fields')
other=app.test_client();login(other,'employee02');r=other.get(url)
assert not r.json['points'] and r.headers['X-Track-Cache']=='miss'
checks.append('cache isolated by authenticated identity and scope')
with svc.tx(True) as db:db.execute("UPDATE session_areas SET area_id='B' WHERE session_id='track-s1' AND valid_from=1000")
r=admin.get(url);assert r.headers['X-Track-Cache']=='miss'
assert next(p for p in r.json['points'] if p['observed_at']==1000)['effective_area_id']=='B'
checks.append('backdated assignment edit invalidates identical query')
admin.get(url)
with svc.tx(True) as db:db.execute("UPDATE settings SET value='17' WHERE key='coverage_gap_seconds'")
r=admin.get(url);assert r.headers['X-Track-Cache']=='miss' and r.json['segment_gap_seconds']==17
checks.append('statistics setting changes invalidate metadata')
leader=app.test_client();login(leader,'leader01');assert leader.get(url).json['points'];assert leader.get(url).headers['X-Track-Cache']=='hit'
with svc.tx(True) as db:db.execute("UPDATE leaderships SET valid_to=1800000000 WHERE user_id='L01'")
r=leader.get(url);assert not r.json['points']
checks.append('management scope revocation has no stale coordinate exposure')
for limit in range(1,8):assert admin.get(url+'&limit='+str(limit)).headers['X-Track-Cache']=='miss'
assert admin.get(url+'&limit=1').headers['X-Track-Cache']=='miss'
checks.append('six-entry LRU capacity evicts oldest request')
admin.get(url)
with patch('minepath.web.time.monotonic',return_value=time.monotonic()+11):
    assert admin.get(url).headers['X-Track-Cache']=='miss'
checks.append('ten-second expiry recomputes response')
with svc.tx(True) as db:
    db.executemany("INSERT INTO observations(point_id,user_id,session_id,observed_at,received_at,x,y,accuracy,source,team_id,assessment) VALUES(?,'E001','track-s1',?,?,50,50,3,'simulation','T01','late_review')",[(f'large-{i}',1010+i/1000,1100+i/1000) for i in range(7000)])
r=admin.get(url);assert len(r.data)>2*1024*1024 and r.headers['X-Track-Cache']=='miss'
assert admin.get(url).headers['X-Track-Cache']=='miss'
checks.append('responses above two MiB are not retained')
def concurrent_read(number):
    client=app.test_client();login(client,'admin');r=client.get(url+'&limit=50')
    assert r.status_code==200
    return fingerprint(r.json)
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:digests=list(pool.map(concurrent_read,range(8)))
assert len(set(digests))==1
checks.append('eight concurrent requests return identical authorized data')
result={'checks':checks,'passed':len(checks),'failed':0,'note':'Additional isolated synthetic checks. Frozen performance command and original 52 run guards unchanged.'}
output=root/'.cache/autoresearch-tracks/cache-consistency.json';output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=True))
