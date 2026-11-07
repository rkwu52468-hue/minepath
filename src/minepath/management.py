from __future__ import annotations
import csv,io,json,math,secrets,sqlite3,threading,time,zipfile
from collections import defaultdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
from flask import request,g,jsonify,send_file,Response
from werkzeug.security import generate_password_hash
from .core import DomainError,uid,encode,timestamp,validate_polygon

def interval_seconds(intervals):
    total=0;left=right=None
    for begin,end in sorted(intervals):
        if end<=begin: continue
        if left is None:left,right=begin,end
        elif begin<=right:right=max(right,end)
        else:total+=right-left;left,right=begin,end
    return total+(right-left if left is not None else 0)

def build_report(svc,db,actor,start,end):
    if not math.isfinite(start) or not math.isfinite(end) or end<=start: raise DomainError('统计时段无效')
    teams=svc.leader_teams(db,actor) if actor['role']=='leader' else None
    users=db.execute("SELECT id,name FROM users WHERE role='employee' ORDER BY username").fetchall(); result=[]
    for user in users:
        if actor['role']=='employee' and actor['id']!=user['id']: continue
        memberships=[dict(r) for r in db.execute('SELECT * FROM memberships WHERE user_id=? AND valid_from<? AND (valid_to IS NULL OR valid_to>?)',(user['id'],end,start)) if teams is None or r['team_id'] in teams]
        if not memberships: continue
        spans=[]
        for work in db.execute('SELECT * FROM work_sessions WHERE user_id=? AND started_at<? AND (ended_at IS NULL OR ended_at>?)',(user['id'],end,start)):
            for member in memberships:
                left=max(start,work['started_at'],member['valid_from']); right=min(end,work['ended_at'] or svc.now(),member['valid_to'] or end)
                if right>left: spans.append((left,right,work['id']))
        points=[dict(r) for r in db.execute('SELECT observed_at,session_id,accuracy,source,team_id FROM observations WHERE user_id=? AND observed_at>=? AND observed_at<? ORDER BY observed_at,id',(user['id'],start,end)) if teams is None or r['team_id'] in teams]
        coverage_intervals=[]; gaps=0; previous_points={}
        for point in points:
            series=(point['session_id'],point['source'],point['team_id']);previous=previous_points.get(series)
            if previous and point['session_id']==previous['session_id'] and point['source']==previous['source']:
                delta=point['observed_at']-previous['observed_at']
                if 0<delta<=svc.setting(db,'coverage_gap_seconds') and max(point['accuracy'],previous['accuracy'])<=svc.setting(db,'max_accuracy_m'):
                    for left,right,sid in spans:
                        if sid==point['session_id']: coverage_intervals.append((max(left,previous['observed_at']),min(right,point['observed_at'])))
                elif delta>svc.setting(db,'coverage_gap_seconds'): gaps+=1
            previous_points[series]=point
        duration=interval_seconds([(l,r) for l,r,_ in spans]);covered=min(interval_seconds(coverage_intervals),duration)
        condition='user_id=? AND started_at>=? AND started_at<?'; params=[user['id'],start,end]
        if teams is not None:
            condition+=' AND team_id IN ('+','.join('?'*len(teams))+')' if teams else ' AND 0=1'; params+=teams
        event_count=db.execute('SELECT count(*) FROM events WHERE '+condition,params).fetchone()[0]
        result.append({'user_id':user['id'],'name':user['name'],'session_count':len({sid for _,_,sid in spans}),'session_seconds':round(duration,3),'coverage_seconds':round(covered,3),'missing_seconds':round(max(0,duration-covered),3),'observed_gap_count':gaps,'point_count':len(points),'simulated_points':sum(p['source']=='simulation' for p in points),'browser_points':sum(p['source']=='browser' for p in points),'event_count':event_count})
    return {'start':start,'end':end,'generated_at':svc.now(),'generated_by':actor['id'],'rows':result,'coverage_rule':{'max_pair_gap_seconds':svc.setting(db,'coverage_gap_seconds'),'max_uncertainty_m':svc.setting(db,'max_accuracy_m'),'method':'有效相邻观测的时间区间并集；不填补长缺口，不跨会话或数据来源连接'},'notice':'仅反映上报活动；模拟与浏览器声明来源分别计数，不等同考勤或工作时长。'}

