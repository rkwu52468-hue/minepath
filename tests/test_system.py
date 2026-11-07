"""Business integration tests: real SQLite transactions and Flask routes; no fabricated GPS."""
import io,json,shutil,sqlite3,sys,zipfile
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from minepath import create_app
from minepath.core import DomainError

PROJECT=Path(__file__).resolve().parents[1]
@pytest.fixture(scope='session')
def template(tmp_path_factory):
    path=tmp_path_factory.mktemp('template')/'test.sqlite3'
    create_app(PROJECT,path,clock=lambda:1800000000,testing=True)
    return path

@pytest.fixture
def env(tmp_path,template):
    db=tmp_path/'app.sqlite3';shutil.copy2(template,db); clock=[1800000000.0]
    app=create_app(PROJECT,db,clock=lambda:clock[0],testing=True)
    svc=app.extensions['service']
    class Client:
        def __init__(self,username):
            self.c=app.test_client();b=self.c.get('/api/bootstrap').json
            r=self.c.post('/api/login',json={'username':username,'password':'Demo@2026!'},headers={'X-CSRF-Token':b['csrf']})
            assert r.status_code==200,r.json
            self.csrf=r.json['csrf']
        def get(self,path):return self.c.get('/api'+path)
        def post(self,path,data=None,**kw):
            if kw.get('content_type')=='multipart/form-data':return self.c.post('/api'+path,data=data,headers={'X-CSRF-Token':self.csrf},**kw)
            return self.c.post('/api'+path,json=data or {},headers={'X-CSRF-Token':self.csrf})
    return app,svc,clock,Client

def actor(svc,uid):
    with svc.tx() as db:return dict(db.execute('SELECT id,role FROM users WHERE id=?',(uid,)).fetchone())
def capture(env,user='E001'):
    _,svc,clock,_=env
    with svc.tx(True) as db:
        binding=db.execute('SELECT device_id FROM bindings WHERE user_id=? AND valid_to IS NULL',(user,)).fetchone()[0]
        area=db.execute('SELECT area_id FROM allocations WHERE user_id=? AND valid_to IS NULL',(user,)).fetchone()[0]
        return svc.start_session(db,user,binding,None,'test',area,'test scenario','simulation')
def emit(env,token,x=50,y=50,at=None,point_id=None,accuracy=1,user='E001'):
    _,svc,clock,_=env
    with svc.tx(True) as db:return svc.ingest(db,user,token['collector_token'],[{'point_id':point_id or f'p-{clock[0]}-{x}-{y}','observed_at':at if at is not None else clock[0],'x':x,'y':y,'accuracy':accuracy}])[0]
def rows(env,sql,args=()):
    with env[1].tx() as db:return [dict(r) for r in db.execute(sql,args)]
def transfer(env,leader,employee,minutes=2):
    r=leader.post('/transfers',{'user_id':'E001','from_area':'A','to_area':'B','allowed_minutes':minutes});assert r.status_code==200,r.json
    tid=r.json['id'];assert employee.post('/transfers/'+tid+'/start').status_code==200
    return tid

def test_auth_csrf_and_role(env):
    app,svc,clock,C=env;c=app.test_client()
    assert c.get('/api/monitor').status_code==401
    assert c.post('/api/login',json={}).status_code==403
    e=C('employee01');assert e.get('/admin/state').status_code==403
    assert e.post('/admin/simulation',{'running':True}).status_code==403
    assert 'password_hash' not in json.dumps(C('admin').get('/admin/state').json)

def test_one_collector_conflict_and_resume(env):
    token=capture(env);svc=env[1]
    with svc.tx(True) as db:
        with pytest.raises(DomainError,match='已有有效采集端'):svc.start_session(db,'E001','D001',None,'other','A','test','simulation')
        resumed=svc.start_session(db,'E001','D001',None,'test','A','test','simulation',token['collector_token'])
        assert resumed['session_id']==token['session_id']
    assert len(rows(env,'SELECT * FROM work_sessions WHERE ended_at IS NULL'))==1

