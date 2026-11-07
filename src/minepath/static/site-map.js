/* Shared site-plan geometry. Image pixels start top-left; world metres bottom-left. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.MinePathMaps=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function point(p){
    if(!Array.isArray(p)||p.length!==2||!p.every(v=>Number.isFinite(v)&&Math.abs(v)<1000000))throw Error('顶点坐标无效');
    return [...p];
  }
  function check(m){
    if(!m||![m.width_px,m.height_px,m.origin_x,m.origin_y,m.meters_per_pixel].every(Number.isFinite)||m.width_px<=0||m.height_px<=0||m.meters_per_pixel<=0)throw Error('图片比例与位置无效');
  }
  function pixelToWorld(m,p){check(m);point(p);return [m.origin_x+p[0]*m.meters_per_pixel,m.origin_y+(m.height_px-p[1])*m.meters_per_pixel];}
  function worldToPixel(m,p){check(m);point(p);return [(p[0]-m.origin_x)/m.meters_per_pixel,m.height_px-(p[1]-m.origin_y)/m.meters_per_pixel];}
  function bounds(m){check(m);return [[m.origin_y,m.origin_x],[m.origin_y+m.height_px*m.meters_per_pixel,m.origin_x+m.width_px*m.meters_per_pixel]];}
  function selectMap(versions,at){return [...(versions||[])].reverse().find(m=>at===undefined?m.valid_to==null:m.valid_from<=at&&(m.valid_to==null||at<m.valid_to))||null;}
  class PolygonDraft{
    constructor(points=[]){this.load(points);}
    load(points){this._points=points.map(point);}
    points(){return this._points.map(p=>[...p]);}
    add(p){if(this._points.length>=100)throw Error('一个区域最多100个顶点');this._points.push(point(p));}
    move(index,p){if(!Number.isInteger(index)||index<0||index>=this._points.length)throw Error('请选择有效顶点');this._points[index]=point(p);}
    remove(index){if(!Number.isInteger(index)||index<0||index>=this._points.length)throw Error('请选择有效顶点');this._points.splice(index,1);}
    undo(){this._points.pop();}
    clear(){this._points=[];}
  }
  return {pixelToWorld,worldToPixel,bounds,selectMap,PolygonDraft};
});
