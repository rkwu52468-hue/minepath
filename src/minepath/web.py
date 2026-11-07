from __future__ import annotations
import csv,io,json,math,os,secrets,sqlite3,threading,time,uuid,weakref
from collections import OrderedDict
from datetime import datetime,timezone
from pathlib import Path
from functools import wraps
from flask import Flask,request,jsonify,session,g,send_file,Response,stream_with_context
from werkzeug.security import check_password_hash,generate_password_hash
from PIL import Image
from .core import DomainError,uid,encode,timestamp
from .business import Service
from .site_maps import map_versions,register_site_maps

def create_app(project=None,database=None,clock=None,testing=False):
    project=Path(project or Path(__file__).resolve().parents[2]); data=Path(database).parent if database else project/'data'
    data.mkdir(parents=True,exist_ok=True); (data/'uploads').mkdir(exist_ok=True); (data/'backups').mkdir(exist_ok=True)
    secret_file=data/'session-secret.txt'
    if not secret_file.exists(): secret_file.write_text(secrets.token_hex(32),encoding='ascii')
    app=Flask(__name__,static_folder=str(Path(__file__).parent/'static'),static_url_path='/static')
    app.config.update(SECRET_KEY=secret_file.read_text().strip(),MAX_CONTENT_LENGTH=6*1024*1024,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',TESTING=testing)
    svc=Service(database or data/'minepath.sqlite3',clock); svc.initialize(os.environ.get('MINEPATH_DEMO_PASSWORD','Demo@2026!'))
    # 模拟器位于进程内。重启后明确结束遗留的模拟采集授权；真实浏览器会话继续保留。
    with svc.tx(True) as db:
        stale=db.execute("SELECT DISTINCT session_id FROM leases WHERE source='simulation' AND tab_id='simulator' AND valid_to IS NULL").fetchall()
        for row in stale: svc.end_session(db,row[0],{'id':'ADMIN','role':'admin'},'进程重启，结束上一轮模拟流')
    app.extensions['service']=svc; app.extensions['simulation']={'running':False,'tokens':{},'tick':0}
    # Repeated historical queries may reuse a short-lived, bounded response.
    # All business writes use other connections: compare data_version only on
    # this persistent read connection, so any committed write invalidates it.
    track_versions = sqlite3.connect(svc.path, check_same_thread=False,
                                     isolation_level=None, timeout=20)
    track_versions.execute('PRAGMA query_only=ON')
    weakref.finalize(app, track_versions.close)
    track_cache = OrderedDict()
    track_cache_lock = threading.Lock()
    track_cache_state = {'version': None}

    def read_track_cache(key):
        with track_cache_lock:
            try:
                version = track_versions.execute('PRAGMA data_version').fetchone()[0]
            except sqlite3.Error:
                track_cache.clear()
                track_cache_state['version'] = None
                return None, None
            if version != track_cache_state['version']:
                track_cache.clear()
                track_cache_state['version'] = version
            now = time.monotonic()
            for old_key in list(track_cache):
                if now - track_cache[old_key][0] > 10:
                    track_cache.pop(old_key)
            entry = track_cache.get(key)
            if entry is not None:
                track_cache.move_to_end(key)
                return entry[1], version
            return None, version

    def save_track_cache(key, version, payload):
        if version is None or len(payload) > 2 * 1024 * 1024:
            return
        with track_cache_lock:
            try:
                current = track_versions.execute('PRAGMA data_version').fetchone()[0]
            except sqlite3.Error:
                track_cache.clear()
                track_cache_state['version'] = None
                return
            if current != version:
                track_cache.clear()
                track_cache_state['version'] = current
                return  # A write raced with the query: do not cache that response.
            track_cache[key] = (time.monotonic(), payload)
            track_cache.move_to_end(key)
            while len(track_cache) > 6:
                track_cache.popitem(last=False)

    login_failures={}; login_lock=threading.Lock()

    @app.errorhandler(DomainError)
    def domain_error(error): return jsonify(error=error.message),error.status
    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    @app.errorhandler(KeyError)
    def bad_input(error): return jsonify(error='输入格式无效：'+str(error)),400
    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(error): return jsonify(error='记录冲突，请检查账号、任务或当前状态'),409
    @app.errorhandler(413)
    def too_large(error): return jsonify(error='文件或请求过大，照片与底图均不超过5MB'),413
    @app.errorhandler(405)
    def method_not_allowed(error): return jsonify(error='请求方式不正确'),405
    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['X-Frame-Options']='DENY'
        if request.path.startswith('/api/'): response.headers['Cache-Control']='no-store'
        return response
    @app.before_request
    def authentication():
        g.user=None
        if session.get('user_id'):
            with svc.tx() as db:
                row=db.execute('SELECT id,username,name,role,enabled FROM users WHERE id=?',(session['user_id'],)).fetchone()
                if row and row['enabled']: g.user=dict(row)
        session.setdefault('csrf',secrets.token_urlsafe(24))
        if request.path.startswith('/api/') and request.path not in ('/api/bootstrap','/api/login','/api/health') and not g.user: raise DomainError('请先登录',401)
        if request.path.startswith('/api/') and request.method not in ('GET','HEAD','OPTIONS'):
            token=request.headers.get('X-CSRF-Token','')
            if not secrets.compare_digest(token,session['csrf']): raise DomainError('请求校验失败，请刷新页面',403)
    def admin():
        if not g.user or g.user['role']!='admin': raise DomainError('仅管理员可操作',403)
    def body(): return request.get_json(silent=True) or {}
    def scope(db,alias='o'):
        if g.user['role']=='admin': return '1=1',[]
        if g.user['role']=='employee': return f'{alias}.user_id=?',[g.user['id']]
        teams=svc.leader_teams(db,g.user)
        if not teams: return '0=1',[]
        return f'{alias}.team_id IN ('+','.join('?'*len(teams))+')',teams
    def current_users(db):
        people=[dict(r) for r in db.execute("SELECT id,username,name,enabled FROM users WHERE role='employee' ORDER BY username")]
        teams=svc.leader_teams(db,g.user) if g.user['role']=='leader' else None
        return [p for p in people if g.user['role']=='admin' or p['id']==g.user['id'] or teams is not None and svc.team_at(db,p['id']) in teams]
    def case_visible(db,case):
        if not case: raise DomainError('求助不存在',404)
        proxy_id=case['proxy_id'] if 'proxy_id' in case.keys() else None
        if g.user['role']=='admin' or g.user['id'] in (case['user_id'],case['owner_id'],proxy_id): return
        if g.user['role']=='leader' and (case['team_id'] in svc.leader_teams(db,g.user) or svc.team_at(db,case['user_id']) in svc.leader_teams(db,g.user)): return
        if g.user['role']=='employee' and svc.help_proxy_allowed(db,g.user,case): return
        raise DomainError('无权查看该求助',403)

    @app.get('/')
    def index(): return send_file(Path(app.static_folder)/'index.html')
    @app.get('/api/health')
    def health(): return jsonify(status='ok',server_time=svc.now(),sqlite_version=sqlite3.sqlite_version)
    @app.get('/api/bootstrap')
    def bootstrap():
        result={'user':g.user,'csrf':session['csrf'],'server_time':svc.now(),'simulation_running':app.extensions['simulation']['running']}
        if g.user:
            with svc.tx() as db:
                result['site_maps']=map_versions(db)
                result['teams']=[dict(r) for r in db.execute('SELECT * FROM teams')]
                result['managed_teams']=svc.leader_teams(db,g.user)
                result['areas']=[dict(r) for r in db.execute('SELECT a.*,v.id version_id,v.polygon,v.valid_from FROM areas a JOIN area_versions v ON v.area_id=a.id WHERE v.valid_from<=? AND (v.valid_to IS NULL OR v.valid_to>?)',(svc.now(),svc.now()))]
                result['area_history']=[dict(r) for r in db.execute('SELECT a.id,a.name,a.kind,v.id version_id,v.polygon,v.valid_from,v.valid_to FROM areas a JOIN area_versions v ON v.area_id=a.id ORDER BY valid_from')]
                people=current_users(db)
                if g.user['role']=='leader':
                    permission,args=scope(db)
                    ids={p['id'] for p in people}
                    for row in db.execute('SELECT DISTINCT u.id,u.username,u.name,u.enabled FROM observations o JOIN users u ON u.id=o.user_id WHERE '+permission,args):
                        if row['id'] not in ids: people.append({**dict(row),'historical_only':True}); ids.add(row['id'])
                result['employees']=people
                result['settings']={r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM settings')}
                result['binding']=[dict(r) for r in db.execute('SELECT b.device_id,d.label,d.client_key FROM bindings b JOIN devices d ON d.id=b.device_id WHERE b.user_id=? AND b.valid_to IS NULL',(g.user['id'],))]
                result['allocations']=[r[0] for r in db.execute('SELECT area_id FROM allocations WHERE user_id=? AND valid_to IS NULL',(g.user['id'],))]
                work=db.execute('SELECT * FROM work_sessions WHERE user_id=? AND ended_at IS NULL',(g.user['id'],)).fetchone()
                result['work_session']=dict(work) if work else None
        return jsonify(result)
    @app.post('/api/login')
    def login():
        info=body(); key=request.remote_addr or 'local'; moment=time.monotonic()
        with login_lock:
            failures=[t for t in login_failures.get(key,[]) if moment-t<60]
            if len(failures)>=12: raise DomainError('尝试过于频繁，请稍后再试',429)
            login_failures[key]=failures
        with svc.tx() as db: user=db.execute('SELECT * FROM users WHERE username=?',(str(info.get('username',''))[:100],)).fetchone()
        if not user or not user['enabled'] or not check_password_hash(user['password_hash'],str(info.get('password',''))):
            with login_lock: login_failures[key].append(moment)
            raise DomainError('账号或密码不正确，或账号已停用',401)
        session.clear(); session['user_id']=user['id']; session['csrf']=secrets.token_urlsafe(24)
        return jsonify(ok=True,csrf=session['csrf'])
    @app.post('/api/logout')
    def logout(): session.clear(); return jsonify(ok=True)
    @app.post('/api/password')
    def password():
        info=body()
        with svc.tx(True) as db:
            user=db.execute('SELECT * FROM users WHERE id=?',(g.user['id'],)).fetchone()
            if not check_password_hash(user['password_hash'],info.get('old_password','')): raise DomainError('原密码不正确')
            if len(info.get('new_password',''))<10: raise DomainError('新密码至少10位')
            db.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(info['new_password']),g.user['id']))
            svc.audit(db,g.user['id'],'修改本人密码','user',g.user['id'])
        return jsonify(ok=True)
    @app.post('/api/profile')
    def update_profile():
        name=str(body().get('name','')).strip()
        if not 1<=len(name)<=100: raise DomainError('姓名长度为1—100字')
        with svc.tx(True) as db:
            db.execute('UPDATE users SET name=? WHERE id=?',(name,g.user['id']))
            svc.audit(db,g.user['id'],'修改本人信息','user',g.user['id'],{'name':name})
        return jsonify(ok=True)
    @app.get('/api/monitor')
    def monitor():
        result=[]
        with svc.tx() as db:
            for person in current_users(db):
                point=db.execute('SELECT * FROM observations WHERE user_id=? ORDER BY observed_at DESC,id DESC LIMIT 1',(person['id'],)).fetchone()
                if point and g.user['role']=='leader' and point['team_id'] not in svc.leader_teams(db,g.user): point=None
                work=db.execute('SELECT * FROM work_sessions WHERE user_id=? AND ended_at IS NULL',(person['id'],)).fetchone()
                age=svc.now()-point['observed_at'] if point else None
                state='not_reporting' if not work else 'no_position' if not point or point['session_id']!=work['id'] else 'interrupted' if age>svc.setting(db,'gap_seconds') else 'uncertain' if point['accuracy']>svc.setting(db,'max_accuracy_m') else 'fresh'
                transfer=db.execute("SELECT id,status,to_area,deadline FROM transfers WHERE user_id=? AND status IN ('pending','in_transit','arrival_pending')",(person['id'],)).fetchone()
                result.append({**person,'team_id':svc.team_at(db,person['id']),'state':state,'last_position':dict(point) if point else None,'work_session':dict(work) if work else None,'transfer':dict(transfer) if transfer else None,'contact_age_seconds':svc.now()-work['last_contact_at'] if work and work['last_contact_at'] else None})
            permission,args=scope(db,'e')
            event_count=db.execute("SELECT count(*) FROM events e WHERE workflow!='closed' AND "+permission,args).fetchone()[0]
            unread=db.execute('SELECT count(*) FROM notifications WHERE user_id=? AND read_at IS NULL',(g.user['id'],)).fetchone()[0]
            site_maps=map_versions(db)
            area_history=[dict(r) for r in db.execute('SELECT a.id,a.name,a.kind,v.id version_id,v.polygon,v.valid_from,v.valid_to FROM areas a JOIN area_versions v ON v.area_id=a.id ORDER BY valid_from')]
        return jsonify(people=result,open_events=event_count,unread=unread,server_time=svc.now(),site_maps=site_maps,area_history=area_history)
    @app.get('/api/stream')
    def stream():
        user_id=g.user['id']
        @stream_with_context
        def generate():
            last=None; deadline=time.monotonic()+25
            yield 'retry: 2500\n\n'
            while time.monotonic()<deadline:
                with svc.tx() as db:
                    user=db.execute('SELECT enabled FROM users WHERE id=?',(user_id,)).fetchone()
                    if not user or not user[0]: break
                    revision=(db.execute('SELECT coalesce(max(id),0) FROM observations').fetchone()[0],db.execute('SELECT coalesce(max(id),0) FROM audit').fetchone()[0],db.execute('SELECT coalesce(max(id),0) FROM notifications WHERE user_id=?',(user_id,)).fetchone()[0])
                if revision!=last:
                    yield 'id: '+str(revision[0])+'-'+str(revision[1])+'-'+str(revision[2])+'\nevent: refresh\ndata: '+encode({'server_time':svc.now()})+'\n\n'; last=revision
                else: yield ': heartbeat\n\n'
                time.sleep(2)
        return Response(generate(),mimetype='text/event-stream',headers={'X-Accel-Buffering':'no'})
    @app.post('/api/sessions/start')
    def start_session():
        if g.user['role']!='employee': raise DomainError('只有员工可开始采集',403)
        info=body()
        with svc.tx(True) as db: result=svc.start_session(db,g.user['id'],info['device_id'],info.get('client_key'),info.get('tab_id'),info['area_id'],str(info.get('purpose',''))[:500],resume_token=info.get('collector_token'))
        return jsonify(result)
    @app.post('/api/sessions/<sid>/end')
    def end_session(sid):
        reason=str(body().get('reason','本人结束上报'))[:500]
        if not reason.strip(): raise DomainError('请填写结束原因')
        with svc.tx(True) as db:
            result=svc.end_session(db,sid,g.user,reason)
            if body().get('client_requested_at') is not None: svc.audit(db,g.user['id'],'客户端结束请求时间','session',sid,{'client_requested_at':timestamp(body()['client_requested_at']),'server_ack_at':svc.now()})
        return jsonify(result)
    @app.post('/api/positions')
    def positions():
        if g.user['role']!='employee': raise DomainError('只有员工可上报',403)
        info=body(); points=info.get('points',[])
        if not isinstance(points,list) or not 1<=len(points)<=500: raise DomainError('每批1—500条观测')
        with svc.tx(True) as db: results=svc.ingest(db,g.user['id'],info.get('collector_token',''),points)
        return jsonify(results=results,server_time=svc.now())
    @app.post('/api/heartbeat')
    def heartbeat():
        with svc.tx(True) as db: db.execute('UPDATE work_sessions SET last_contact_at=? WHERE user_id=? AND ended_at IS NULL',(svc.now(),g.user['id']))
        return jsonify(ok=True)
    @app.get('/api/tracks')
    def tracks():
        user=request.args.get('user_id',g.user['id']); start=float(request.args.get('start',svc.now()-86400)); end=float(request.args.get('end',svc.now()+1))
        limit=max(1,min(50000,int(request.args.get('limit',10000)))); after_time=float(request.args.get('after_time',-1)); after_id=int(request.args.get('after_id',0))
        if not all(math.isfinite(v) for v in (start,end,after_time)) or end<=start:raise DomainError('查询时段或游标无效')
        with svc.tx() as db:
            permission,args=scope(db)
            cache_key = (g.user['id'], g.user['role'], tuple(args), user,
                         start, end, limit, after_time, after_id)
            cached, version = read_track_cache(cache_key)
            if cached is not None:
                return Response(cached, mimetype='application/json',
                                headers={'X-Track-Cache': 'hit'})
            params=[user,start,end,after_time,after_time,after_id]+args+[limit+1]
            observation_cursor = db.execute('SELECT o.* FROM observations o WHERE o.user_id=? AND o.observed_at>=? AND o.observed_at<? AND (o.observed_at>? OR (o.observed_at=? AND o.id>?)) AND '+permission+' ORDER BY o.observed_at,o.id LIMIT ?', params)
            observation_cursor.row_factory = None
            columns = tuple(column[0] for column in observation_cursor.description)
            rows = [dict(zip(columns, values)) for values in observation_cursor.fetchall()]
            more=len(rows)>limit; rows=rows[:limit]; points=[]
            # Load effective assignments once per page instead of querying for every point.
            # Descending start + ascending row id preserves the original lookup's tie order.
            session_ids = list(dict.fromkeys(row['session_id'] for row in rows))
            assignments = {sid: [] for sid in session_ids}
            if rows:
                first_at, last_at = rows[0]['observed_at'], rows[-1]['observed_at']
                for offset in range(0, len(session_ids), 900):
                    batch = session_ids[offset:offset + 900]
                    slots = ','.join('?' for _ in batch)
                    for assignment in db.execute(
                        'SELECT session_id,area_id,valid_from,valid_to FROM session_areas '
                        'WHERE session_id IN (' + slots + ') AND valid_from<=? '
                        'AND (valid_to IS NULL OR valid_to>?) '
                        'ORDER BY valid_from DESC,id ASC', batch + [last_at, first_at]
                    ):
                        assignments[assignment['session_id']].append(
                            (assignment['valid_from'], assignment['valid_to'], assignment['area_id'])
                        )
            for row in rows:
                point = row
                observed_at = row['observed_at']
                point['effective_area_id'] = next(
                    (area for begin, end, area in assignments[row['session_id']]
                     if begin <= observed_at and (end is None or observed_at < end)), None
                )
                points.append(point)
            sessions=[dict(r) for r in db.execute('SELECT o.session_id id,min(o.observed_at) first_visible_at,max(o.observed_at) last_visible_at FROM observations o WHERE o.user_id=? AND o.observed_at>=? AND o.observed_at<? AND '+permission+' GROUP BY o.session_id ORDER BY first_visible_at DESC LIMIT 100',[user,start,end]+args)]
            gap=svc.setting(db,'coverage_gap_seconds')
        cursor={'after_time':points[-1]['observed_at'],'after_id':points[-1]['id']} if more and points else None
        # JSON object key order is not part of the API; avoid sorting every observation.
        payload = encode({'points': points, 'sessions': sessions,
                          'next_cursor': cursor, 'segment_gap_seconds': gap}).encode('utf-8')
        save_track_cache(cache_key, version, payload)
        return Response(payload, mimetype='application/json',
                        headers={'X-Track-Cache': 'miss'})
    @app.get('/api/transfers')
    def transfers():
        with svc.tx() as db:
            if g.user['role']=='employee': condition='t.user_id=?'; args=[g.user['id']]
            elif g.user['role']=='admin': condition='1=1'; args=[]
            else:
                teams=svc.leader_teams(db,g.user); condition='t.owner_team_id IN ('+','.join('?'*len(teams))+')' if teams else '0=1'; args=teams
            tasks=[dict(r) for r in db.execute("SELECT t.*,u.name employee_name FROM transfers t JOIN users u ON u.id=t.user_id WHERE "+condition+" ORDER BY (t.status IN ('pending','in_transit','arrival_pending')) DESC,created_at DESC LIMIT 300",args)]
            for task in tasks: task['actions']=[dict(r) for r in db.execute('SELECT * FROM transfer_actions WHERE transfer_id=? ORDER BY at,id',(task['id'],))]
        return jsonify(items=tasks)
    @app.post('/api/transfers')
    def issue_transfer():
        info=body(); user=info['user_id']; allowed=int(info.get('allowed_minutes',0))*60
        if not 60<=allowed<=86400: raise DomainError('允许时长为1—1440分钟')
        with svc.tx(True) as db:
            team=svc.team_at(db,user); svc.require_team(db,g.user,team)
            for area in [info['from_area'],info['to_area']]:
                if not db.execute("SELECT 1 FROM areas WHERE id=? AND kind='work'",(area,)).fetchone(): raise DomainError('起终点必须是作业区')
            if info['from_area']==info['to_area']: raise DomainError('起终点不能相同')
            tid=uid('transfer')
            db.execute('INSERT INTO transfers(id,user_id,from_area,to_area,origin_team_id,owner_team_id,created_by,created_at,allowed_seconds,status) VALUES(?,?,?,?,?,?,?,?,?,?)',(tid,user,info['from_area'],info['to_area'],team,team,g.user['id'],svc.now(),allowed,'pending'))
            svc.audit(db,g.user['id'],'下达转场任务','transfer',tid,info); svc.notify(db,[user],'收到新的转场任务','transfer',tid)
        return jsonify(id=tid)
    @app.post('/api/transfers/<tid>/<action>')
    def transfer_action(tid,action):
        with svc.tx(True) as db: result=svc.transfer_action(db,g.user,tid,action,body())
        return jsonify(result)
    @app.get('/api/events')
    def events():
        with svc.tx() as db:
            condition,args=scope(db,'e')
            items=[dict(r) for r in db.execute("SELECT e.*,u.name employee_name FROM events e JOIN users u ON u.id=e.user_id WHERE "+condition+" ORDER BY (workflow!='closed') DESC,created_at DESC LIMIT 400",args)]
            for item in items:
                item['evidence']=json.loads(item['evidence']); item['actions']=[dict(r) for r in db.execute('SELECT * FROM event_actions WHERE event_id=? ORDER BY at,id',(item['id'],))]
        return jsonify(items=items)
    @app.post('/api/events/<eid>/action')
    def event_action(eid):
        info=body(); action=info.get('action'); text=str(info.get('text','')).strip()
        if not text: raise DomainError('请填写说明、调查结果或处理措施')
        with svc.tx(True) as db:
            event=db.execute('SELECT * FROM events WHERE id=?',(eid,)).fetchone()
            if not event: raise DomainError('事件不存在',404)
            if action=='explain':
                if g.user['id']!=event['user_id']: raise DomainError('只能提交本人说明',403)
            else:
                svc.require_team(db,g.user,event['team_id'])
                if action not in ('investigate','close'): raise DomainError('未知操作')
                db.execute('UPDATE events SET workflow=? WHERE id=?',('closed' if action=='close' else 'investigating',eid))
            db.execute('INSERT INTO event_actions(event_id,actor_id,action,at,text) VALUES(?,?,?,?,?)',(eid,g.user['id'],action,svc.now(),text[:4000]))
            svc.audit(db,g.user['id'],'事件:'+action,'event',eid,{'text':text[:4000]})
            if action=='explain':svc.managed_notify(db,event['team_id'],'员工补充了事件说明','event',eid)
            else:svc.notify(db,[event['user_id']],'事件核查进度已更新','event',eid)
        return jsonify(ok=True)
    @app.get('/api/help')
    def helps():
        with svc.tx() as db:
            items=[]; active=svc.active_inspection(db,g.user['id']) if g.user['role']=='employee' else None
            if g.user['role']=='admin':condition='1=1';params=[]
            elif g.user['role']=='employee':
                condition='(h.user_id=? OR h.proxy_id=?'+( " OR (h.team_id=? AND h.status!='closed')" if active else '')+')'
                params=[g.user['id'],g.user['id']]+([active['team_id']] if active else [])
            else:
                teams=svc.leader_teams(db,g.user);slots=','.join('?'*len(teams)) or 'NULL'
                condition=f'(h.owner_id=? OR h.team_id IN ({slots}) OR EXISTS(SELECT 1 FROM memberships m WHERE m.user_id=h.user_id AND m.team_id IN ({slots}) AND m.valid_from<=? AND (m.valid_to IS NULL OR m.valid_to>?)))'
                params=[g.user['id']]+teams+teams+[svc.now(),svc.now()]
            for row in db.execute("SELECT h.*,u.name employee_name,owner.name owner_name,proxy.name proxy_name FROM help_requests h JOIN users u ON u.id=h.user_id LEFT JOIN users owner ON owner.id=h.owner_id LEFT JOIN users proxy ON proxy.id=h.proxy_id WHERE "+condition+" ORDER BY (h.status!='closed') DESC,h.created_at DESC LIMIT 500",params):
                try: case_visible(db,row)
                except DomainError: continue
                item=dict(row); item['position']=json.loads(item['position_json']) if item['position_json'] else None
                item['can_proxy']=bool(active and item['user_id']!=g.user['id'] and item['status'] not in ('closed','awaiting_confirmation') and (not item['proxy_id'] or item['proxy_id']==g.user['id']) and (item['team_id']==active['team_id'] or svc.team_at(db,item['user_id'])==active['team_id']))
                item['can_proxy_update']=item['proxy_id']==g.user['id'] and item['status'] in ('processing','awaiting_confirmation')
                item['actions']=[dict(r) for r in db.execute('SELECT * FROM help_actions WHERE help_id=? ORDER BY at,id',(item['id'],))]; items.append(item)
        return jsonify(items=items,proxy_team=active['team_id'] if active else None)
    @app.post('/api/help')
    def submit_help():
        if g.user['role']!='employee': raise DomainError('请用员工账号发起求助',403)
        info=request.form if request.mimetype=='multipart/form-data' else body()
        description=str(info.get('description','')).strip(); client_id=str(info.get('client_id',''))[:100]
        if not description or not client_id: raise DomainError('填写求助内容')
        with svc.tx(True) as db:
            existing=db.execute('SELECT id FROM help_requests WHERE user_id=? AND client_id=?',(g.user['id'],client_id)).fetchone()
            if existing: return jsonify(id=existing[0],duplicate=True)
            photo=None; upload=request.files.get('photo')
            if upload and upload.filename:
                raw=upload.read(5*1024*1024+1)
                if len(raw)>5*1024*1024: raise DomainError('照片不能超过5MB')
                try:
                    image=Image.open(io.BytesIO(raw)); image.verify()
                    if image.format not in ('PNG','JPEG','WEBP'): raise ValueError()
                    if image.width*image.height>20000000: raise ValueError()
                except Exception: raise DomainError('只接受有效的PNG、JPEG或WebP照片')
                photo=uid('photo')+'.'+{'PNG':'png','JPEG':'jpg','WEBP':'webp'}[image.format]
                (data/'uploads'/photo).write_bytes(raw)
            hid=uid('help'); now=svc.now(); team=svc.team_at(db,g.user['id'])
            point=db.execute('SELECT observed_at,x,y,lat,lng,accuracy,source FROM observations WHERE user_id=? ORDER BY observed_at DESC LIMIT 1',(g.user['id'],)).fetchone()
            position=encode(dict(point)) if point and now-point['observed_at']<=svc.setting(db,'position_max_age_seconds') else None
            db.execute('INSERT INTO help_requests(id,client_id,user_id,team_id,category,description,photo,position_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(hid,client_id,g.user['id'],team,str(info.get('category','其他'))[:100],description[:4000],photo,position,'pending',now,now))
            svc.managed_notify(db,team,'收到新的求助请求','help',hid); svc.audit(db,g.user['id'],'发起求助','help',hid)
        return jsonify(id=hid)
    @app.get('/api/help/<hid>/photo')
    def photo(hid):
        with svc.tx() as db:
            case=db.execute('SELECT * FROM help_requests WHERE id=?',(hid,)).fetchone(); case_visible(db,case)
            if not case['photo']: raise DomainError('没有照片',404)
            path=data/'uploads'/case['photo']
        return send_file(path)
    @app.post('/api/help/<hid>/action')
    def help_action(hid):
        info=body(); action=info.get('action'); text=str(info.get('text','')).strip()
        with svc.tx(True) as db:
            case=db.execute('SELECT * FROM help_requests WHERE id=?',(hid,)).fetchone(); case_visible(db,case)
            if action in ('confirm','reopen'):
                if g.user['id']!=case['user_id'] or case['status']!='awaiting_confirmation': raise DomainError('当前不能确认或反馈结果',409)
                db.execute('UPDATE help_requests SET status=? WHERE id=?',('closed' if action=='confirm' else 'processing',hid))
            elif action=='assign':
                admin(); owner=info.get('owner_id')
                if not db.execute("SELECT 1 FROM users WHERE id=? AND role='leader' AND enabled=1",(owner,)).fetchone(): raise DomainError('请选择有效管理人员')
                if not text: raise DomainError('填写交接原因')
                db.execute("UPDATE help_requests SET owner_id=?,proxy_id=NULL,status=CASE WHEN status='pending' THEN 'processing' ELSE status END WHERE id=?",(owner,hid))
            elif action=='accept':
                if case['owner_id']: raise DomainError('已有处理人',409)
                svc.require_team(db,g.user,case['team_id'],admin=False)
                db.execute("UPDATE help_requests SET owner_id=?,status='processing' WHERE id=?",(g.user['id'],hid))
            elif action=='assist':
                if g.user['role']!='leader' or not text: raise DomainError('由负责人填写协助记录',403)
            elif action in ('progress','resolve'):
                if g.user['id']!=case['owner_id'] or not text: raise DomainError('由当前处理人填写进展和结果',403)
                if case['status'] not in ('processing','awaiting_confirmation'): raise DomainError('求助状态不允许此操作',409)
                if action=='resolve': db.execute("UPDATE help_requests SET status='awaiting_confirmation' WHERE id=?",(hid,))
            elif action=='proxy_accept':
                task=svc.help_proxy_allowed(db,g.user,case)
                if not task: raise DomainError('只有正在巡检的本组员工可以代组长处理',403)
                if case['status'] in ('closed','awaiting_confirmation'): raise DomainError('当前求助不能代接单',409)
                if case['proxy_id'] and case['proxy_id']!=g.user['id']: raise DomainError('已有其他巡检员在代处理',409)
                leaders=svc.leader_ids(db,case['team_id'] or task['team_id'])
                owner=case['owner_id'] or (task['assigned_by'] if task['assigned_by'] in leaders else (leaders[0] if leaders else None))
                if not text: text='巡检员代组长接单'
                db.execute("UPDATE help_requests SET owner_id=?,proxy_id=?,status='processing' WHERE id=?",(owner,g.user['id'],hid))
            elif action in ('proxy_progress','proxy_resolve'):
                if g.user['id']!=case['proxy_id'] or not text: raise DomainError('由代处理的巡检员填写进展和结果',403)
                if case['status'] not in ('processing','awaiting_confirmation'): raise DomainError('求助状态不允许此操作',409)
                if action=='proxy_resolve': db.execute("UPDATE help_requests SET status='awaiting_confirmation' WHERE id=?",(hid,))
            else: raise DomainError('未知操作')
            db.execute('UPDATE help_requests SET updated_at=? WHERE id=?',(svc.now(),hid))
            db.execute('INSERT INTO help_actions(help_id,actor_id,action,at,text) VALUES(?,?,?,?,?)',(hid,g.user['id'],action,svc.now(),text[:4000]))
            svc.audit(db,g.user['id'],'求助:'+action,'help',hid,{'text':text[:4000]})
            current=db.execute('SELECT owner_id,proxy_id FROM help_requests WHERE id=?',(hid,)).fetchone()
            recipients=[case['user_id'],current['owner_id'],current['proxy_id'],case['owner_id'],case['proxy_id']]
            svc.notify(db,[user for user in recipients if user],'求助进度已更新','help',hid)
        return jsonify(ok=True)
    @app.get('/api/inspections')
    def inspections():
        with svc.tx() as db: items=svc.list_inspections(db,g.user)
        return jsonify(items=items)
    @app.post('/api/inspections')
    def create_inspection():
        info=body()
        with svc.tx(True) as db: iid=svc.assign_inspection(db,g.user,info.get('user_id'),info.get('area_id'),info.get('note',''))
        return jsonify(id=iid)
    @app.get('/api/inspections/<iid>')
    def inspection_detail(iid):
        with svc.tx() as db:
            item=svc.inspection_for(db,g.user,iid); item['findings']=svc.inspection_findings(db,iid)
            if item['status']=='in_progress':
                item['workers']=svc.area_workers(db,item['area_id'])
                item['area_events']=svc.area_open_events(db,item['area_id'],[person['id'] for person in item['workers']])
                item['helps']=svc.team_open_helps(db,item['team_id'],g.user)
            else: item['workers']=[]; item['area_events']=[]; item['helps']=[]
        return jsonify(item)
    @app.post('/api/inspections/<iid>/start')
    def start_inspection(iid):
        with svc.tx(True) as db: svc.start_inspection(db,g.user,iid)
        return jsonify(ok=True)
    @app.post('/api/inspections/<iid>/finding')
    def inspection_finding(iid):
        with svc.tx(True) as db: fid=svc.add_inspection_finding(db,g.user,iid,body())
        return jsonify(id=fid)
    @app.post('/api/inspections/<iid>/complete')
    def complete_inspection(iid):
        with svc.tx(True) as db: svc.complete_inspection(db,g.user,iid)
        return jsonify(ok=True)
    @app.post('/api/inspections/<iid>/cancel')
    def cancel_inspection(iid):
        with svc.tx(True) as db: svc.cancel_inspection(db,g.user,iid,body().get('reason',''))
        return jsonify(ok=True)
    @app.get('/api/notifications')
    def notifications():
        with svc.tx() as db: items=[dict(r) for r in db.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 200',(g.user['id'],))]
        return jsonify(items=items)
    @app.post('/api/notifications/read')
    def read_notifications():
        with svc.tx(True) as db:
            ids=body().get('ids',[])
            if ids: db.executemany('UPDATE notifications SET read_at=? WHERE id=? AND user_id=?',[(svc.now(),int(i),g.user['id']) for i in ids[:200]])
        return jsonify(ok=True)

    # Administration, reporting and demo routes are registered below.
    from .management import register_management
    register_management(app,svc,data,project,admin,body,scope,current_users)
    register_site_maps(app,svc,admin,body)
    return app