def test_batch_duplicate_late_and_server_cutoff(env):
    app,svc,clock,C=env;t=capture(env);clock[0]+=10
    assert emit(env,t,at=clock[0]-5,point_id='old')['status']=='accepted'
    assert emit(env,t,at=clock[0]-5,point_id='old')['status']=='duplicate'
    assert emit(env,t,at=clock[0]-8,point_id='out-of-order')['assessment']=='late_review'
    end=clock[0];r=C('employee01').post('/sessions/'+t['session_id']+'/end',{'reason':'end'});assert r.status_code==200
    clock[0]+=10
    assert emit(env,t,at=end-1,point_id='before')['status']=='accepted'
    assert emit(env,t,at=end,point_id='exact')['status']=='rejected'
    assert emit(env,t,at=end+1,point_id='after')['status']=='rejected'
    assert rows(env,'SELECT ended_at FROM work_sessions WHERE id=?',(t['session_id'],))[0]['ended_at']==end

def test_rebind_revokes_prospectively_but_keeps_backfill(env):
    app,svc,clock,C=env;t=capture(env);clock[0]+=10;cutoff=clock[0]
    a=C('admin');assert a.post('/admin/action',{'action':'device_bind','user_id':'E001','client_key':'new-key','reason':'lost old phone'}).status_code==200
    clock[0]+=5
    assert emit(env,t,at=cutoff-1,point_id='cached')['status']=='accepted'
    assert emit(env,t,at=cutoff,point_id='old-unauthorized')['status']=='rejected'
    e=C('employee01');b=e.get('/bootstrap').json
    r=e.post('/sessions/start',{'device_id':b['binding'][0]['device_id'],'client_key':'new-key','tab_id':'new','area_id':'A','purpose':'resume'})
    assert r.status_code==200,r.json;assert r.json['session_id']==t['session_id']

def test_browser_coordinates_derived_server_side(env):
    app,svc,clock,C=env;a=C('admin');e=C('employee01')
    a.post('/admin/action',{'action':'device_bind','user_id':'E001','client_key':'key','reason':'first'})
    a.post('/admin/action',{'action':'calibration','lat':31,'lng':121,'x':50,'y':50,'rotation':0})
    b=e.get('/bootstrap').json;t=e.post('/sessions/start',{'device_id':b['binding'][0]['device_id'],'client_key':'key','tab_id':'t','area_id':'A','purpose':'real test'}).json
    r=e.post('/positions',{'collector_token':t['collector_token'],'points':[{'point_id':'gps','observed_at':clock[0],'lat':31,'lng':121,'accuracy':3,'x':999,'source':'simulation'}]})
    assert r.json['results'][0]['status']=='accepted'
    p=rows(env,'SELECT * FROM observations')[0];assert p['source']=='browser' and p['x']==50 and p['y']==50

def test_membership_history_access_and_current_map_no_leak(env):
    app,svc,clock,C=env;t=capture(env);emit(env,t);clock[0]+=10
    a=C('admin');l1=C('leader01');l2=C('leader02');assert a.post('/admin/action',{'action':'team_change','user_id':'E001','team_id':'T02'}).status_code==200
    assert next(p for p in l2.get('/monitor').json['people'] if p['id']=='E001')['last_position'] is None
    clock[0]+=5;emit(env,t,x=250)
    def points(c):return c.get('/tracks?user_id=E001&start=0&end=1900000000').json['points']
    assert [p['x'] for p in points(l1)]==[50]
    assert [p['x'] for p in points(l2)]==[250]
    assert len(points(a))==2

def test_transfer_deadline_extension_rejection_and_arrival(env):
    app,svc,clock,C=env;t=capture(env);e=C('employee01');l=C('leader01');tid=transfer(env,l,e)
    original=rows(env,'SELECT * FROM transfers')[0]['deadline'];clock[0]+=130;svc.tick()
    assert rows(env,"SELECT * FROM events WHERE kind='transfer_timeout'")
    r=l.post('/transfers/'+tid+'/extend',{'minutes':5,'reason':'detour'});assert r.status_code==200
    assert r.json['original_deadline']==original and r.json['deadline']==original+300
    clock[0]+=2;arrived=clock[0];assert e.post('/transfers/'+tid+'/arrive',{'arrived_at':arrived}).status_code==200
    assert l.post('/transfers/'+tid+'/reject',{'reason':'confirm location again'}).status_code==200
    assert rows(env,'SELECT * FROM transfers')[0]['deadline']==original+300
    clock[0]+=2;e.post('/transfers/'+tid+'/arrive',{'arrived_at':arrived});clock[0]+=20
    r=l.post('/transfers/'+tid+'/confirm',{'effective_at':arrived,'reason':'phone confirmation'});assert r.status_code==200,r.json
    assert r.json['effective_arrival_at']==arrived and r.json['confirmed_at']==clock[0]
    with svc.tx() as db:assert svc.session_area(db,t['session_id'],arrived-1)=='A' and svc.session_area(db,t['session_id'],arrived)=='B'
    assert len(rows(env,"SELECT * FROM events WHERE kind='transfer_timeout'"))==1

