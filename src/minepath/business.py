from __future__ import annotations
import csv,json,math,sqlite3
from datetime import datetime,timezone,timedelta
from collections import defaultdict
from pathlib import Path
from .core import Store,DomainError,uid,encode,digest,timestamp,inside,boundary_distance

class Service(Store):
    def create_event(self,db,user,team,session,kind,at,evidence,area=None,version=None):
        eid=uid('event')
        db.execute('INSERT INTO events(id,user_id,team_id,session_id,kind,area_id,area_version_id,started_at,last_at,observation_state,workflow,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (eid,user,team,session,kind,area,version,at,at,'active','pending',encode(evidence),self.now()))
        self.notify(db,[user],'有一条待说明的事件提示','event',eid)
        self.managed_notify(db,team,'有新的事件待核查','event',eid)
        return eid
    def assess(self,db,point):
        at=point['observed_at']; user=point['user_id']
        work=db.execute('SELECT last_assessed_at FROM work_sessions WHERE id=?',(point['session_id'],)).fetchone()
        if work and work[0] is not None and at<=work[0]: return 'late_review'
        if self.now()-at>self.setting(db,'position_max_age_seconds'): return 'late_review'
        if point['x'] is None or point['accuracy']>self.setting(db,'max_accuracy_m'):
            db.execute('UPDATE detector_state SET streak=0,candidate=NULL WHERE user_id=?',(user,))
            db.execute('UPDATE work_sessions SET last_assessed_at=? WHERE id=?',(at,point['session_id']))
            return 'uncertain'
        previous=work[0] if work else None
        db.execute('UPDATE work_sessions SET last_assessed_at=? WHERE id=?',(at,point['session_id']))
        transferring=db.execute("SELECT 1 FROM transfers WHERE user_id=? AND status IN ('in_transit','arrival_pending') AND started_at<=?",(user,at)).fetchone()
        zones=list(db.execute("SELECT v.*,a.kind FROM area_versions v JOIN areas a ON a.id=v.area_id WHERE a.kind='restricted' AND v.valid_from<=? AND (v.valid_to IS NULL OR v.valid_to>?)",(at,at)))
        if point['area_id']:
            version=self.area_at(db,point['area_id'],at)
            if version: zones.append(version)
        uncertain=False
        for zone in zones:
            kind='restricted' if zone['kind']=='restricted' else 'outside'
            polygon=json.loads(zone['polygon']); distance=boundary_distance((point['x'],point['y']),polygon)
            if distance<=point['accuracy']:
                uncertain=True
                db.execute('UPDATE detector_state SET streak=0,candidate=NULL WHERE user_id=? AND area_id=? AND kind=?',(user,zone['area_id'],kind))
                continue
            in_zone=inside((point['x'],point['y']),polygon)
            violation=in_zone if kind=='restricted' else not in_zone and not transferring
            value='violation' if violation else 'safe'
            state=db.execute('SELECT * FROM detector_state WHERE user_id=? AND area_id=? AND kind=?',(user,zone['area_id'],kind)).fetchone()
            active=state['active_event_id'] if state else None
            continuous=state and at-state['last_at']<=self.setting(db,'gap_seconds') and state['version_id']==zone['id']
            streak=state['streak']+1 if continuous and state['candidate']==value else 1
            if streak>=self.setting(db,'confirmation_samples'):
                if violation and not active:
                    active=self.create_event(db,user,point['team_id'],point['session_id'],kind,at,
                        {'point_id':point['point_id'],'x':point['x'],'y':point['y'],'accuracy':point['accuracy'],'source':point['source'],'area_version':zone['id'],'boundary_change':bool(state and state['version_id']!=zone['id'])},zone['area_id'],zone['id'])
                elif not violation and active:
                    db.execute("UPDATE events SET observation_state='recovered',recovered_at=?,last_at=? WHERE id=?",(at,at,active)); active=None
            if active: db.execute('UPDATE events SET last_at=? WHERE id=?',(at,active))
            db.execute('INSERT INTO detector_state VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,area_id,kind) DO UPDATE SET version_id=excluded.version_id,candidate=excluded.candidate,streak=excluded.streak,last_at=excluded.last_at,active_event_id=excluded.active_event_id',
                       (user,zone['area_id'],kind,zone['id'],value,streak,at,active))
        return 'uncertain' if uncertain else 'assessed'
    def ingest(self,db,user,token,points):
        lease=db.execute('SELECT * FROM leases WHERE token_hash=? AND user_id=?',(digest(token),user)).fetchone()
        if not lease: raise DomainError('采集授权无效',403)
        work=db.execute('SELECT * FROM work_sessions WHERE id=?',(lease['session_id'],)).fetchone()
        results=[]; now=self.now()
        for item in points:
            point_id=str(item.get('point_id',''))[:100]
            try:
                if not point_id: raise ValueError('缺少观测标识')
                existing=db.execute('SELECT id FROM observations WHERE user_id=? AND point_id=?',(user,point_id)).fetchone()
                if existing:
                    results.append({'point_id':point_id,'status':'duplicate'}); continue
                observed=timestamp(item['observed_at'])
                if observed>now+30: raise ValueError('观测时间超前')
                if observed<work['started_at']: raise ValueError('早于会话开始')
                if work['ended_at'] is not None and observed>=work['ended_at']: raise ValueError('会话已终止')
                if observed<lease['valid_from'] or lease['valid_to'] is not None and observed>=lease['valid_to']: raise ValueError('不在采集授权有效期')
                accuracy=float(item['accuracy'])
                if not math.isfinite(accuracy) or accuracy<0 or accuracy>100000: raise ValueError('不确定度无效')
                source=lease['source']; lat=lng=x=y=None
                if source=='simulation':
                    x,y=float(item['x']),float(item['y'])
                    if not all(math.isfinite(v) and abs(v)<1000000 for v in [x,y]): raise ValueError('平面坐标无效')
                else:
                    lat,lng=float(item['lat']),float(item['lng'])
                    if not math.isfinite(lat) or not math.isfinite(lng) or not -90<=lat<=90 or not -180<=lng<=180: raise ValueError('经纬度无效')
                    calibration=self.setting(db,'calibration')
                    if calibration:
                        east=math.radians(lng-calibration['lng'])*6371000*math.cos(math.radians(calibration['lat']))
                        north=math.radians(lat-calibration['lat'])*6371000; angle=math.radians(calibration.get('rotation',0))
                        x=calibration['x']+east*math.cos(angle)-north*math.sin(angle)
                        y=calibration['y']+east*math.sin(angle)+north*math.cos(angle)
                area=self.session_area(db,work['id'],observed); version=self.area_at(db,area,observed) if area else None
                point={'point_id':point_id,'user_id':user,'session_id':work['id'],'device_id':lease['device_id'],'lease_id':lease['id'],
                       'observed_at':observed,'received_at':now,'x':x,'y':y,'lat':lat,'lng':lng,'accuracy':accuracy,'source':source,
                       'team_id':self.team_at(db,user,observed),'area_id':area,'area_version_id':version['id'] if version else None}
                assessment=self.assess(db,point)
                values=[point[k] for k in ['point_id','user_id','session_id','device_id','lease_id','observed_at','received_at','x','y','lat','lng','accuracy','source','team_id','area_id','area_version_id']]
                db.execute('INSERT INTO observations(point_id,user_id,session_id,device_id,lease_id,observed_at,received_at,x,y,lat,lng,accuracy,source,team_id,area_id,area_version_id,assessment) VALUES('+','.join(['?']*17)+')',values+[assessment])
                if assessment in ('assessed','uncertain'):
                    db.execute("UPDATE events SET observation_state='recovered',recovered_at=? WHERE user_id=? AND session_id=? AND kind='reporting_gap' AND observation_state='active'",(now,user,work['id']))
                results.append({'point_id':point_id,'status':'accepted','assessment':assessment})
            except (ValueError,TypeError,KeyError) as error:
                results.append({'point_id':point_id,'status':'rejected','reason':str(error)})
        if lease['valid_to'] is None:
            db.execute('UPDATE work_sessions SET last_contact_at=? WHERE id=? AND ended_at IS NULL',(now,work['id']))
        return results
    def transfer_action(self,db,actor,task_id,action,data):
        task=db.execute('SELECT * FROM transfers WHERE id=?',(task_id,)).fetchone()
        if not task: raise DomainError('任务不存在',404)
        now=self.now(); employee=actor['id']==task['user_id']
        if action in ('start','arrive'):
            if not employee: raise DomainError('只能由本人操作',403)
        else: self.require_team(db,actor,task['owner_team_id'])
        status=task['status']
        if action=='start':
            if status!='pending': raise DomainError('任务不处于待出发状态',409)
            work=db.execute('SELECT id FROM work_sessions WHERE user_id=? AND ended_at IS NULL',(task['user_id'],)).fetchone()
            if not work: raise DomainError('请先开启上报会话',409)
            if self.session_area(db,work['id'],now)!=task['from_area']: raise DomainError('当前作业区与任务起点不符，请负责人核查任务',409)
            deadline=now+task['allowed_seconds']
            db.execute("UPDATE transfers SET status='in_transit',started_at=?,original_deadline=?,deadline=? WHERE id=?",(now,deadline,deadline,task_id))
        elif action=='arrive':
            if status!='in_transit': raise DomainError('当前不能提交到达申请',409)
            arrived=timestamp(data.get('arrived_at',now))
            if arrived<task['started_at'] or arrived>now+30: raise DomainError('到达时间无效')
            db.execute("UPDATE transfers SET status='arrival_pending',claimed_arrival_at=? WHERE id=?",(arrived,task_id))
            self.managed_notify(db,task['owner_team_id'],'员工已申报到达，等待确认','transfer',task_id)
        elif action=='extend':
            if status not in ('in_transit','arrival_pending'): raise DomainError('当前任务不能延期',409)
            reason=str(data.get('reason','')).strip(); minutes=int(data.get('minutes',0))
            if not reason or not 1<=minutes<=1440: raise DomainError('填写延期原因及1—1440分钟的延长量')
            data={**data,'previous_deadline':task['deadline'],'new_deadline':task['deadline']+minutes*60}
            db.execute('UPDATE transfers SET deadline=deadline+?,overdue_notified=0 WHERE id=?',(minutes*60,task_id))
        elif action=='confirm':
            if status!='arrival_pending': raise DomainError('没有待确认的到达申请',409)
            effective=timestamp(data.get('effective_at',task['claimed_arrival_at']))
            if not task['started_at']<=effective<=now: raise DomainError('生效时间无效')
            work=db.execute('SELECT * FROM work_sessions WHERE user_id=? AND ended_at IS NULL',(task['user_id'],)).fetchone()
            if not work: raise DomainError('当前上报会话已结束，请人工核查后重新安排',409)
            current=db.execute('SELECT * FROM session_areas WHERE session_id=? AND valid_to IS NULL',(work['id'],)).fetchone()
            if current and effective<=current['valid_from']: raise DomainError('不能覆盖已确认的更晚区域变更',409)
            db.execute('UPDATE session_areas SET valid_to=? WHERE session_id=? AND valid_to IS NULL',(effective,work['id']))
            db.execute('INSERT INTO session_areas(session_id,area_id,valid_from,recorded_at,actor_id,task_id) VALUES(?,?,?,?,?,?)',(work['id'],task['to_area'],effective,now,actor['id'],task_id))
            db.execute("UPDATE events SET observation_state='assignment_changed' WHERE user_id=? AND kind='outside' AND observation_state='active'",(task['user_id'],))
            db.execute("DELETE FROM detector_state WHERE user_id=? AND kind='outside'",(task['user_id'],))
            db.execute("UPDATE transfers SET status='completed',effective_arrival_at=?,confirmed_at=? WHERE id=?",(effective,now,task_id))
            for event in db.execute("SELECT id FROM events WHERE user_id=? AND kind='outside' AND last_at>=?",(task['user_id'],effective)):
                db.execute('INSERT INTO event_actions(event_id,actor_id,action,at,text) VALUES(?,?,?,?,?)',(event['id'],actor['id'],'area_review',now,'到达生效时间已确认；保留原提示依据，请结合新归属复核。'))
        elif action=='reject':
            if status!='arrival_pending' or not str(data.get('reason','')).strip(): raise DomainError('填写驳回原因')
            db.execute("UPDATE transfers SET status='in_transit',claimed_arrival_at=NULL WHERE id=?",(task_id,))
        elif action=='cancel':
            if status in ('cancelled','completed'): raise DomainError('任务已结束',409)
            if not data.get('reason') or not data.get('next_instruction'): raise DomainError('填写取消原因和下一步安排')
            db.execute("UPDATE transfers SET status='cancelled',reason=?,next_instruction=? WHERE id=?",(data['reason'],data['next_instruction'],task_id))
        else: raise DomainError('未知操作')
        db.execute('INSERT INTO transfer_actions(transfer_id,actor_id,action,at,data) VALUES(?,?,?,?,?)',(task_id,actor['id'],action,now,encode(data)))
        self.audit(db,actor['id'],'转场:'+action,'transfer',task_id,data)
        self.notify(db,[task['user_id']],'转场任务状态已更新','transfer',task_id)
        return dict(db.execute('SELECT * FROM transfers WHERE id=?',(task_id,)).fetchone())
    def change_team(self,db,actor,user,new_team):
        if actor['role']!='admin': raise DomainError('仅管理员可调组',403)
        if not db.execute('SELECT 1 FROM teams WHERE id=?',(new_team,)).fetchone(): raise DomainError('班组不存在')
        now=self.now(); old=self.team_at(db,user)
        if old==new_team: raise DomainError('员工已在目标班组')
        db.execute('UPDATE memberships SET valid_to=? WHERE user_id=? AND valid_to IS NULL',(now,user))
        db.execute('INSERT INTO memberships(user_id,team_id,valid_from) VALUES(?,?,?)',(user,new_team,now))
        for task in db.execute("SELECT id FROM transfers WHERE user_id=? AND status IN ('pending','in_transit','arrival_pending')",(user,)):
            db.execute('UPDATE transfers SET owner_team_id=? WHERE id=?',(new_team,task['id']))
            db.execute('INSERT INTO transfer_actions(transfer_id,actor_id,action,at,data) VALUES(?,?,?,?,?)',(task['id'],actor['id'],'handover',now,encode({'from':old,'to':new_team})))
            self.managed_notify(db,new_team,'调组员工有转场任务需要接管','transfer',task['id'])
        if not self.leader_ids(db,new_team): self.notify(db,['ADMIN'],'目标班组无可用负责人，请安排接管人员','team',new_team)
        self.audit(db,actor['id'],'员工调组','user',user,{'from':old,'to':new_team,'effective_at':now})
        self.notify(db,[user],'所属班组已调整','user',user)
    def tick(self):
        with self.tx(True) as db:
            now=self.now()
            for task in db.execute("SELECT * FROM transfers WHERE status IN ('in_transit','arrival_pending') AND deadline<? AND overdue_notified=0",(now,)).fetchall():
                self.create_event(db,task['user_id'],task['owner_team_id'],None,'transfer_timeout',task['deadline'],{'transfer_id':task['id'],'deadline':task['deadline']})
                db.execute('UPDATE transfers SET overdue_notified=1 WHERE id=?',(task['id'],))
            for work in db.execute('SELECT * FROM work_sessions WHERE ended_at IS NULL').fetchall():
                latest=db.execute('SELECT max(observed_at) FROM observations WHERE session_id=?',(work['id'],)).fetchone()[0]
                reference=latest or work['started_at']
                if now-reference>self.setting(db,'gap_seconds'):
                    if not db.execute("SELECT 1 FROM events WHERE session_id=? AND kind='reporting_gap' AND observation_state='active'",(work['id'],)).fetchone():
                        self.create_event(db,work['user_id'],self.team_at(db,work['user_id']),work['id'],'reporting_gap',reference,{'last_observed_at':latest,'threshold_seconds':self.setting(db,'gap_seconds')})
            cutoff=now-self.setting(db,'escalation_seconds')
            local=datetime.fromtimestamp(now,timezone(timedelta(hours=8))); clock=local.strftime('%H:%M'); day=local.date().isoformat()
            duty=[]
            for shift in db.execute('SELECT shifts.* FROM shifts JOIN users ON users.id=shifts.leader_id WHERE shifts.enabled=1 AND users.enabled=1'):
                active=shift['start_time']<=clock<shift['end_time'] if shift['start_time']<shift['end_time'] else clock>=shift['start_time'] or clock<shift['end_time']
                shift_day=(local.date()-timedelta(days=1)).isoformat() if shift['start_time']>=shift['end_time'] and clock<shift['end_time'] else day
                if active and (not shift['duty_date'] or shift['duty_date']==shift_day): duty.append(shift['leader_id'])
            for table,kind,column in [('events','event','created_at'),('help_requests','help','created_at')]:
                condition="workflow!='closed'" if table=='events' else "status!='closed'"
                for row in db.execute(f'SELECT id FROM {table} WHERE {column}<? AND escalated=0 AND {condition}',(cutoff,)).fetchall():
                    self.notify(db,['ADMIN']+duty,'有超时未结事项需要督办',kind,row['id'])
                    db.execute(f'UPDATE {table} SET escalated=1 WHERE id=?',(row['id'],))
    def import_simulation(self,path,actor):
        count=0
        with path.open(encoding='utf-8',newline='') as stream: data=list(csv.DictReader(stream))
        if not data: return 0
        # 1万是10万输入的前缀。固定同一数据族的会话、点ID和时间锚点，升级只新增剩余记录。
        full=path.with_name('positions_100000.csv')
        with (full if full.exists() else path).open(encoding='utf-8',newline='') as stream:
            last=max(timestamp(r['observed_at']) for r in csv.DictReader(stream))
        with self.tx(True) as db:
            saved=db.execute("SELECT value FROM settings WHERE key='fixture_time_shift'").fetchone()
            shift=json.loads(saved[0]) if saved else self.now()-300-last
            if not saved: db.execute('INSERT INTO settings VALUES(?,?)',('fixture_time_shift',encode(shift)))
            grouped=defaultdict(list)
            for row in data: grouped[row['employee_id']].append(row)
            for user,rows in grouped.items():
                sid='fixture-v1-'+user; begin=timestamp(rows[0]['observed_at'])+shift; end=timestamp(rows[-1]['observed_at'])+shift+5
                exists=db.execute('SELECT 1 FROM work_sessions WHERE id=?',(sid,)).fetchone()
                db.execute('INSERT INTO work_sessions(id,user_id,started_at,ended_at,end_reason,purpose,last_contact_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ended_at=max(ended_at,excluded.ended_at)',(sid,user,begin,end,'历史模拟导入','模拟查询测试',end))
                if not exists: db.execute('INSERT INTO session_areas(session_id,area_id,valid_from,recorded_at,actor_id) VALUES(?,?,?,?,?)',(sid,rows[0]['assigned_area_id'],begin,self.now(),actor))
                values=[]
                for r in rows:
                    observed=timestamp(r['observed_at'])+shift; version=self.area_at(db,r['assigned_area_id'],observed)
                    values.append((sid+'-'+r['observation_id'],user,sid,r['device_id'],observed,timestamp(r['received_at'])+shift,float(r['x_m']),float(r['y_m']),float(r['uncertainty_m']),'simulation',self.team_at(db,user,observed),r['assigned_area_id'],version['id'] if version else None,'imported_history',r['observed_at']))
                before=db.total_changes
                db.executemany('INSERT OR IGNORE INTO observations(point_id,user_id,session_id,device_id,observed_at,received_at,x,y,accuracy,source,team_id,area_id,area_version_id,assessment,source_observed_at) VALUES('+','.join(['?']*15)+')',values)
                count+=db.total_changes-before
            self.audit(db,actor,'导入模拟历史','dataset',path.name,{'count':count,'time_shift_seconds':shift,'sha256':__import__('hashlib').sha256(path.read_bytes()).hexdigest()})
        return count
    def list_inspections(self,db,actor):
        if actor['role']=='employee': condition,args='i.assignee_id=?',[actor['id']]
        elif actor['role']=='admin': condition,args='1=1',[]
        else:
            teams=self.leader_teams(db,actor)
            condition,args=('i.team_id IN ('+','.join('?'*len(teams))+')',teams) if teams else ('0=1',[])
        sql=('SELECT i.*,a.name area_name,u.name assignee_name,assigner.name assigner_name,'
             '(SELECT count(*) FROM inspection_findings f WHERE f.inspection_id=i.id) finding_count,'
             '(SELECT count(*) FROM inspection_findings f WHERE f.inspection_id=i.id AND f.problem=1) problem_count '
             'FROM inspections i JOIN areas a ON a.id=i.area_id JOIN users u ON u.id=i.assignee_id '
             'LEFT JOIN users assigner ON assigner.id=i.assigned_by WHERE '+condition+
             " ORDER BY (i.status IN ('assigned','in_progress')) DESC,i.created_at DESC")
        return [dict(row) for row in db.execute(sql,args)]
    def inspection_for(self,db,actor,iid):
        row=db.execute('SELECT i.*,a.name area_name,u.name assignee_name,assigner.name assigner_name FROM inspections i JOIN areas a ON a.id=i.area_id JOIN users u ON u.id=i.assignee_id LEFT JOIN users assigner ON assigner.id=i.assigned_by WHERE i.id=?',(iid,)).fetchone()
        if not row: raise DomainError('巡检任务不存在',404)
        task=dict(row)
        if actor['role']=='admin': return task
        if actor['role']=='employee':
            if task['assignee_id']!=actor['id']: raise DomainError('无权查看该巡检',403)
            return task
        self.require_team(db,actor,task['team_id'])
        return task
    def inspection_findings(self,db,iid):
        return [dict(row) for row in db.execute('SELECT f.*,u.name subject_name FROM inspection_findings f LEFT JOIN users u ON u.id=f.subject_user_id WHERE f.inspection_id=? ORDER BY f.created_at,f.rowid',(iid,))]
    def active_inspection(self,db,user_id):
        row=db.execute("SELECT * FROM inspections WHERE assignee_id=? AND status='in_progress'",(user_id,)).fetchone()
        return dict(row) if row else None
    def assign_inspection(self,db,actor,user_id,area_id,note):
        if actor['role']=='employee': raise DomainError('由负责人或管理员指派巡检',403)
        assignee=db.execute("SELECT id FROM users WHERE id=? AND role='employee' AND enabled=1",(user_id,)).fetchone()
        if not assignee: raise DomainError('请选择在职员工')
        team=self.team_at(db,user_id)
        if not team: raise DomainError('该员工尚未加入班组')
        if actor['role']!='admin': self.require_team(db,actor,team)
        if not db.execute("SELECT 1 FROM areas WHERE id=? AND kind='work'",(area_id,)).fetchone() or not self.area_at(db,area_id,self.now()):
            raise DomainError('请选择当前有效的作业区')
        if db.execute("SELECT 1 FROM inspections WHERE assignee_id=? AND status IN ('assigned','in_progress')",(user_id,)).fetchone():
            raise DomainError('该员工已有未完成的巡检，完成或撤回后再指派',409)
        iid=uid('inspect'); now=self.now(); text=str(note or '').strip()[:500]
        db.execute('INSERT INTO inspections(id,assignee_id,assigned_by,team_id,area_id,status,note,created_at) VALUES(?,?,?,?,?,?,?,?)',(iid,user_id,actor['id'],team,area_id,'assigned',text,now))
        self.audit(db,actor['id'],'指派巡检','inspection',iid,{'assignee':user_id,'area':area_id,'note':text})
        self.notify(db,[user_id],'收到巡检任务：请检查作业区和在岗人员','inspection',iid)
        self.managed_notify(db,team,'已指派员工巡检作业区','inspection',iid)
        return iid
    def start_inspection(self,db,actor,iid):
        task=self.inspection_for(db,actor,iid)
        if actor['id']!=task['assignee_id']: raise DomainError('由被指派的员工开始巡检',403)
        if task['status']!='assigned': raise DomainError('当前巡检不能开始',409)
        db.execute("UPDATE inspections SET status='in_progress',started_at=? WHERE id=?",(self.now(),iid))
        self.audit(db,actor['id'],'开始巡检','inspection',iid)
        self.managed_notify(db,task['team_id'],task['assignee_name']+'已开始巡检','inspection',iid)
    def area_workers(self,db,area_id):
        now=self.now(); version=self.area_at(db,area_id,now)
        polygon=json.loads(version['polygon']) if version else None
        gap=self.setting(db,'gap_seconds'); max_accuracy=self.setting(db,'max_accuracy_m'); people=[]
        sessions=db.execute("SELECT w.id session_id,w.user_id,u.name,u.username FROM work_sessions w JOIN users u ON u.id=w.user_id WHERE w.ended_at IS NULL AND u.role='employee' AND u.enabled=1")
        for work in sessions:
            session_area=self.session_area(db,work['session_id'],now)
            point=db.execute('SELECT x,y,lat,lng,accuracy,source,observed_at,session_id FROM observations WHERE user_id=? ORDER BY observed_at DESC,id DESC LIMIT 1',(work['user_id'],)).fetchone()
            inside_area=bool(point and point['x'] is not None and polygon and inside((point['x'],point['y']),polygon))
            if session_area!=area_id and not inside_area: continue
            if not point or point['session_id']!=work['session_id']: state='no_position'
            elif now-point['observed_at']>gap: state='interrupted'
            elif point['accuracy']>max_accuracy: state='uncertain'
            else: state='fresh'
            events=[dict(row) for row in db.execute("SELECT id,kind,observation_state,workflow,started_at FROM events WHERE user_id=? AND workflow!='closed' AND ifnull(json_extract(evidence,'$.source'),'')!='inspection' ORDER BY started_at DESC",(work['user_id'],))]
            open_help=db.execute("SELECT count(*) FROM help_requests WHERE user_id=? AND status!='closed'",(work['user_id'],)).fetchone()[0]
            people.append({'id':work['user_id'],'name':work['name'],'username':work['username'],'state':state,'inside_area':inside_area,'session_area_id':session_area,'last_observed_at':point['observed_at'] if point else None,'open_events':events,'open_help_count':open_help})
        return people
    def area_open_events(self,db,area_id,user_ids):
        slots=','.join('?'*len(user_ids)) or 'NULL'
        sql=("SELECT e.id,e.kind,e.user_id,u.name employee_name,e.observation_state,e.workflow,e.started_at,e.area_id,e.evidence FROM events e JOIN users u ON u.id=e.user_id "
             "WHERE e.workflow!='closed' AND ifnull(json_extract(e.evidence,'$.source'),'')!='inspection' AND (e.area_id=? OR e.user_id IN ("+slots+")) ORDER BY e.started_at DESC")
        items=[]
        for row in db.execute(sql,[area_id]+list(user_ids)):
            item=dict(row); item['evidence']=json.loads(item['evidence']) if item['evidence'] else {}; items.append(item)
        return items
    def team_open_helps(self,db,team_id,actor):
        items=[]
        for row in db.execute("SELECT h.*,u.name employee_name,owner.name owner_name,proxy.name proxy_name FROM help_requests h JOIN users u ON u.id=h.user_id LEFT JOIN users owner ON owner.id=h.owner_id LEFT JOIN users proxy ON proxy.id=h.proxy_id WHERE h.status!='closed' AND (h.team_id=? OR h.user_id IN (SELECT user_id FROM memberships WHERE team_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?))) ORDER BY h.created_at DESC",(team_id,team_id,self.now(),self.now())):
            item=dict(row)
            item['can_proxy']=actor['id']!=item['user_id'] and item['status'] not in ('closed','awaiting_confirmation') and (not item['proxy_id'] or item['proxy_id']==actor['id'])
            item['can_proxy_update']=item['proxy_id']==actor['id'] and item['status'] in ('processing','awaiting_confirmation')
            items.append(item)
        return items
    def add_inspection_finding(self,db,actor,iid,info):
        task=self.inspection_for(db,actor,iid)
        if actor['id']!=task['assignee_id'] or task['status']!='in_progress': raise DomainError('请先开始本人的巡检再上报',409)
        target=info.get('target'); description=str(info.get('description','')).strip()
        if target not in ('area','worker'): raise DomainError('请选择检查区域或在岗人员')
        if not description: raise DomainError('请填写检查说明')
        problem=info.get('problem')
        if not isinstance(problem,bool): raise DomainError('请标明是否发现问题')
        category=str(info.get('category') or ('发现问题' if problem else '无异常'))[:100]
        subject=info.get('subject_user_id') or None
        workers=self.area_workers(db,task['area_id'])
        if target=='worker':
            if subject not in {person['id'] for person in workers}: raise DomainError('只能记录当前正在该作业区作业的人员')
        elif subject: raise DomainError('区域检查不指定人员')
        fid=uid('finding'); now=self.now(); event_id=None
        if problem:
            event_id=uid('event'); subject_user=subject or actor['id']
            version=self.area_at(db,task['area_id'],now)
            kind='inspection_worker' if target=='worker' else 'inspection_area'
            evidence={'source':'inspection','inspection_id':iid,'finding_id':fid,'target':target,'category':category,'description':description,'reporter_id':actor['id']}
            db.execute('INSERT INTO events(id,user_id,team_id,session_id,kind,area_id,area_version_id,started_at,last_at,observation_state,workflow,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (event_id,subject_user,task['team_id'],None,kind,task['area_id'],version['id'] if version else None,now,now,'reported','pending',encode(evidence),now))
            if target=='worker': self.notify(db,[subject_user],'巡检员记录了需要你说明的现场情况','event',event_id)
            self.managed_notify(db,task['team_id'],'巡检员上报了现场问题','event',event_id)
        db.execute('INSERT INTO inspection_findings(id,inspection_id,target,subject_user_id,problem,category,description,event_id,created_at,actor_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (fid,iid,target,subject,1 if problem else 0,category,description[:4000],event_id,now,actor['id']))
        self.audit(db,actor['id'],'巡检上报','inspection',iid,{'finding':fid,'target':target,'problem':problem,'subject':subject})
        return fid
    def complete_inspection(self,db,actor,iid):
        task=self.inspection_for(db,actor,iid)
        if actor['id']!=task['assignee_id']: raise DomainError('由被指派的员工完成巡检',403)
        if task['status']!='in_progress': raise DomainError('当前巡检不能完成',409)
        if not db.execute('SELECT 1 FROM inspection_findings WHERE inspection_id=?',(iid,)).fetchone():
            raise DomainError('请先记录区域或在岗人员的检查结果',409)
        db.execute("UPDATE inspections SET status='completed',finished_at=? WHERE id=?",(self.now(),iid))
        self.audit(db,actor['id'],'完成巡检','inspection',iid)
        self.managed_notify(db,task['team_id'],task['assignee_name']+'已完成巡检','inspection',iid)
    def cancel_inspection(self,db,actor,iid,reason):
        task=self.inspection_for(db,actor,iid)
        if actor['role']=='employee': raise DomainError('由负责人或管理员撤回巡检',403)
        if task['status'] not in ('assigned','in_progress'): raise DomainError('巡检已结束',409)
        text=str(reason or '').strip()
        if not text: raise DomainError('请填写撤回原因')
        db.execute("UPDATE inspections SET status='cancelled',finished_at=? WHERE id=?",(self.now(),iid))
        self.audit(db,actor['id'],'撤回巡检','inspection',iid,{'reason':text[:500]})
        self.notify(db,[task['assignee_id']],'巡检任务已撤回','inspection',iid)
    def help_proxy_allowed(self,db,actor,case):
        task=self.active_inspection(db,actor['id'])
        if not task or case['user_id']==actor['id']: return None
        team=case['team_id'] or self.team_at(db,case['user_id'])
        return task if team==task['team_id'] else None
