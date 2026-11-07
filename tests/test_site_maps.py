"""Frozen map acceptance contract. Do not change during the experiment."""
import io,json,sqlite3,subprocess,sys,zipfile,os,shutil
from pathlib import Path
import pytest
from PIL import Image
from test_system import env,template,rows

ROOT=Path(__file__).resolve().parents[1]
NODE=os.environ.get('MINEPATH_NODE') or shutil.which('node')

def image(fmt='PNG',size=(160,100)):
    f=io.BytesIO();Image.new('RGB',size,'#b7cba5').save(f,format=fmt);f.seek(0);return f

def upload(c,base='',fmt='PNG',**values):
    data={'file':(image(fmt),'site.png' if fmt=='PNG' else 'site.jpg'),'name':'验收场区图','origin_x':'-20','origin_y':'-10','meters_per_pixel':'2','reason':'固定验收地图','base_id':base}
    data.update(values)
    return c.post('/admin/site-map',data,content_type='multipart/form-data')

def new_map(env):
    c=env[3]('admin');r=upload(c);assert r.status_code==200,r.json;return c,r.json['map']

def node(code):
    assert NODE, 'Node.js is required for geometry checks; install Node.js or set MINEPATH_NODE'
    module=ROOT/'src/minepath/static/site-map.js'
    assert module.is_file(),'map interaction module is missing'
    r=subprocess.run([NODE,'-e',"const assert=require('node:assert/strict');const M=require("+json.dumps(str(module))+');'+code],capture_output=True,text=True,encoding='utf-8')
    assert r.returncode==0,r.stdout+r.stderr

def test_01_upload_png_and_jpeg(env):
    c,m=new_map(env)
    assert (m['width_px'],m['height_px'])==(160,100)
    r=c.get(m['image_url'].removeprefix('/api'))
    assert r.status_code==200 and r.mimetype=='image/png'
    assert Image.open(io.BytesIO(r.data)).size==(160,100)
    env[2][0]+=1
    j=upload(c,m['id'],'JPEG');assert j.status_code==200,j.json
    assert j.json['map']['id']!=m['id']

def test_02_reject_nonimages_oversize_and_unsafe_formats(env):
    c=env[3]('admin')
    for file in [(io.BytesIO(b'<svg onload="alert(1)"/>'),'fake.png'),(image('GIF'),'site.gif'),(image(size=(4097,16)),'wide.png'),(io.BytesIO(b'x'*(5*1024*1024+1)),'large.png')]:
        r=upload(c,file=file)
        assert r.status_code in (400,413),r.status_code
    assert c.get('/site-maps').json['versions']==[]

def test_03_permissions_csrf_and_private_image(env):
    admin,m=new_map(env)
    app=env[0];guest=app.test_client()
    assert guest.get(m['image_url']).status_code==401
    assert guest.get('/api/site-maps').status_code==401
    assert admin.c.post('/api/admin/site-map',json={}).status_code==403
    for user in ('employee01','leader01'):
        c=env[3](user)
        assert upload(c).status_code==403
        assert c.get(m['image_url'].removeprefix('/api')).status_code==200
        assert c.get('/site-maps').status_code==200

def test_04_placement_revisions_and_stale_edit_conflict(env):
    c,m=new_map(env);env[2][0]+=5
    info={'base_id':m['id'],'name':'移动底图','origin_x':10,'origin_y':20,'meters_per_pixel':0.5,'reason':'调整图片定位'}
    r=c.post('/admin/site-map',info);assert r.status_code==200,r.json
    n=r.json['map'];assert n['image_url']==m['image_url']
    assert n['bounds']==[[20,10],[70,90]]
    assert c.post('/admin/site-map',info).status_code==409
    assert c.get('/site-maps').json['current']['id']==n['id']

def test_05_invalid_scale_origin_reason_are_atomic(env):
    c,m=new_map(env)
    base=dict(base_id=m['id'],name='invalid',origin_x=0,origin_y=0,meters_per_pixel=1,reason='invalid placement')
    for change in ({'meters_per_pixel':0},{'meters_per_pixel':-1},{'meters_per_pixel':'NaN'},{'origin_x':'Infinity'},{'origin_y':1000000},{'reason':' '},{'meters_per_pixel':10000}):
        r=c.post('/admin/site-map',{**base,**change})
        assert r.status_code==400,(change,r.status_code)
    d=c.get('/site-maps').json
    assert d['current']['id']==m['id'] and len(d['versions'])==1

def test_06_restart_keeps_image_and_geometry(env):
    c,m=new_map(env);before=c.get(m['image_url'].removeprefix('/api')).data
    from minepath import create_app
    app=create_app(ROOT,env[1].path,clock=lambda:env[2][0],testing=True)
    fresh=app.test_client();b=fresh.get('/api/bootstrap').json
    r=fresh.post('/api/login',json={'username':'admin','password':'Demo@2026!'},headers={'X-CSRF-Token':b['csrf']});assert r.status_code==200
    d=fresh.get('/api/site-maps').json
    assert d['current']['id']==m['id'] and d['current']['bounds']==m['bounds']
    assert fresh.get(m['image_url']).data==before