def test_detour_allowed_restricted_still_checked_and_cancel(env):
    app,svc,clock,C=env;t=capture(env);e=C('employee01');l=C('leader01');tid=transfer(env,l,e)
    clock[0]+=1;emit(env,t,x=180);clock[0]+=5;emit(env,t,x=180)
    assert not rows(env,"SELECT * FROM events WHERE kind='outside'")
    clock[0]+=5;emit(env,t,x=137);clock[0]+=5;emit(env,t,x=137)
    assert len(rows(env,"SELECT * FROM events WHERE kind='restricted'"))==1
    assert l.post('/transfers/'+tid+'/cancel',{'reason':'change'}).status_code==400
    assert l.post('/transfers/'+tid+'/cancel',{'reason':'change','next_instruction':'wait; issue new task if moving'}).status_code==200
    assert rows(env,'SELECT * FROM transfers')[0]['status']=='cancelled'

def test_open_transfer_follows_employee_even_without_leader(env):
    app,svc,clock,C=env;capture(env);a=C('admin');e=C('employee01');l=C('leader01');tid=transfer(env,l,e)
    deadline=rows(env,'SELECT deadline FROM transfers')[0]['deadline'];clock[0]+=10
    with svc.tx(True) as db:db.execute("UPDATE leaderships SET valid_to=? WHERE team_id='T02'",(clock[0],))
    assert a.post('/admin/action',{'action':'team_change','user_id':'E001','team_id':'T02'}).status_code==200
    t=rows(env,'SELECT * FROM transfers')[0];assert t['owner_team_id']=='T02' and t['deadline']==deadline
    assert l.post('/transfers/'+tid+'/extend',{'minutes':1,'reason':'x'}).status_code==403
    a.post('/admin/action',{'action':'leader_assign','team_id':'T02','leader_id':'L03','temporary':True})
    assert C('leader03').post('/transfers/'+tid+'/extend',{'minutes':1,'reason':'takeover'}).status_code==200

def test_episode_recovery_is_separate_from_closure_and_uncertainty_breaks(env):
    app,svc,clock,C=env;t=capture(env)
    emit(env,t,x=180);clock[0]+=5;emit(env,t,x=180,accuracy=100);clock[0]+=5;emit(env,t,x=180)
    assert not rows(env,"SELECT * FROM events WHERE kind='outside'")
    clock[0]+=5;emit(env,t,x=180);first=rows(env,"SELECT * FROM events WHERE kind='outside'")[0]
    for x in [50,50,180,180]:clock[0]+=5;emit(env,t,x=x)
    records=rows(env,"SELECT * FROM events WHERE kind='outside' ORDER BY started_at")
    assert len(records)==2 and records[0]['observation_state']=='recovered' and records[0]['workflow']=='pending' and records[1]['observation_state']=='active'

def test_region_version_uses_observation_time(env):
    app,svc,clock,C=env;t=capture(env);clock[0]+=10;cutoff=clock[0];a=C('admin')
    r=a.post('/admin/action',{'action':'area_version','area_id':'A','polygon':[[0,0],[60,0],[60,100],[0,100]],'reason':'shrink'});assert r.status_code==200
    clock[0]+=10
    emit(env,t,x=80,at=cutoff-1,point_id='prior');emit(env,t,x=80,at=cutoff,point_id='new')
    records=rows(env,'SELECT * FROM observations ORDER BY observed_at');assert records[0]['area_version_id']!=records[1]['area_version_id']
    assert records[0]['area_version_id']=='A-v1'

