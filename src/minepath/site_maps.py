"""Authenticated, versioned site plan images in the same local SQLite backup."""
from __future__ import annotations
import hashlib,io,math,warnings
from flask import request,g,jsonify,send_file
from PIL import Image,ImageOps,UnidentifiedImageError
from .core import DomainError,uid

MAP_COLUMNS='v.id,v.asset_id,v.name,a.width_px,a.height_px,a.sha256,v.origin_x,v.origin_y,v.meters_per_pixel,v.valid_from,v.valid_to,v.actor_id,v.reason'

def map_versions(db):
    result=[]
    for row in db.execute('SELECT '+MAP_COLUMNS+' FROM site_map_versions v JOIN site_map_assets a ON a.id=v.asset_id ORDER BY v.valid_from,v.rowid'):
        m=dict(row)
        m['image_url']='/api/site-maps/'+m['asset_id']+'/image'
        m['bounds']=[[m['origin_y'],m['origin_x']],[m['origin_y']+m['height_px']*m['meters_per_pixel'],m['origin_x']+m['width_px']*m['meters_per_pixel']]]
        result.append(m)
    return result

def placement(info,width,height):
    try: value={k:float(info[k]) for k in ('origin_x','origin_y','meters_per_pixel')}
    except (ValueError,TypeError,KeyError): raise DomainError('填写有效的图片比例和左下角坐标')
    x,y,scale=(value[k] for k in ('origin_x','origin_y','meters_per_pixel'))
    if not all(math.isfinite(v) for v in value.values()) or not 0.001<=scale<=100 or max(width,height)*scale>50000 or max(abs(x),abs(y),abs(x+width*scale),abs(y+height*scale))>=1000000:
        raise DomainError('图片比例应为0.001—100米/像素，边长不超过50千米，坐标须在有效范围内')
    return value

def normalized_image(file):
    raw=file.read(5*1024*1024+1)
    if len(raw)>5*1024*1024: raise DomainError('底图文件不能超过5MB',413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as candidate:
                if candidate.format not in ('PNG','JPEG'): raise DomainError('底图只支持PNG或JPG图片')
                if not (16<=candidate.width<=4096 and 16<=candidate.height<=4096): raise DomainError('底图宽高均须在16—4096像素之间')
                candidate.verify()
            with Image.open(io.BytesIO(raw)) as source:
                im=ImageOps.exif_transpose(source).convert('RGBA' if source.mode=='RGBA' else 'RGB')
                im.info.clear()
                out=io.BytesIO();im.save(out,format='PNG')
                if out.tell()>12*1024*1024: raise DomainError('图片解码后过大，请减小尺寸或简化底图')
                return out.getvalue(),im.width,im.height
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning,ValueError):
        raise DomainError('无法读取底图，请选择有效PNG或JPG图片')

def register_site_maps(app,svc,admin,body):
    @app.get('/api/site-maps')
    def site_maps():
        with svc.tx() as db: versions=map_versions(db)
        current=next((m for m in reversed(versions) if m['valid_to'] is None),None)
        return jsonify(versions=versions,current=current)

    @app.get('/api/site-maps/<asset_id>/image')
    def site_map_image(asset_id):
        with svc.tx() as db: asset=db.execute('SELECT image_data,sha256 FROM site_map_assets WHERE id=?',(asset_id,)).fetchone()
        if not asset: raise DomainError('底图不存在',404)
        response=send_file(io.BytesIO(asset['image_data']),mimetype='image/png',download_name='site-map.png',etag=asset['sha256'])
        response.headers['Cache-Control']='no-store'
        return response

    @app.post('/api/admin/site-map')
    def save_site_map():
        admin()
        info=request.form if request.files else body()
        reason=str(info.get('reason','')).strip()
        if not reason or len(reason)>500: raise DomainError('填写1—500字的底图调整原因')
        name=str(info.get('name','场区底图')).strip()
        if not name or len(name)>100: raise DomainError('底图名称长度为1—100字')
        file=request.files.get('file')
        uploaded=normalized_image(file) if file else None
        with svc.tx(True) as db:
            old=db.execute('SELECT v.*,a.width_px,a.height_px FROM site_map_versions v JOIN site_map_assets a ON a.id=v.asset_id WHERE v.valid_to IS NULL').fetchone()
            if str(info.get('base_id') or '')!=(old['id'] if old else ''):
                raise DomainError('底图已被其他操作更新，请刷新后重新调整',409)
            if not uploaded and not old: raise DomainError('请先上传一张场区底图')
            width,height=(uploaded[1],uploaded[2]) if uploaded else (old['width_px'],old['height_px'])
            values=placement(info,width,height)
            if uploaded:
                raw=uploaded[0];asset_id=uid('map-image')
                db.execute('INSERT INTO site_map_assets VALUES(?,?,?,?,?,?)',(asset_id,name,width,height,hashlib.sha256(raw).hexdigest(),raw))
            else: asset_id=old['asset_id']
            now=svc.now();version=uid('map')
            db.execute('UPDATE site_map_versions SET valid_to=? WHERE valid_to IS NULL',(now,))
            db.execute('INSERT INTO site_map_versions VALUES(?,?,?,?,?,?,?,?,?,?)',(version,asset_id,name,values['origin_x'],values['origin_y'],values['meters_per_pixel'],now,None,g.user['id'],reason))
            svc.audit(db,g.user['id'],'上传场区底图' if uploaded else '调整底图比例与位置','site_map',version,dict(asset_id=asset_id,**values,reason=reason))
            result=next(m for m in map_versions(db) if m['id']==version)
        return jsonify(ok=True,map=result)
