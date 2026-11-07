from __future__ import annotations
import csv,hashlib,json,math,secrets,sqlite3,time,uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from werkzeug.security import generate_password_hash

def uid(prefix): return prefix+'-'+uuid.uuid4().hex[:16]
def encode(value): return json.dumps(value,ensure_ascii=False,separators=(',',':'))
def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def timestamp(value):
    if isinstance(value,(int,float)):
        result=float(value)
    else:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is None: raise ValueError('时间必须包含时区')
        result=parsed.timestamp()
    if not math.isfinite(result): raise ValueError('时间无效')
    return result

class DomainError(Exception):
    def __init__(self,message,status=400): self.message,self.status=message,status

class Store:
    def __init__(self,path,clock=None):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.clock=clock or time.time
    def now(self): return float(self.clock())
    def connect(self):
        db=sqlite3.connect(self.path,timeout=20)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA busy_timeout=20000')
        return db
    @contextmanager
    def tx(self,write=False):
        db=self.connect()
        try:
            if write: db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback(); raise
        finally: db.close()
    def initialize(self,password='Demo@2026!'):
        with self.tx(True) as db:
            db.executescript((Path(__file__).parent/'schema.sql').read_text(encoding='utf-8'))
            db.execute('PRAGMA journal_mode=WAL')
            help_columns={row['name'] for row in db.execute('PRAGMA table_info(help_requests)')}
            if 'proxy_id' not in help_columns: db.execute('ALTER TABLE help_requests ADD COLUMN proxy_id TEXT')
            for i in (31,32):db.execute('INSERT OR IGNORE INTO devices VALUES(?,?,NULL,1)',(f'D{i:03}',f'备用采集端{i:02}'))
            if db.execute('SELECT count(*) FROM users').fetchone()[0]: return
            password_hash=generate_password_hash(password)
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,1)',('ADMIN','admin','系统管理员',password_hash,'admin'))
            for i in range(1,4):
                team=f'T{i:02}'; leader=f'L{i:02}'
                db.execute('INSERT INTO teams VALUES(?,?)',(team,f'第{i}作业班组'))
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,1)',(leader,f'leader{i:02}',f'班组负责人{i}',password_hash,'leader'))
                db.execute('INSERT INTO leaderships(user_id,team_id,valid_from) VALUES(?,?,0)',(leader,team))
            for i in range(1,31):
                user=f'E{i:03}'; team=f'T{(i-1)//10+1:02}'; area='ABC'[(i-1)//10]; device=f'D{i:03}'
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,1)',(user,f'employee{i:02}',f'演示员工{i:02}',password_hash,'employee'))
                db.execute('INSERT INTO memberships(user_id,team_id,valid_from) VALUES(?,?,0)',(user,team))
                db.execute('INSERT INTO devices VALUES(?,?,NULL,1)',(device,f'采集端{i:02}'))
                db.execute('INSERT INTO bindings(user_id,device_id,valid_from) VALUES(?,?,0)',(user,device))
            for i,area in enumerate('ABC'):
                db.execute('INSERT INTO areas VALUES(?,?,?)',(area,['A 加工作业区','B 仓储作业区','C 检修作业区'][i],'work'))
                x=i*200; polygon=[[x,0],[x+100,0],[x+100,100],[x,100]]
                db.execute('INSERT INTO area_versions VALUES(?,?,?,?,?,?,?)',(area+'-v1',area,encode(polygon),0,None,'ADMIN','初始示意配置'))
            db.execute('INSERT INTO areas VALUES(?,?,?)',('R01','设备检修受限区','restricted'))
            db.execute('INSERT INTO area_versions VALUES(?,?,?,?,?,?,?)',('R01-v1','R01',encode([[125,30],[150,30],[150,70],[125,70]]),0,None,'ADMIN','初始示意配置'))
            for i in range(1,31): db.execute('INSERT INTO allocations(user_id,area_id,valid_from,actor_id) VALUES(?,?,0,?)',(f'E{i:03}','ABC'[(i-1)//10],'ADMIN'))
            defaults={'gap_seconds':45,'coverage_gap_seconds':15,'position_max_age_seconds':30,'max_accuracy_m':30,'confirmation_samples':2,'escalation_seconds':300,'calibration':None,'simulation_interval_seconds':5}
            db.executemany('INSERT INTO settings VALUES(?,?)',[(k,encode(v)) for k,v in defaults.items()])
            db.execute('INSERT INTO shifts(id,name,start_time,end_time,leader_id,enabled) VALUES(?,?,?,?,?,1)',('day','日班','08:00','20:00','L01'))
    def setting(self,db,key):
        row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    def team_at(self,db,user,at=None):
        at=self.now() if at is None else at
        row=db.execute('SELECT team_id FROM memberships WHERE user_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from DESC LIMIT 1',(user,at,at)).fetchone()
        return row[0] if row else None
    def leader_teams(self,db,actor):
        now=self.now()
        return [r[0] for r in db.execute('SELECT DISTINCT team_id FROM leaderships WHERE user_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)',(actor['id'],now,now))]
    def leader_ids(self,db,team):
        now=self.now()
        return [r[0] for r in db.execute('SELECT DISTINCT l.user_id FROM leaderships l JOIN users u ON u.id=l.user_id WHERE l.team_id=? AND l.valid_from<=? AND (l.valid_to IS NULL OR l.valid_to>?) AND u.enabled=1',(team,now,now))]
    def require_team(self,db,actor,team,admin=True):
        if admin and actor['role']=='admin': return
        if actor['role']!='leader' or team not in self.leader_teams(db,actor): raise DomainError('无权管理该班组',403)
    def require_user(self,db,actor,user):
        if actor['id']==user: return
        self.require_team(db,actor,self.team_at(db,user))
    def audit(self,db,actor,action,kind,entity,data=None):
        db.execute('INSERT INTO audit(actor_id,action,entity_type,entity_id,at,data) VALUES(?,?,?,?,?,?)',(actor,action,kind,entity,self.now(),encode(data or {})))
    def notify(self,db,users,title,kind,entity):
        for user in set(users):
            db.execute('INSERT INTO notifications(user_id,title,entity_type,entity_id,created_at) VALUES(?,?,?,?,?)',(user,title,kind,entity,self.now()))
    def managed_notify(self,db,team,title,kind,entity):
        leaders=self.leader_ids(db,team)
        self.notify(db,leaders or ['ADMIN'],title,kind,entity)
    def area_at(self,db,area,at):
        return db.execute('SELECT v.*,a.name,a.kind FROM area_versions v JOIN areas a ON a.id=v.area_id WHERE v.area_id=? AND v.valid_from<=? AND (v.valid_to IS NULL OR v.valid_to>?) ORDER BY valid_from DESC LIMIT 1',(area,at,at)).fetchone()
    def session_area(self,db,session_id,at):
        row=db.execute('SELECT area_id FROM session_areas WHERE session_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from DESC LIMIT 1',(session_id,at,at)).fetchone()
        return row[0] if row else None
    def start_session(self,db,user,device_id,client_key,tab_id,area,purpose,source='browser',resume_token=None):
        now=self.now()
        binding=db.execute('SELECT b.*,d.client_key,d.enabled FROM bindings b JOIN devices d ON d.id=b.device_id WHERE b.user_id=? AND b.device_id=? AND b.valid_to IS NULL',(user,device_id)).fetchone()
        if not binding or not binding['enabled']: raise DomainError('采集端未绑定或已停用',403)
        if source!='simulation' and (not binding['client_key'] or binding['client_key']!=client_key): raise DomainError('请让管理员将本浏览器登记码绑定到采集端',409)
        lease=db.execute('SELECT * FROM leases WHERE user_id=? AND valid_to IS NULL',(user,)).fetchone()
        if lease:
            if resume_token and digest(resume_token)==lease['token_hash'] and tab_id==lease['tab_id']:
                return {'session_id':lease['session_id'],'collector_token':resume_token,'device_id':device_id,'resumed':True}
            raise DomainError('已有有效采集端，请先结束上报或由管理员换绑',409)
        allocated=db.execute('SELECT 1 FROM allocations WHERE user_id=? AND area_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)',(user,area,now,now)).fetchone()
        if not allocated: raise DomainError('只能选择已分配的作业区',403)
        work=db.execute('SELECT * FROM work_sessions WHERE user_id=? AND ended_at IS NULL',(user,)).fetchone()
        if not work:
            sid=uid('session'); db.execute('INSERT INTO work_sessions(id,user_id,started_at,purpose,last_contact_at) VALUES(?,?,?,?,?)',(sid,user,now,purpose,now))
            db.execute('INSERT INTO session_areas(session_id,area_id,valid_from,recorded_at,actor_id) VALUES(?,?,?,?,?)',(sid,area,now,now,user))
        else: sid=work['id']
        token=secrets.token_urlsafe(32)
        db.execute('INSERT INTO leases VALUES(?,?,?,?,?,?,?,?,?)',(uid('lease'),sid,user,device_id,digest(token),tab_id,now,None,source))
        self.audit(db,user,'开始采集','session',sid,{'device':device_id,'source':source})
        return {'session_id':sid,'collector_token':token,'device_id':device_id,'resumed':False}
    def end_session(self,db,sid,actor,reason):
        work=db.execute('SELECT * FROM work_sessions WHERE id=?',(sid,)).fetchone()
        if not work: raise DomainError('会话不存在',404)
        if actor['id']!=work['user_id'] and actor['role']!='admin': raise DomainError('只能本人或管理员结束会话',403)
        if not work['ended_at']:
            now=self.now(); db.execute('UPDATE work_sessions SET ended_at=?,end_reason=? WHERE id=?',(now,reason,sid))
            db.execute('UPDATE leases SET valid_to=? WHERE session_id=? AND valid_to IS NULL',(now,sid))
            db.execute("UPDATE events SET observation_state='monitoring_ended',last_at=? WHERE session_id=? AND observation_state='active'",(now,sid))
            db.execute('DELETE FROM detector_state WHERE user_id=?',(work['user_id'],))
            self.audit(db,actor['id'],'结束上报会话','session',sid,{'reason':reason})
        return dict(db.execute('SELECT * FROM work_sessions WHERE id=?',(sid,)).fetchone())

def inside(point,polygon):
    x,y=point; result=False
    for a,b in zip(polygon,polygon[1:]+polygon[:1]):
        if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]: result=not result
    return result

def boundary_distance(point,polygon):
    x,y=point; distances=[]
    for a,b in zip(polygon,polygon[1:]+polygon[:1]):
        dx,dy=b[0]-a[0],b[1]-a[1]; length=dx*dx+dy*dy
        t=max(0,min(1,((x-a[0])*dx+(y-a[1])*dy)/length)) if length else 0
        distances.append(math.hypot(x-a[0]-t*dx,y-a[1]-t*dy))
    return min(distances)

def validate_polygon(polygon):
    if not isinstance(polygon,list) or not 3<=len(polygon)<=101:raise DomainError('区域顶点数量无效')
    if any(not isinstance(p,(list,tuple)) or len(p)!=2 for p in polygon):raise DomainError('每个顶点必须包含x、y两个坐标')
    polygon=[[float(p[0]),float(p[1])] for p in polygon if isinstance(p,(list,tuple)) and len(p)==2]
    if polygon and polygon[0]==polygon[-1]:polygon.pop()
    if not 3<=len(polygon)<=100 or len({tuple(p) for p in polygon})!=len(polygon):raise DomainError('区域顶点缺失或重复')
    if not all(math.isfinite(v) and abs(v)<1000000 for p in polygon for v in p):raise DomainError('坐标无效')
    def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def on(a,b,c):return abs(cross(a,b,c))<1e-9 and min(a[0],b[0])-1e-9<=c[0]<=max(a[0],b[0])+1e-9 and min(a[1],b[1])-1e-9<=c[1]<=max(a[1],b[1])+1e-9
    edges=list(zip(polygon,polygon[1:]+polygon[:1]));n=len(edges)
    for i,(a,b) in enumerate(edges):
        for j,(c,d) in enumerate(edges):
            if j<=i or j==i+1 or i==0 and j==n-1:continue
            if cross(a,b,c)*cross(a,b,d)<0 and cross(c,d,a)*cross(c,d,b)<0 or any((on(a,b,c),on(a,b,d),on(c,d,a),on(c,d,b))):raise DomainError('区域边界不可自相交或重叠')
    if abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in edges))/2<1:raise DomainError('区域面积过小')
    return polygon