def test_gap_retains_session_and_old_backfill_does_not_refresh(env):
    app,svc,clock,C=env;t=capture(env);emit(env,t);clock[0]+=100;svc.tick()
    assert rows(env,'SELECT * FROM work_sessions')[0]['ended_at'] is None
    assert rows(env,"SELECT * FROM events WHERE kind='reporting_gap'")[0]['observation_state']=='active'
    emit(env,t,at=clock[0]-95,point_id='backfill');assert C('employee01').get('/monitor').json['people'][0]['state']=='interrupted'
    emit(env,t,point_id='resume');assert rows(env,"SELECT * FROM events WHERE kind='reporting_gap'")[0]['observation_state']=='recovered'

def test_old_events_stay_original_team_and_can_close_without_employee_reply(env):
    app,svc,clock,C=env;t=capture(env);emit(env,t,x=180);clock[0]+=5;emit(env,t,x=180)
    eid=rows(env,'SELECT id FROM events')[0]['id'];clock[0]+=5
    C('admin').post('/admin/action',{'action':'team_change','user_id':'E001','team_id':'T02'})
    assert C('leader02').post('/events/'+eid+'/action',{'action':'close','text':'try'}).status_code==403
    assert C('leader01').post('/events/'+eid+'/action',{'action':'close','text':'contacted by phone, resolved'}).status_code==200

def test_help_owner_survives_group_change_and_reopens(env):
    app,svc,clock,C=env;e=C('employee01');l1=C('leader01');l2=C('leader02');a=C('admin')
    hid=e.post('/help',{'client_id':'help-1','category':'other','description':'need help'}).json['id']
    assert e.post('/help',{'client_id':'help-1','description':'repeat'}).json['duplicate']
    assert l1.post('/help/'+hid+'/action',{'action':'accept','text':'received'}).status_code==200
    clock[0]+=10;a.post('/admin/action',{'action':'team_change','user_id':'E001','team_id':'T02'})
    assert l2.post('/help/'+hid+'/action',{'action':'assist','text':'contact employee'}).status_code==200
    assert l2.post('/help/'+hid+'/action',{'action':'resolve','text':'steal'}).status_code==403
    assert l1.post('/help/'+hid+'/action',{'action':'resolve','text':'done'}).status_code==200
    assert e.post('/help/'+hid+'/action',{'action':'reopen','text':'still broken'}).status_code==200
    assert rows(env,'SELECT * FROM help_requests')[0]['owner_id']=='L01'
    assert C('employee02').get('/help/'+hid+'/photo').status_code==403
    assert a.post('/help/'+hid+'/action',{'action':'assign','owner_id':'L02','text':'explicit handover'}).status_code==200

def test_snapshot_frozen_and_partial_membership_span(env):
    app,svc,clock,C=env;t=capture(env);begin=clock[0];emit(env,t);clock[0]+=5;emit(env,t);clock[0]+=5
    l1=C('leader01');first=l1.post('/reports',{'start':begin,'end':clock[0]}).json
    before=first['report'];clock[0]+=5;emit(env,t,at=begin+8,point_id='late')
    assert l1.get('/reports/'+first['id']).json==before
    C('admin').post('/admin/action',{'action':'team_change','user_id':'E001','team_id':'T02'});cutoff=clock[0];clock[0]+=10
    r=l1.post('/reports',{'start':begin,'end':clock[0]}).json['report']['rows'][0]
    assert r['session_seconds']==cutoff-begin and r['coverage_seconds']<=r['session_seconds'] and r['browser_points']==0
    assert C('leader02').get('/reports/'+first['id']).status_code==403
    assert l1.get('/reports/'+first['id']+'.csv').status_code==200

def test_bulk_import_per_row_and_backup_integrity(env):
    app,svc,clock,C=env;a=C('admin')
    csv='username,name,team_id,area_id,password\nnew01,测试员工,T01,A,Strong12345!\nnew01,重复,T01,A,Strong12345!\nbad,无效,none,A,Strong12345!\n'
    r=a.post('/admin/import-employees',data={'file':(io.BytesIO(csv.encode()),'employees.csv')},content_type='multipart/form-data')
    assert [x['status'] for x in r.json['results']]==['created','failed','failed']
    r=a.post('/admin/backup');assert r.status_code==200,r.json
    assert r.json['integrity']=='ok' and r.json['foreign_key_errors']==0
    z=zipfile.ZipFile(io.BytesIO(a.get(r.json['download_url'].removeprefix('/api')).data));assert 'minepath.sqlite3' in z.namelist()
    assert 'session-secret.txt' not in z.namelist()