def test_07_versions_current_monitor_and_bootstrap_share_metadata(env):
    c,m=new_map(env);old=env[2][0];env[2][0]+=10
    r=upload(c,m['id']);assert r.status_code==200,r.json
    maps=c.get('/site-maps').json;assert len(maps['versions'])==2
    v=next(x for x in maps['versions'] if x['id']==m['id'])
    assert v['valid_from']==old and v['valid_to']==env[2][0]
    assert c.get('/bootstrap').json['site_maps']==maps['versions']
    assert c.get('/monitor').json['site_maps']==maps['versions']
    assert all('image_data' not in x for x in maps['versions'])

def test_08_js_pixel_world_roundtrip_and_historical_selection():
    node("""
    const m={width_px:160,height_px:100,origin_x:-20,origin_y:-10,meters_per_pixel:2};
    assert.deepEqual(M.pixelToWorld(m,[0,100]),[-20,-10]);
    assert.deepEqual(M.pixelToWorld(m,[160,0]),[300,190]);
    for(const p of [[0,0],[160,100],[35.5,47.25]])assert.deepEqual(M.worldToPixel(m,M.pixelToWorld(m,p)),p);
    const vs=[{id:'a',valid_from:10,valid_to:20},{id:'b',valid_from:20,valid_to:null}];
    assert.equal(M.selectMap(vs,19).id,'a');assert.equal(M.selectMap(vs,20).id,'b');
    assert.equal(M.selectMap(vs,5),null);assert.equal(M.selectMap(vs).id,'b');
    assert.throws(()=>M.pixelToWorld({...m,meters_per_pixel:0},[0,0]));
    """)

def test_09_js_draw_drag_undo_and_snapshot_isolation():
    node("""
    const d=new M.PolygonDraft();d.add([0,0]);d.add([100,0]);d.add([100,100]);d.add([0,100]);
    d.move(1,[120,0]);assert.deepEqual(d.points()[1],[120,0]);
    const p=d.points();p[0][0]=999;assert.deepEqual(d.points()[0],[0,0]);
    d.undo();assert.equal(d.points().length,3);d.add([0,100]);d.remove(1);assert.equal(d.points().length,3);
    d.load([[1,2],[5,2],[5,8]]);assert.deepEqual(d.points(),[[1,2],[5,2],[5,8]]);
    d.clear();assert.equal(d.points().length,0);assert.throws(()=>d.add([NaN,0]));
    """)

def test_10_drawn_region_save_and_edit_preserve_time_versions(env):
    c=env[3]('admin');poly=[[0,0],[120,0],[120,80],[0,80]]
    r=c.post('/admin/action',dict(action='area_version',area_id='DRAW',name='图上作业区',kind='work',polygon=poly,reason='鼠标绘制'))
    assert r.status_code==200,r.json
    first=env[2][0];env[2][0]+=10
    updated=[[0,0],[140,0],[120,80],[0,80]]
    assert c.post('/admin/action',dict(action='area_version',area_id='DRAW',polygon=updated,reason='拖动顶点')).status_code==200
    with env[1].tx() as db:
        assert json.loads(env[1].area_at(db,'DRAW',first)['polygon'])==poly
        assert json.loads(env[1].area_at(db,'DRAW',env[2][0])['polygon'])==updated
    r=c.post('/admin/action',dict(action='area_version',name='受限区',kind='restricted',polygon=poly,reason='鼠标绘制'))
    assert r.status_code==200,r.json

def test_11_existing_gnss_alignment_and_observations_stay_unchanged(env):
    c=env[3]('admin')
    assert c.post('/admin/action',dict(action='calibration',lat=31,lng=121,x=50,y=50,rotation=0)).status_code==200
    assert c.get('/bootstrap').json['settings']['calibration']['x']==50
    poly=[[0,0],[100,100],[0,100],[100,0]]
    r=c.post('/admin/action',dict(action='area_version',area_id='A',polygon=poly,reason='无效自交'))
    assert r.status_code==400
    assert rows(env,'SELECT count(*) AS n FROM observations')[0]['n']==0

def test_12_backup_contains_uploaded_image_and_audit(env):
    c,m=new_map(env)
    r=c.post('/admin/backup');assert r.status_code==200,r.json
    z=c.get(r.json['download_url'].removeprefix('/api'));assert z.status_code==200
    with zipfile.ZipFile(io.BytesIO(z.data)) as archive:
        data=archive.read('minepath.sqlite3')
    restored=sqlite3.connect(':memory:');restored.deserialize(data)
    assert restored.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert restored.execute('SELECT count(*) FROM site_map_versions').fetchone()[0]==1
    png=restored.execute('SELECT image_data FROM site_map_assets').fetchone()[0]
    assert png.startswith(b'%PDF') is False and png.startswith(b'\x89PNG')
    assert restored.execute("SELECT count(*) FROM audit WHERE entity_type='site_map'").fetchone()[0]>=1