def register_management(app,svc,data,project,admin,body,scope,current_users):
    @app.get('/api/admin/state')
    def state():
        admin()
        with svc.tx() as db:
            return jsonify(users=[dict(r) for r in db.execute('SELECT id,username,name,role,enabled FROM users ORDER BY role,username')],
                memberships=[dict(r) for r in db.execute('SELECT * FROM memberships ORDER BY valid_from')],
                leaderships=[dict(r) for r in db.execute('SELECT * FROM leaderships ORDER BY valid_from')],
                devices=[dict(r) for r in db.execute('SELECT d.*,b.user_id FROM devices d LEFT JOIN bindings b ON b.device_id=d.id AND b.valid_to IS NULL')],
                shifts=[dict(r) for r in db.execute('SELECT * FROM shifts')],
                area_versions=[dict(r) for r in db.execute('SELECT v.*,a.name,a.kind FROM area_versions v JOIN areas a ON a.id=v.area_id ORDER BY valid_from DESC')],
                counts={'positions':db.execute('SELECT count(*) FROM observations').fetchone()[0],'audit':db.execute('SELECT count(*) FROM audit').fetchone()[0]})
    @app.post('/api/allocations')
    def allocate():
        info=body(); user=info['user_id']; area=info['area_id']
        with svc.tx(True) as db:
            svc.require_team(db,g.user,svc.team_at(db,user))
            if not db.execute("SELECT 1 FROM areas WHERE id=? AND kind='work'",(area,)).fetchone(): raise DomainError('作业区不存在')
            if not db.execute('SELECT 1 FROM allocations WHERE user_id=? AND area_id=? AND valid_to IS NULL',(user,area)).fetchone():
                db.execute('INSERT INTO allocations(user_id,area_id,valid_from,actor_id) VALUES(?,?,?,?)',(user,area,svc.now(),g.user['id']))
            svc.notify(db,[user],'作业区分配已更新','allocation',area); svc.audit(db,g.user['id'],'分配作业区','user',user,{'area':area})
        return jsonify(ok=True)
    @app.post('/api/admin/action')
    def manage():
        admin(); info=body(); action=info.get('action'); now=svc.now(); entity=info.get('user_id') or info.get('team_id') or info.get('area_id') or ''
        with svc.tx(True) as db:
            if action=='team_change': svc.change_team(db,g.user,info['user_id'],info['team_id'])
            elif action=='team_create':
                team=uid('team'); db.execute('INSERT INTO teams VALUES(?,?)',(team,str(info['name'])[:100])); entity=team
            elif action=='leader_assign':
                user=info['leader_id']; team=info['team_id']
                if not db.execute("SELECT 1 FROM users WHERE id=? AND role='leader' AND enabled=1",(user,)).fetchone(): raise DomainError('管理人员不可用')
                if info.get('replace',True): db.execute('UPDATE leaderships SET valid_to=? WHERE team_id=? AND valid_to IS NULL',(now,team))
                db.execute('INSERT INTO leaderships(user_id,team_id,valid_from,temporary) VALUES(?,?,?,?)',(user,team,now,int(bool(info.get('temporary',False)))))
                svc.notify(db,[user],'收到班组管理委派','team',team)
            elif action=='user_enable':
                if info['user_id']==g.user['id']: raise DomainError('不能停用当前管理员')
                enabled=int(bool(info.get('enabled')))
                db.execute('UPDATE users SET enabled=? WHERE id=?',(enabled,info['user_id']))
                if not enabled:
                    db.execute('UPDATE leases SET valid_to=? WHERE user_id=? AND valid_to IS NULL',(now,info['user_id']))
                    svc.notify(db,['ADMIN'],'账号已停用，请检查其未完成事项并安排接管','user',info['user_id'])
            elif action=='reset_password':
                if len(info.get('password',''))<10: raise DomainError('密码至少10位')
                db.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(info['password']),info['user_id']))
            elif action=='user_create':
                role=info.get('role','employee'); password=info.get('password','')
                if role not in ('employee','leader') or len(password)<10 or not str(info.get('username','')).strip() or not str(info.get('name','')).strip(): raise DomainError('角色、姓名、账号或密码无效')
                user=uid('user'); entity=user
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,1)',(user,str(info['username'])[:80],str(info['name'])[:100],generate_password_hash(password),role))
                if role=='employee':
                    if info.get('area_id') and not db.execute("SELECT 1 FROM areas WHERE id=? AND kind='work'",(info['area_id'],)).fetchone(): raise DomainError('请选择作业区')
                    db.execute('INSERT INTO memberships(user_id,team_id,valid_from) VALUES(?,?,?)',(user,info['team_id'],now))
                    if info.get('area_id'): db.execute('INSERT INTO allocations(user_id,area_id,valid_from,actor_id) VALUES(?,?,?,?)',(user,info['area_id'],now,g.user['id']))
                    device=uid('device'); db.execute('INSERT INTO devices VALUES(?,?,NULL,1)',(device,info.get('device_label','新采集端')))
                    db.execute('INSERT INTO bindings(user_id,device_id,valid_from) VALUES(?,?,?)',(user,device,now))
            elif action=='device_bind':
                user=info['user_id']; device=info.get('device_id') or uid('device'); client=str(info.get('client_key','')).strip()
                if not client or len(client)>100 or not info.get('reason'): raise DomainError('填写浏览器登记码和换绑原因')
                occupied=db.execute('SELECT user_id FROM bindings WHERE device_id=? AND valid_to IS NULL AND user_id!=?',(device,user)).fetchone()
                if occupied: raise DomainError('采集端已绑定其他员工')
                db.execute('INSERT INTO devices(id,label,client_key) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET client_key=excluded.client_key,label=excluded.label,enabled=1',(device,info.get('label','浏览器采集端'),client))
                db.execute('UPDATE bindings SET valid_to=? WHERE user_id=? AND valid_to IS NULL',(now,user))
                db.execute('UPDATE leases SET valid_to=? WHERE user_id=? AND valid_to IS NULL',(now,user))
                db.execute('INSERT INTO bindings(user_id,device_id,valid_from) VALUES(?,?,?)',(user,device,now))
                svc.notify(db,[user],'采集端已换绑，请在新端继续上报','device',device)
            elif action=='area_version':
                area=str(info.get('area_id') or uid('area')); entity=area
                polygon=validate_polygon(info['polygon'])
                kind=info.get('kind','work')
                if kind not in ('work','restricted'): raise DomainError('区域类别无效')
                if not info.get('reason'): raise DomainError('填写修改原因')
                db.execute('INSERT OR IGNORE INTO areas VALUES(?,?,?)',(area,str(info.get('name',area))[:100],kind))
                db.execute('UPDATE area_versions SET valid_to=? WHERE area_id=? AND valid_to IS NULL',(now,area))
                db.execute('INSERT INTO area_versions VALUES(?,?,?,?,?,?,?)',(uid(area+'-v'),area,encode(polygon),now,None,g.user['id'],str(info['reason'])[:500]))
                svc.notify(db,[r[0] for r in db.execute("SELECT id FROM users WHERE enabled=1")],'区域配置已有新版本，请注意作业范围','area',area)
            elif action=='settings':
                bounds={'gap_seconds':(10,3600),'coverage_gap_seconds':(1,600),'position_max_age_seconds':(5,3600),'max_accuracy_m':(1,500),'confirmation_samples':(1,10),'escalation_seconds':(30,86400),'simulation_interval_seconds':(1,60)}
                for key,value in info.get('values',{}).items():
                    if key not in bounds or not bounds[key][0]<=float(value)<=bounds[key][1]: raise DomainError('设置超出范围：'+key)
                    db.execute('UPDATE settings SET value=? WHERE key=?',(encode(int(value)),key))
            elif action=='calibration':
                value={k:float(info[k]) for k in ('lat','lng','x','y','rotation')}
                if not all(math.isfinite(v) for v in value.values()) or not -90<value['lat']<90 or not -180<=value['lng']<=180: raise DomainError('标定参数无效')
                db.execute("UPDATE settings SET value=? WHERE key='calibration'",(encode(value),))
            elif action=='shift':
                for key in ('start_time','end_time'):
                    try: datetime.strptime(info[key],'%H:%M')
                    except Exception: raise DomainError('班次时间格式应为HH:MM')
                sid=info.get('id') or uid('shift')
                db.execute('INSERT INTO shifts VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,start_time=excluded.start_time,end_time=excluded.end_time,leader_id=excluded.leader_id,enabled=excluded.enabled,duty_date=excluded.duty_date',(sid,info['name'],info['start_time'],info['end_time'],info['leader_id'],int(info.get('enabled',True)),info.get('duty_date') or None))
            else: raise DomainError('未知管理操作')
            safe={k:v for k,v in info.items() if 'password' not in k}
            svc.audit(db,g.user['id'],action,'management',entity,safe)
        return jsonify(ok=True)
    @app.post('/api/admin/import-employees')
    def import_employees():
        admin(); file=request.files.get('file')
        if not file: raise DomainError('请选择CSV文件')
        try: reader=csv.DictReader(io.StringIO(file.read().decode('utf-8-sig')))
        except UnicodeError: raise DomainError('CSV必须为UTF-8编码')
        rows=list(reader)
        if len(rows)>500: raise DomainError('单次最多500行')
        results=[]
        for number,row in enumerate(rows,2):
            try:
                with svc.tx(True) as db:
                    if not all(row.get(k) for k in ('username','name','team_id','area_id','password')): raise DomainError('缺少username,name,team_id,area_id,password字段')
                    if len(row['password'])<10: raise DomainError('密码至少10位')
                    if not db.execute("SELECT 1 FROM areas WHERE id=? AND kind='work'",(row['area_id'],)).fetchone(): raise DomainError('作业区不存在')
                    user=uid('user'); device=uid('device')
                    db.execute('INSERT INTO users VALUES(?,?,?,?,?,1)',(user,row['username'][:80],row['name'][:100],generate_password_hash(row['password']),'employee'))
                    db.execute('INSERT INTO memberships(user_id,team_id,valid_from) VALUES(?,?,?)',(user,row['team_id'],svc.now()))
                    db.execute('INSERT INTO allocations(user_id,area_id,valid_from,actor_id) VALUES(?,?,?,?)',(user,row['area_id'],svc.now(),g.user['id']))
                    db.execute('INSERT INTO devices VALUES(?,?,?,1)',(device,row.get('device_label','导入采集端'),row.get('client_key') or None))
                    db.execute('INSERT INTO bindings(user_id,device_id,valid_from) VALUES(?,?,?)',(user,device,svc.now()))
                    svc.audit(db,g.user['id'],'CSV创建员工','user',user)
                results.append({'line':number,'status':'created','user_id':user,'device_id':device})
            except (DomainError,sqlite3.IntegrityError) as error: results.append({'line':number,'status':'failed','reason':error.message if isinstance(error,DomainError) else '账号重复或班组/区域不存在'})
        return jsonify(results=results)
    @app.get('/api/audit')
    def audit():
        admin()
        with svc.tx() as db: items=[dict(r) for r in db.execute('SELECT a.*,u.name actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 500')]
        return jsonify(items=items)
    @app.post('/api/reports')
    def reports():
        info=body(); start=timestamp(info.get('start',svc.now()-86400)); end=timestamp(info.get('end',svc.now()))
        with svc.tx(True) as db:
            result=build_report(svc,db,g.user,start,end); rid=uid('report')
            db.execute('INSERT INTO report_snapshots VALUES(?,?,?,?)',(rid,g.user['id'],svc.now(),encode(result)))
            svc.audit(db,g.user['id'],'生成报表快照','report',rid,{'start':start,'end':end})
        return jsonify(id=rid,report=result)
    @app.get('/api/reports')
    def report_list():
        with svc.tx() as db:
            if g.user['role']=='admin': rows=db.execute('SELECT id,created_by,created_at FROM report_snapshots ORDER BY created_at DESC LIMIT 100')
            else: rows=db.execute('SELECT id,created_by,created_at FROM report_snapshots WHERE created_by=? ORDER BY created_at DESC LIMIT 100',(g.user['id'],))
            return jsonify(items=[dict(r) for r in rows])
    def load_snapshot(rid):
        with svc.tx() as db: row=db.execute('SELECT * FROM report_snapshots WHERE id=?',(rid,)).fetchone()
        if not row or g.user['role']!='admin' and row['created_by']!=g.user['id']: raise DomainError('无权查看该快照',403)
        return json.loads(row['body'])
    @app.get('/api/reports/<rid>')
    def report_detail(rid): return jsonify(load_snapshot(rid))
    @app.get('/api/reports/<rid>.csv')
    def report_csv(rid):
        report=load_snapshot(rid); stream=io.StringIO(); fields=['user_id','name','session_count','session_seconds','coverage_seconds','missing_seconds','observed_gap_count','point_count','simulated_points','browser_points','event_count']
        writer=csv.DictWriter(stream,fields); writer.writeheader()
        for row in report['rows']: writer.writerow({k:("'"+str(v) if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()})
        return Response('\ufeff'+stream.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename="{rid}.csv"'})
    @app.post('/api/evaluations')
    def evaluation():
        admin(); info=body(); user=info['user_id']; start=timestamp(info['start']); end=timestamp(info['end']); source=info.get('source','browser')
        if source not in ('browser','simulation') or not info.get('conditions'): raise DomainError('填写测试条件并选择来源')
        with svc.tx(True) as db:
            points=[dict(r) for r in db.execute('SELECT point_id,x,y,accuracy,observed_at,session_id,team_id FROM observations WHERE user_id=? AND observed_at>=? AND observed_at<? AND source=? ORDER BY observed_at,id',(user,start,end,source))]
            valid=[p for p in points if p['x'] is not None and p['y'] is not None and p['accuracy']<=svc.setting(db,'max_accuracy_m')]
            if len(valid)<2: raise DomainError('所选时段至少需要2个已标定且质量合格的样本')
            cx=sum(p['x'] for p in valid)/len(valid); cy=sum(p['y'] for p in valid)/len(valid)
            distances=sorted(math.hypot(p['x']-cx,p['y']-cy) for p in valid)
            reference=None
            method='相对算术样本中心的距离，最近秩分位数；固定点样本的重复性描述。'
            if info.get('test_type')=='route':
                if not info.get('reference_start') or not info.get('reference_end'):raise DomainError('重复路线评估需要选择独立的参考趟次时段')
                rs=timestamp(info['reference_start']);re=timestamp(info['reference_end'])
                if re<=rs or not (re<=start or end<=rs):raise DomainError('参考与待比较时段须有效且不重叠')
                refs=[dict(r) for r in db.execute('SELECT point_id,x,y,accuracy,observed_at,session_id,team_id FROM observations WHERE user_id=? AND observed_at>=? AND observed_at<? AND source=? AND x IS NOT NULL AND accuracy<=? ORDER BY observed_at,id',(user,rs,re,source,svc.setting(db,'max_accuracy_m')))]
                segments=[(a,b) for a,b in zip(refs,refs[1:]) if a['session_id']==b['session_id'] and a['team_id']==b['team_id'] and 0<b['observed_at']-a['observed_at']<=svc.setting(db,'coverage_gap_seconds')]
                if not segments:raise DomainError('参考趟次缺少连续有效路段，不能跨观测缺口连接')
                def distance(p,a,b):
                    dx=b['x']-a['x'];dy=b['y']-a['y'];length=dx*dx+dy*dy
                    t=max(0,min(1,((p['x']-a['x'])*dx+(p['y']-a['y'])*dy)/length)) if length else 0
                    return math.hypot(p['x']-a['x']-t*dx,p['y']-a['y']-t*dy)
                distances=sorted(min(distance(p,a,b) for a,b in segments) for p in valid)
                reference={'start':rs,'end':re,'samples':len(refs),'segments':len(segments),'point_ids_sha256':__import__('hashlib').sha256(encode([p['point_id'] for p in refs]).encode()).hexdigest()}
                method='待比较趟次各点到参考趟次有效折线路段的最短平面距离，最近秩分位数；参考长缺口不连接。单向几何比较，非时间同步或绝对定位误差；须人工确认两趟为同一路线。'
            planned=int(info['planned_samples']) if info.get('planned_samples') else None
            if planned is not None and planned<len(points): raise DomainError('计划样本数不能小于已取得样本数')
            result={'user_id':user,'start':start,'end':end,'source':source,'test_type':info.get('test_type','stationary'),'conditions':info['conditions'],'valid_samples':len(valid),'low_quality_samples':len(points)-len(valid),'planned_samples':planned,'missing_samples':planned-len(points) if planned is not None else None,'center_x':cx,'center_y':cy,'r50':distances[math.ceil(.5*len(distances))-1],'r90':distances[math.ceil(.9*len(distances))-1],'max_distance':distances[-1],'method':'相对算术样本中心的距离，最近秩分位数；重复路线全程汇总反映路线分布，应按同一路段/参考点另做对齐比较。','notice':'模拟来源不代表实测。上述指标不证明绝对定位精度。未提供计划样本数时不推测缺失样本数量。'}
            result.update(method=method,reference=reference,total_samples=len(points),uncalibrated_samples=sum(p['x'] is None for p in points),low_quality_samples=sum(p['accuracy']>svc.setting(db,'max_accuracy_m') for p in points),point_ids_sha256=__import__('hashlib').sha256(encode([p['point_id'] for p in valid]).encode()).hexdigest())
            eid=uid('evaluation'); db.execute('INSERT INTO evaluations VALUES(?,?,?,?)',(eid,g.user['id'],svc.now(),encode(result))); svc.audit(db,g.user['id'],'定位重复性评测','evaluation',eid)
        return jsonify(id=eid,result=result)
    @app.get('/api/evaluations')
    def evaluations():
        admin()
        with svc.tx() as db: return jsonify(items=[{'id':r['id'],'created_at':r['created_at'],'result':json.loads(r['body'])} for r in db.execute('SELECT * FROM evaluations ORDER BY created_at DESC LIMIT 100')])
    @app.post('/api/admin/backup')
    def backup():
        admin(); bid=uid('backup'); dbfile=data/'backups'/(bid+'.sqlite3')
        original=svc.connect(); destination=sqlite3.connect(dbfile)
        try:
            original.backup(destination)
            destination.execute('PRAGMA journal_mode=DELETE')
        finally: destination.close(); original.close()
        with sqlite3.connect(dbfile) as check:
            integrity=check.execute('PRAGMA integrity_check').fetchone()[0]; fk=check.execute('PRAGMA foreign_key_check').fetchall()
        if integrity!='ok' or fk: raise DomainError('备份恢复校验未通过',500)
        archive=data/'backups'/(bid+'.zip')
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(dbfile,'minepath.sqlite3')
            for image in (data/'uploads').iterdir():
                if image.is_file(): bundle.write(image,'uploads/'+image.name)
            bundle.writestr('manifest.json',encode({'backup_id':bid,'created_at':svc.now(),'restore_check':integrity,'foreign_key_errors':len(fk),'note':'恢复到独立SQLite连接并完成结构校验，未覆盖运行中的数据库。会话签名密钥不在包内。'}))
        with svc.tx(True) as db: svc.audit(db,g.user['id'],'备份与恢复校验','backup',bid)
        return jsonify(id=bid,integrity=integrity,foreign_key_errors=len(fk),download_url='/api/admin/backups/'+bid)
    @app.get('/api/admin/backups/<bid>')
    def backup_download(bid):
        admin()
        if not bid.startswith('backup-') or not all(c.isalnum() or c=='-' for c in bid): raise DomainError('备份标识无效')
        path=data/'backups'/(bid+'.zip')
        if not path.is_file(): raise DomainError('备份不存在',404)
        return send_file(path,as_attachment=True)
    @app.post('/api/admin/import-simulation')
    def import_simulation():
        admin(); size=int(body().get('count',10000))
        if size not in (10000,100000): raise DomainError('选择1万或10万条输入')
        file=project/'evidence'/'simulated'/'sim-20261028-v1'/f'positions_{size}.csv'
        if not file.exists(): raise DomainError('模拟输入文件不存在',404)
        return jsonify(imported=svc.import_simulation(file,g.user['id']),source='simulation')
    @app.post('/api/admin/maintenance')
    def maintenance(): admin(); svc.tick(); return jsonify(ok=True)
    @app.post('/api/admin/simulation')
    def simulation():
        admin(); mode=body().get('running'); simulator=app.extensions['simulation']
        if mode and not simulator['running']:
            tokens={}
            with svc.tx(True) as db:
                for person in db.execute("SELECT id FROM users WHERE id GLOB 'E[0-9][0-9][0-9]' AND enabled=1").fetchall():
                    user=person['id']; binding=db.execute('SELECT device_id FROM bindings WHERE user_id=? AND valid_to IS NULL',(user,)).fetchone()
                    allocation=db.execute('SELECT area_id FROM allocations WHERE user_id=? AND valid_to IS NULL ORDER BY valid_from DESC LIMIT 1',(user,)).fetchone()
                    if not binding or not allocation: continue
                    try: tokens[user]=svc.start_session(db,user,binding[0],None,'simulator',allocation[0],'30人模拟演示','simulation')
                    except DomainError: continue
                svc.audit(db,g.user['id'],'开启模拟数据流','simulation','30-streams',{'active_collectors':len(tokens),'source':'simulation'})
            simulator.update(running=True,tokens=tokens,tick=0)
        elif not mode:
            simulator['running']=False
            with svc.tx(True) as db:
                for token in list(simulator['tokens'].values()): svc.end_session(db,token['session_id'],g.user,'停止模拟演示')
                svc.audit(db,g.user['id'],'停止模拟数据流','simulation','30-streams')
            simulator['tokens']={}
        return jsonify(running=simulator['running'],collectors=len(simulator['tokens']))

def run_background(app,stop):
    svc=app.extensions['service']; last=0
    while not stop.wait(1):
        try:
            if svc.now()-last<1: continue
            svc.tick(); simulator=app.extensions['simulation']
            with svc.tx() as config_db: interval=svc.setting(config_db,'simulation_interval_seconds')
            if simulator['running'] and svc.now()-simulator.get('last_emit',0)>=interval:
                simulator['last_emit']=svc.now(); simulator['tick']+=1; tick=simulator['tick']
                with svc.tx(True) as db:
                    for user,token in list(simulator['tokens'].items()):
                        number=int(user[1:])-1; group=number//10
                        x=group*200+35+12*math.sin(tick/7+number); y=50+15*math.cos(tick/9+number)
                        if number==0 and tick%40 in range(10,20): x,y=137+math.sin(tick),50
                        try: svc.ingest(db,user,token['collector_token'],[{'point_id':f'live-{user}-{uid("p")}','observed_at':svc.now(),'x':x,'y':y,'accuracy':3}])
                        except DomainError: continue
            last=svc.now()
        except Exception:
            app.logger.exception('维护或模拟轮次失败，等待下一轮；未伪造结果')