def test_duty_escalation_uses_actual_shift(env):
    app,svc,clock,C=env;e=C('employee01');hid=e.post('/help',{'client_id':'duty','description':'help'}).json['id']
    with svc.tx(True) as db:
        db.execute('DELETE FROM shifts');db.execute("INSERT INTO shifts VALUES('night','all','00:00','23:59','L03',1,NULL)")
    clock[0]+=301;svc.tick()
    assert rows(env,"SELECT * FROM notifications WHERE user_id='L03' AND entity_id=?",(hid,))
    assert rows(env,"SELECT escalated FROM help_requests WHERE id=?",(hid,))[0]['escalated']==1

def test_fixture_prefix_dedup_and_upgrade(env):
    app,svc,clock,C=env;base=PROJECT/'evidence'/'simulated'/'sim-20261028-v1'
    assert svc.import_simulation(base/'positions_10000.csv','ADMIN')==10000
    assert svc.import_simulation(base/'positions_10000.csv','ADMIN')==0
    assert svc.import_simulation(base/'positions_100000.csv','ADMIN')==90000
    assert len(rows(env,'SELECT id FROM observations'))==100000
    assert max(r['observed_at'] for r in rows(env,'SELECT observed_at FROM observations'))<=clock[0]-299

def test_sse_auth_and_initial_resync(env):
    app,svc,clock,C=env;c=C('leader01');response=c.c.get('/api/stream',buffered=False)
    iterator=iter(response.response);first=next(iterator);second=next(iterator)
    assert b'retry:' in first and b'event: refresh' in second
    response.close()

def test_profile_update_cannot_change_role(env):
    e=env[3]('employee01');assert e.post('/profile',{'name':'测试员工','role':'admin'}).status_code==200
    assert e.get('/bootstrap').json['user']['name']=='测试员工'
    assert e.get('/bootstrap').json['user']['role']=='employee'

def test_repeated_route_uses_reference_segments(env):
    app,svc,clock,C=env;t=capture(env);begin=clock[0]
    emit(env,t,x=30,y=30);clock[0]+=5;emit(env,t,x=40,y=30)
    clock[0]+=15;emit(env,t,x=32,y=33);clock[0]+=5;emit(env,t,x=38,y=34);clock[0]+=5
    r=C('admin').post('/evaluations',{'user_id':'E001','source':'simulation','conditions':'two synthetic passes','test_type':'route','reference_start':begin,'reference_end':begin+10,'start':begin+20,'end':clock[0]})
    assert r.status_code==200,r.json
    result=r.json['result'];assert result['r50']==3 and result['r90']==4 and result['max_distance']==4
    assert result['reference']['segments']==1

def test_report_overlapping_session_spans_are_unioned(env):
    app,svc,clock,C=env;t=capture(env);begin=clock[0];clock[0]+=20
    with svc.tx(True) as db:
        db.execute("INSERT INTO work_sessions(id,user_id,started_at,ended_at,purpose) VALUES('overlap','E001',?,?,?)",(begin+5,begin+15,'synthetic overlapping history'))
    row=C('admin').post('/reports',{'start':begin,'end':clock[0]}).json['report']['rows'][0]
    assert row['session_count']==2 and row['session_seconds']==20

def test_photo_upload_and_case_scoped_download(env):
    from PIL import Image
    png=io.BytesIO();Image.new('RGB',(2,2),'green').save(png,format='PNG');png.seek(0)
    e=env[3]('employee01');r=e.post('/help',data={'client_id':'photo-help','description':'photo test','photo':(png,'test.png')},content_type='multipart/form-data')
    assert r.status_code==200,r.json;hid=r.json['id']
    assert e.get('/help/'+hid+'/photo').status_code==200
    assert env[3]('leader01').get('/help/'+hid+'/photo').status_code==200
    assert env[3]('leader02').get('/help/'+hid+'/photo').status_code==403

