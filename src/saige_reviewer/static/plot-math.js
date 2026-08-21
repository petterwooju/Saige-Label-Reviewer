(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.SaigePlotMath=Object.freeze(api);
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';

  function clamp(value,minimum,maximum){
    return Math.min(maximum,Math.max(minimum,value));
  }

  function normalizedWheelDelta(deltaY,deltaMode,pageHeight){
    const unit=deltaMode===1?16:deltaMode===2?Math.max(1,pageHeight):1;
    return deltaY*unit;
  }

  function stablePlotHash(value){
    let hash=2166136261;
    for(let index=0;index<value.length;index++){
      hash^=value.charCodeAt(index);
      hash=Math.imul(hash,16777619);
    }
    return hash>>>0;
  }

  function zoomViewAt(view,nextZoom,pointer,viewport,minimum=.35,maximum=40){
    const previous=clamp(Number(view.zoom)||1,minimum,maximum);
    const zoom=clamp(Number(nextZoom)||previous,minimum,maximum);
    const centerX=viewport.width/2,centerY=viewport.height/2;
    const relativeX=pointer.x-centerX,relativeY=pointer.y-centerY;
    const worldX=(relativeX-(Number(view.panX)||0))/previous;
    const worldY=(relativeY-(Number(view.panY)||0))/previous;
    return {
      zoom,
      panX:relativeX-worldX*zoom,
      panY:relativeY-worldY*zoom
    };
  }

  function fittedPoint(point,bounds,viewport,view,padding=36){
    const usableWidth=Math.max(1,viewport.width-padding*2);
    const usableHeight=Math.max(1,viewport.height-padding*2);
    const rangeX=bounds.maxX-bounds.minX;
    const rangeY=bounds.maxY-bounds.minY;
    const scale=Math.min(usableWidth/(rangeX||1),usableHeight/(rangeY||1));
    const centerX=viewport.width/2,centerY=viewport.height/2;
    const dataCenterX=(bounds.minX+bounds.maxX)/2;
    const dataCenterY=(bounds.minY+bounds.maxY)/2;
    return {
      x:centerX+(point.x-dataCenterX)*scale*view.zoom+view.panX,
      y:centerY-(point.y-dataCenterY)*scale*view.zoom+view.panY
    };
  }

  function adaptiveGridStep(zoom,base=42,minimum=28,maximum=84){
    let step=base*Math.max(Number(zoom)||1,.0001);
    while(step<minimum)step*=2;
    while(step>maximum)step/=2;
    return step;
  }

  return {clamp,normalizedWheelDelta,stablePlotHash,zoomViewAt,fittedPoint,adaptiveGridStep};
});
