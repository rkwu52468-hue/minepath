"""Post-run cold-query validation. Does not change the frozen optimization metric."""
from pathlib import Path
import sys,tempfile,time,json,threading,statistics,hashlib
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'tools'))
from track_eval import create_app,CLOCK,QUERY,FIXTURE,fingerprint,PERF_CONTRACT
import requests
from waitress import create_server
app=create_app(root,Path(tempfile.mkdtemp(prefix='track-accept-',dir=str(root / '.cache/tmp')))/'app.sqlite3',clock=lambda:CLOCK)
svc=app.extensions['service'];svc.import_simulation(root/FIXTURE,'ADMIN')
expected=json.loads(PERF_CONTRACT.read_text(encoding='utf-8'))
server=create_server(app,host='127.0.0.1',port=0,threads=4);threading.Thread(target=server.run,daemon=True).start()
base=f'http://127.0.0.1:{server.socket.getsockname()[1]}/api';client=requests.Session()
csrf=client.get(base+'/bootstrap').json()['csrf'];r=client.post(base+'/login',json={'username':'admin','password':'Demo@2026!'},headers={'X-CSRF-Token':csrf});r.raise_for_status()
cold=[];misses=[]
try:
    for number in range(15):
        # Distinct legitimate bounds, same complete historical result; every request is a miss.
        query={**QUERY,'end':CLOCK+1+number}
        before=time.perf_counter_ns();r=client.get(base+'/tracks',params=query,timeout=30);r.raise_for_status();body=r.json();cold.append((time.perf_counter_ns()-before)/1e6)
        assert fingerprint(body)==expected['response_sha256'];assert r.headers['X-Track-Cache']=='miss';misses.append(r.headers['X-Track-Cache'])
    r=client.get(base+'/tracks',params=query,timeout=30);r.raise_for_status();assert r.headers['X-Track-Cache']=='hit' and fingerprint(r.json())==expected['response_sha256']
finally:
    client.close();server.close()
result={'cold_query_median_ms':statistics.median(cold),'cold_query_durations_ms':cold,'cold_queries':len(cold),'cache_statuses':misses,'returned_points_each':3334,'response_sha256':expected['response_sha256'],'repeat_hit_verified':True,'web_sha256':hashlib.sha256((root/'src/minepath/web.py').read_bytes()).hexdigest(),'scope':'Post-run diagnostic with distinct end bounds and identical full result; not the frozen 31-repeat optimization metric.'}
dest=root/'evidence/tests/autoresearch-tracks-20261028';dest.mkdir(parents=True,exist_ok=True);(dest/'cold-query-acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result))