def test_mixed_batch_returns_each_result(env):
    t=capture(env);e=env[3]('employee01');now=env[2][0]
    r=e.post('/positions',{'collector_token':t['collector_token'],'points':[{'point_id':'good','observed_at':now,'x':50,'y':50,'accuracy':3},{'point_id':'bad','observed_at':now,'x':50,'y':50},{'point_id':'good','observed_at':now,'x':50,'y':50,'accuracy':3}]})
    assert r.status_code==200,r.json
    assert [p['status'] for p in r.json['results']]==['accepted','rejected','duplicate']
    assert len(rows(env,'SELECT * FROM observations'))==1

def test_inspection_reports_problems_and_proxies_help(env):
    app,svc,clock,C=env
    leader,inspector,worker=C('leader01'),C('employee01'),C('employee02')
    token=capture(env,'E002'); emit(env,token,x=50,y=50,user='E002')
    hid=worker.post('/help',{'client_id':'patrol-help','category':'现场风险','description':'设备异响'}).json['id']
    assert inspector.post('/inspections',{'user_id':'E001','area_id':'A','note':'检查A区'}).status_code==403
    assert C('leader02').post('/inspections',{'user_id':'E001','area_id':'A','note':'越权'}).status_code==403
    created=leader.post('/inspections',{'user_id':'E001','area_id':'A','note':'检查A区人员和求助'})
    assert created.status_code==200,created.json
    iid=created.json['id']
    assert all(item['id']!=hid for item in inspector.get('/help').json['items'])
    assert inspector.get('/inspections/'+iid+'/start').status_code==405
    assert inspector.get('/inspections/'+iid+'/start').json['error']
    assert inspector.post('/inspections/'+iid+'/finding',{'target':'area','problem':True,'description':'过早上报'}).status_code==409
    assert inspector.post('/inspections/'+iid+'/start').status_code==200
    detail=inspector.get('/inspections/'+iid).json
    assert any(person['id']=='E002' and person['inside_area'] for person in detail['workers'])
    assert any(item['id']==hid for item in detail['helps'])
    listed=inspector.get('/help').json
    assert listed['proxy_team']=='T01' and any(item['id']==hid and item['can_proxy'] for item in listed['items'])
    assert inspector.post('/inspections/'+iid+'/finding',{'target':'area','problem':True,'category':'现场环境','description':'通道堆有障碍物'}).status_code==200
    assert inspector.post('/inspections/'+iid+'/finding',{'target':'worker','problem':False,'subject_user_id':'E002','category':'无异常','description':'在岗，位置正常'}).status_code==200
    assert inspector.post('/inspections/'+iid+'/finding',{'target':'worker','problem':True,'subject_user_id':'E010','description':'不在现场'}).status_code==400
    assert rows(env,"SELECT kind FROM events WHERE kind='inspection_area'")
    assert inspector.post('/help/'+hid+'/action',{'action':'resolve','text':'直接结案'}).status_code==403
    assert inspector.post('/help/'+hid+'/action',{'action':'proxy_accept','text':'巡检员先到现场'}).status_code==200
    assert inspector.post('/help/'+hid+'/action',{'action':'proxy_resolve','text':'已确认设备停用'}).status_code==200
    saved=rows(env,'SELECT status,proxy_id,owner_id FROM help_requests WHERE id=?',(hid,))[0]
    assert saved=={'status':'awaiting_confirmation','proxy_id':'E001','owner_id':'L01'}
    assert inspector.post('/inspections/'+iid+'/complete').status_code==200
    assert C('employee03').post('/help/'+hid+'/action',{'action':'proxy_progress','text':'无权'}).status_code==403
    assert any(item['id']==iid and item['problem_count']==1 for item in leader.get('/inspections').json['items'])
    assert worker.post('/help/'+hid+'/action',{'action':'confirm','text':'已经恢复'}).status_code==200

def test_area_rejects_self_intersection(env):
    a=env[3]('admin');r=a.post('/admin/action',{'action':'area_version','area_id':'A','reason':'invalid geometry','polygon':[[0,0],[100,0],[0,100],[100,100],[0,50]]})
    assert r.status_code==400
    assert len(rows(env,"SELECT * FROM area_versions WHERE area_id='A'"))==1
