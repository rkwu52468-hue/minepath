'use strict';
async function renderMapEditor(){
  if(S.user.role!=='admin')throw Error('仅管理员可配置地图');
  const response=await api('/site-maps');S.site_maps=response.versions;
  const current=response.current,initialBase=current?.id||'';
  const knownWidth=current?current.width_px*current.meters_per_pixel:600;
  $('#main').innerHTML=heading('地图与区域','上传场区平面图，在图上划定作业区和受限区。监控与轨迹回放共用已保存的底图。')+
    `<div class="site-plan-grid"><div class="site-plan-canvas">${panel('场区平面图',`<div class="map-toolbar"><span id="map-mode-note">拖动地图查看；选择区域后可拖动顶点。</span>${button('查看全图','map-fit')}</div><div id="map" class="map site-plan-map"></div><div class="map-note" id="map-pointer">坐标单位：米。图片左下角为配置原点，右方为 x 正向，上方为 y 正向。</div><div class="map-toolbar"><label class="opacity-control">底图透明度 <input id="map-opacity" type="range" min="0.15" max="1" step="0.05" value="1" aria-label="底图透明度"></label><span id="map-draft-summary">尚未选择区域</span></div>`)}${panel('区域列表',table(['名称','类别','操作'],S.areas.map(a=>[esc(a.name),a.kind==='restricted'?'受限区':'作业区',button('编辑边界','map-edit-area','subtle',`data-id="${esc(a.id)}"`)])))}
    ${panel('底图版本',`<div class="map-note">${current?'当前：'+esc(current.name)+' · '+dt(current.valid_from):'当前使用内置场区示意图。'}<br>图片配置有版本记录。替换底图不会修改已有区域和人员坐标，请在同一场区坐标系中定位图片。</div><details class="map-version-details"><summary>查看 ${response.versions.length} 条版本记录</summary>${table(['版本','生效时间','状态','原因'],[...response.versions].reverse().map(v=>[esc(v.id),dt(v.valid_from),v.valid_to==null?'当前':'截至 '+dt(v.valid_to),esc(v.reason)]))}</details>`)}</div>
    <div class="site-plan-controls">${panel('1 上传底图与设置比例',`<form id="site-map-form" class="form-grid"><label>场区图片（PNG / JPG，最大5MB）<input id="site-map-file" name="file" type="file" accept="image/png,image/jpeg" ${current?'':'required'}></label><span id="map-file-info" class="muted small">${current?`${current.width_px} × ${current.height_px}像素；不选新文件可调整当前图片。`:'请上传清晰的场区平面图。'}</span>${field('name','底图名称','text',current?.name||'场区平面图','required maxlength="100"')}${field('width_m','图片完整宽度对应实际距离（米）','number',knownWidth,'required min="0.016" max="50000" step="any"')}${field('origin_x','图片左下角 x（米）','number',current?.origin_x??0,'required step="any"')}${field('origin_y','图片左下角 y（米）','number',current?.origin_y??0,'required step="any"')}${reason('本次调整原因')}<div class="form-actions"><button class="primary" type="submit">保存底图配置</button></div></form>`)}
    ${panel('2 在图上绘制或编辑区域',`<form id="map-region-form" class="form-grid">${select('area_id','选择已有区域或新建',[['','新建区域'],...S.areas.map(a=>[a.id,a.name])])}${field('name','区域名称','text','新作业区','required maxlength="100"')}${select('kind','区域类别',[['work','作业区'],['restricted','受限区']])}<div class="button-row">${button('开始绘制','map-draw','primary')}${button('完成绘制','map-finish')}${button('撤销末点','map-undo')}${button('清空草稿','map-clear')}</div><p class="muted small">依次点击边界拐点，至少3点后点击“完成绘制”。拖动编号顶点调整边界；草稿在保存前不会生效。</p><label>选中顶点<select id="map-vertex"><option value="">尚无顶点</option></select></label><div class="button-row">${button('删除选中顶点','map-remove')}</div>${reason('区域新增或边界调整原因')}<div id="map-region-message" class="notice" role="status">保存后立即生效；历史位置和补传仍按记录发生时的区域版本判断。</div><div class="form-actions"><button class="primary" type="submit">保存区域边界</button></div></form>`)}
    ${panel('3 手机定位与底图对齐',`<p class="muted">选择图上的已知点，再填写该点实测经纬度。此标定用于后续手机位置，已有记录保持原坐标。</p><form id="map-gps-form" class="form-grid">${[['lat','已知点纬度'],['lng','已知点经度'],['x','已知点平面 x（米）'],['y','已知点平面 y（米）'],['rotation','东向量到场区 x 轴的旋转角（度）']].map(([k,n])=>field(k,n,'number',S.settings.calibration?.[k]??(k==='rotation'?0:''),'step="any" required')).join('')}<div class="button-row">${button('在图上选择已知点','map-anchor')}</div><div class="form-actions"><button class="primary" type="submit">保存定位标定</button></div></form>`)}</div></div>`;
  createMap();drawAreas();map.doubleClickZoom.disable();
  const draft=new MinePathMaps.PolygonDraft(),draftLayers=L.layerGroup().addTo(map);
  let mode='pan',selected=-1,reading=false,imageWidth=current?.width_px||0;
  const regionForm=$('#map-region-form'),mapForm=$('#site-map-form');
  function setMode(value){mode=value;map.getContainer().classList.toggle('drawing-mode',mode==='draw'||mode==='anchor');$('#map-mode-note').textContent=mode==='draw'?'绘制中：依次点击边界拐点，然后点击“完成绘制”。':mode==='anchor'?'请选择一个实测经纬度已知的基准点。':'拖动编号顶点修改；拖动空白位置平移地图。';}
  function redraw(){
    draftLayers.clearLayers();const pts=draft.points(),xy=pts.map(p=>[p[1],p[0]]),color=regionForm.elements.kind.value==='restricted'?'#bc6551':'#245e49';
    if(pts.length>=3)L.polygon(xy,{color,fillOpacity:.19,weight:2.5,interactive:false}).addTo(draftLayers);
    else if(pts.length>=2)L.polyline(xy,{color,weight:2.5,interactive:false}).addTo(draftLayers);
    pts.forEach((p,i)=>{
      const marker=L.marker([p[1],p[0]],{draggable:true,keyboard:true,title:`顶点 ${i+1}，可拖动`,icon:L.divIcon({className:'map-vertex-icon'+(selected===i?' selected':''),html:String(i+1),iconSize:[26,26],iconAnchor:[13,13]})}).addTo(draftLayers);
      marker.on('click',()=>{selected=i;$('#map-vertex').value=String(i);});
      marker.on('dragend',()=>{const p=marker.getLatLng();draft.move(i,[p.lng,p.lat]);selected=i;redraw();});
    });
    $('#map-vertex').innerHTML=pts.length?pts.map((p,i)=>`<option value="${i}">${i+1} · x ${p[0].toFixed(2)} / y ${p[1].toFixed(2)}</option>`).join(''):'<option value="">尚无顶点</option>';
    if(selected>=0&&selected<pts.length)$('#map-vertex').value=String(selected);
    $('#map-draft-summary').textContent=pts.length?`${pts.length} 个草稿顶点 · 尚未保存`:'尚无草稿顶点';
  }
  function loadArea(id){
    const area=S.areas.find(a=>a.id===id);regionForm.elements.area_id.value=id;
    regionForm.elements.name.value=area?.name||'新作业区';regionForm.elements.name.readOnly=!!area;
    regionForm.elements.kind.value=area?.kind||'work';regionForm.elements.kind.disabled=!!area;
    regionForm.elements.reason.value='';draft.load(area?JSON.parse(area.polygon):[]);selected=-1;setMode('pan');redraw();
    if(area)map.fitBounds(draft.points().map(p=>[p[1],p[0]]),{padding:[40,40],maxZoom:3,animate:false});
  }
  map.on('click',e=>{
    if(mode==='draw'){try{draft.add([e.latlng.lng,e.latlng.lat]);selected=draft.points().length-1;redraw();}catch(err){toast(err.message);}}
    else if(mode==='anchor'){const f=$('#map-gps-form');f.elements.x.value=e.latlng.lng.toFixed(4);f.elements.y.value=e.latlng.lat.toFixed(4);setMode('pan');toast('已选基准点，请填写其经纬度并保存标定');}
  });
  map.on('mousemove',e=>$('#map-pointer').textContent=`场区坐标：x ${e.latlng.lng.toFixed(2)} 米 / y ${e.latlng.lat.toFixed(2)} 米`);
  $('#map-opacity').oninput=e=>mapBackground?.setOpacity(Number(e.target.value));
  regionForm.elements.area_id.onchange=e=>loadArea(e.target.value);
  regionForm.elements.kind.onchange=redraw;
  $('#map-vertex').onchange=e=>{selected=Number(e.target.value);redraw();};
  const localActions={
    'map-fit':()=>setMapBackground(undefined,true),
    'map-draw':()=>setMode('draw'),
    'map-finish':()=>{if(draft.points().length<3)return toast('至少选择3个顶点');setMode('pan');toast('可以拖动顶点调整，确认后保存边界');},
    'map-undo':()=>{draft.undo();selected=-1;redraw();},
    'map-clear':()=>{draft.clear();selected=-1;redraw();},
    'map-remove':()=>{if(!draft.points().length)return;draft.remove(Number($('#map-vertex').value));selected=-1;redraw();},
    'map-anchor':()=>setMode('anchor'),
    'map-edit-area':el=>loadArea(el.dataset.id)
  };
  Object.assign(actions,localActions);
  $('#site-map-file').onchange=async e=>{
    const file=e.target.files[0];if(!file)return;reading=true;
    try{
      if(file.size>5*1024*1024)throw Error('底图不能超过5MB');
      const bitmap=await createImageBitmap(file);imageWidth=bitmap.width;const height=bitmap.height;bitmap.close();
      if(imageWidth<16||imageWidth>4096||height<16||height>4096)throw Error('底图宽高均须在16—4096像素之间');
      $('#map-file-info').textContent=`已选 ${file.name} · ${imageWidth} × ${height}像素。确认图片实际宽度和左下角位置后保存。`;
    }catch(err){imageWidth=current?.width_px||0;e.target.value='';toast(err.message);}finally{reading=false;}
  };
  mapForm.onsubmit=async e=>{
    e.preventDefault();const submit=mapForm.querySelector('[type=submit]');if(reading)return toast('正在读取图片，请稍候');if(!imageWidth)return toast('请先选择底图');
    submit.disabled=true;
    try{
      const f=new FormData(mapForm);f.set('base_id',initialBase);f.set('meters_per_pixel',String(Number(f.get('width_m'))/imageWidth));
      const file=$('#site-map-file').files[0];if(!file)f.delete('file');
      let saved;if(file)saved=await api('/admin/site-map',f);else saved=await api('/admin/site-map',Object.fromEntries(f));
      toast('底图配置已保存');await boot(false);await render();
    }catch(err){toast(err.message);}finally{submit.disabled=false;}
  };
  regionForm.onsubmit=async e=>{
    e.preventDefault();const submit=regionForm.querySelector('[type=submit]');submit.disabled=true;
    try{
      const points=draft.points();if(points.length<3)throw Error('至少绘制3个顶点');
      await adminAction('area_version',{area_id:regionForm.elements.area_id.value,name:regionForm.elements.name.value,kind:regionForm.elements.kind.value,polygon:points,reason:regionForm.elements.reason.value});
      toast('区域边界已保存并生效');await boot(false);await render();
    }catch(err){$('#map-region-message').textContent=err.message;toast(err.message);}finally{submit.disabled=false;}
  };
  $('#map-gps-form').onsubmit=e=>submitInline(e,f=>adminAction('calibration',f));
}
