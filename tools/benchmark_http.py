"""Reproducible local HTTP test. Synthetic observations, real HTTP requests and SQLite."""
from pathlib import Path
import sys,time,json,threading,statistics,platform,sqlite3,concurrent.futures,importlib.metadata,socket,logging,hashlib,re
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import requests
from waitress import create_server
from minepath import create_app
PROJECT=Path(__file__).resolve().parents[1]
OUT=PROJECT/'evidence'/'tests';OUT.mkdir(parents=True,exist_ok=True)
queue_log=logging.FileHandler(OUT/'http-server.log',mode='w',encoding='utf-8');queue_log.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
queue_logger=logging.getLogger('waitress.queue');queue_logger.addHandler(queue_log);queue_logger.propagate=False
RUN=(PROJECT/'.cache/tmp')/('minepath-http-'+str(int(time.time())));RUN.mkdir(parents=True,exist_ok=True)
app=create_app(PROJECT,RUN/'benchmark.sqlite3');svc=app.extensions['service']
tokens={}
with svc.tx(True) as db:
    for i in range(1,31):
        user=f'E{i:03}';area='ABC'[(i-1)//10]
        tokens[user]=svc.start_session(db,user,f'D{i:03}',None,'http-load',area,'HTTP benchmark','simulation')
server=create_server(app,host='127.0.0.1',port=8091,threads=48)
thread=threading.Thread(target=server.run,daemon=True);thread.start();base='http://127.0.0.1:8091/api'
def login(username):
    s=requests.Session();csrf=s.get(base+'/bootstrap',timeout=10).json()['csrf'];r=s.post(base+'/login',json={'username':username,'password':'Demo@2026!'},headers={'X-CSRF-Token':csrf},timeout=30);r.raise_for_status();s.headers['X-CSRF-Token']=r.json()['csrf'];return s
clients={}
with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
    for i,s in zip(range(1,31),pool.map(login,[f'employee{i:02}' for i in range(1,31)])):clients[f'E{i:03}']=s
admin=login('admin')
leaders=[login(f'leader{i:02}') for i in range(1,4)]
def open_stream(client):
    response=client.get(base+'/stream',stream=True,timeout=(10,30));response.raise_for_status()
    for line in response.iter_lines(chunk_size=1):
        if line==b'event: refresh':break
    return response
with concurrent.futures.ThreadPoolExecutor(max_workers=34) as pool:
    streams=list(pool.map(open_stream,list(clients.values())+leaders+[admin]))
def quantile(values,p):return sorted(values)[max(0,__import__('math').ceil(p*len(values))-1)]
def summarize(values):return {'n':len(values),'median_ms':round(statistics.median(values),3),'p95_ms':round(quantile(values,.95),3),'max_ms':round(max(values),3)}
barrier=threading.Barrier(30)
def producer(user):
    s=clients[user];durations=[];failures=[];barrier.wait()
    for round in range(10):
        payload={'collector_token':tokens[user]['collector_token'],'points':[{'point_id':f'http-{user}-{round}','observed_at':time.time(),'x':50+200*((int(user[1:])-1)//10),'y':50,'accuracy':3}]}
        begin=time.perf_counter()
        try:
            r=s.post(base+'/positions',json=payload,timeout=30);r.raise_for_status()
            if r.json()['results'][0]['status']!='accepted':failures.append(r.json())
        except Exception as e:failures.append(str(e))
        durations.append((time.perf_counter()-begin)*1000)
    return durations,failures
begin=time.perf_counter()
with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:results=list(pool.map(producer,clients))
elapsed=time.perf_counter()-begin
latencies=[v for result,_ in results for v in result];failures=[v for _,errors in results for v in errors]
record={'generated_at':time.time(),'environment':{'platform':platform.platform(),'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,'flask':importlib.metadata.version('Flask'),'waitress':importlib.metadata.version('waitress'),'host':socket.gethostname(),'logical_cpus':__import__('os').cpu_count(),'server_threads':48},'scope':'localhost HTTP; 30 authenticated synthetic employees, 10 one-point requests each with server-issued simulation leases; 34 SSE connections opened and retained during test (30 employees, 3 leaders, 1 admin), initial refresh checked. No real device throughput or Internet/LAN guarantee. Login/SSE setup time excluded; no artificial pacing; not a 30-phone field experiment.','sse_initial_connections':len(streams),'upload':{**summarize(latencies),'clients':30,'requests':300,'elapsed_seconds':round(elapsed,3),'accepted':300-len(failures),'failures':failures,'completed_requests_per_second':round(300/elapsed,2)},'history':[]}
for size in (10000,100000):
    start=time.perf_counter();added=svc.import_simulation(PROJECT/'evidence'/'simulated'/'sim-20261028-v1'/f'positions_{size}.csv','ADMIN');load=time.perf_counter()-start
    timings=[];counts=[]
    for _ in range(10):
        start=time.perf_counter();r=admin.get(base+'/tracks',params={'user_id':'E001','start':0,'end':time.time()+1,'limit':10000},timeout=30);r.raise_for_status();payload=r.json();timings.append((time.perf_counter()-start)*1000);counts.append(len(payload['points']))
    with svc.tx() as db:count=db.execute('SELECT count(*) FROM observations').fetchone()[0]
    record['history'].append({'fixture_target':size,'new_fixture_points':added,'table_total_including_300_http_points':count,'fixture_import_seconds':round(load,3),'single_employee_query':summarize(timings),'returned_counts':counts,'note':'10 successive queries, includes JSON decoding; warm filesystem cache, first query included. Table volume is not returned-row volume.'})
    (OUT/'http-benchmark.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
record['database_bytes']=(RUN/'benchmark.sqlite3').stat().st_size
queue_log.flush();queue_depths=[int(x) for x in re.findall(r'Task queue depth is (\d+)',(OUT/'http-server.log').read_text(encoding='utf-8'))]
record['queue']={'warning_count':len(queue_depths),'max_observed_depth':max(queue_depths,default=0),'raw_log':'http-server.log'}
with svc.tx() as db:record['upload']['stored_http_points']=db.execute("SELECT count(*) FROM observations WHERE point_id LIKE 'http-%' AND source='simulation'").fetchone()[0]
record['backend_sha256']={p.relative_to(PROJECT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((PROJECT/'src'/'minepath').glob('*.py'))}
(OUT/'http-benchmark.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(record,ensure_ascii=True,indent=2),flush=True)
for response in streams:response.close()
for s in list(clients.values())+leaders+[admin]:s.close()
server.close()
