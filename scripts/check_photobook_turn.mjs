import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const Fold = require('../static/js/photobook_fold.js').PhotobookFold;
const source = fs.readFileSync('static/js/photobook_viewer.js', 'utf8');
new Function(source);
const close = (a, b, label) => assert.ok(Math.abs(a-b) < 1e-6, `${label}: ${a} != ${b}`);
let samples = 0;
for (const [w,h] of [[450,600], [180,240], [640,400]]) {
  for (const grab of [0, h*0.2, h*0.5, h*0.92, h]) {
    for (const x of [-w*2, -w, -w*0.6, 0, w*0.6, w, w*2]) {
      for (const y of [-h, 0, h*0.3, h*0.8, h, h*2]) {
        const f = Fold.geometry(w,h,grab,{x,y});
        for (const sy of [0,h*0.25,h*0.5,h]) {
          const p=Fold.position(f,{x:0,y:sy});
          close(p.x,0,'spine x'); close(p.y,sy,'spine y'); close(p.z,0,'spine z');
        }
        // Once a full half-cylinder fits before the held edge, that edge must
        // land at the constrained finger position in both screen coordinates.
        if (!f.flat && f.nx*w+f.ny*grab-f.crease >= Math.PI*f.radius) {
          const p=Fold.position(f,{x:w,y:grab});
          close(p.x,f.point.x,'finger x'); close(p.y,f.point.y,'finger y');
        }
        const strips=Fold.strips(w,h,f,32);
        let area=0;
        for (const strip of strips) {
          assert.ok(strip.matrix.every(Number.isFinite));
          const p=strip.polygon;
          area += Math.abs(p.reduce((a,q,i) => a+q.x*p[(i+1)%p.length].y-q.y*p[(i+1)%p.length].x,0))/2;
        }
        close(area,w*h,'all source paper accounted for');
        samples++;
      }
    }
    const finished=Fold.geometry(w,h,grab,{x:-w,y:grab});
    for (const p of [{x:0,y:0},{x:w,y:0},{x:w,y:h},{x:0,y:h}]) {
      const q=Fold.position(finished,p);
      close(q.x,-p.x,'finished x'); close(q.y,p.y,'finished y');
    }
  }
}
const upper=Fold.geometry(450,600,550,{x:200,y:250});
const lower=Fold.geometry(450,600,50,{x:200,y:350});
assert.ok(upper.ny>0 && lower.ny<0,'Up/down drags must create opposite diagonal creases');
assert.ok(upper.radius>1 && lower.radius>1,'Diagonal bends must have a rounded curl');

function setup(wide, pageCount=7) {
  let now=0, nextFrame=1;
  const frames=new Map(), elements=new Map();
  const ctx=new Proxy({}, {get: (target,key) => target[key] || (()=>{}), set:(target,key,value)=>{target[key]=value;return true;}});
  function element() {
    return {style:{}, classList:{toggle(){},add(){}},children:[],listeners:{},
      clientWidth:wide?1000:360,clientHeight:600,
      appendChild(node){this.children.push(node);},remove(){this.removed=true;},
      contains(node){return node===this;},setAttribute(){},getContext(){return ctx;},
      addEventListener(type,fn){this.listeners[type]=fn;},
      getBoundingClientRect(){return {left:0,top:0,width:this.clientWidth,height:600};},
      setPointerCapture(id){this.capture=id;},hasPointerCapture(id){return this.capture===id;},
      releasePointerCapture(){this.capture=null;}};
  }
  const document={body:element(),createElement:element,addEventListener(){},getElementById(id){
    if(!elements.has(id)) elements.set(id,element());return elements.get(id);
  }};
  document.getElementById('pbPageData').textContent=JSON.stringify(Array.from({length:pageCount},(_,i)=>({no:i+1,url:`/${i}`,w:600,h:800})));
  const context=vm.createContext({document,window:{PhotobookFold:Fold,addEventListener(){},devicePixelRatio:1},Image:class{},
    matchMedia:q=>({matches:q.includes('reduced-motion')?false:wide,addEventListener(){}}),
    performance:{now:()=>now},requestAnimationFrame:fn=>{const id=nextFrame++;frames.set(id,fn);return id;},cancelAnimationFrame:id=>frames.delete(id)});
  vm.runInContext(source.replace(/\}\)\(\);\s*$/,`globalThis.test={beginTurn,jump,getDrag:()=>drag,getIndex:()=>index,isBusy:()=>busy};})();`),context);
  const stage=document.getElementById('pbStage');
  return {...context.test,stage,
    event(type,x,y,extra={}){stage.listeners[type]?.({type,clientX:x,clientY:y,pointerId:1,isPrimary:true,button:0,target:document.getElementById('pbBook'),...extra});},
    settle(){now+=1000;const queued=[...frames.values()];frames.clear();queued.forEach(fn=>fn(now));},
  };
}
for (const wide of [true,false]) {
  const x=wide?950:340, travel=wide?350:140;
  for (const ending of ['pointerup','pointercancel','lostpointercapture']) {
    const app=setup(wide);
    app.event('pointerdown',x,580);
    app.event('pointermove',x-travel,120);
    const state=app.getDrag().state;
    assert.ok(state);
    app.event('pointermove',0,0,{pointerId:2});
    assert.equal(state.requested.y,120,'Ignore a second pointer');
    app.event('pointerleave',x-travel,-100);
    assert.ok(app.getDrag(),'Capture continues outside stage');
    app.event(ending,x-travel,120);
    app.settle();
    assert.equal(app.getIndex(),ending==='pointerup'?2:0);
    assert.equal(app.stage.capture,null);
    assert.equal(app.isBusy(),false);
    assert.equal(state.canvas.removed,true);
  }
  const reverse=setup(wide);
  reverse.jump(3);
  reverse.event('pointerdown',20,30);
  reverse.event('pointermove',20+travel,450);
  reverse.event('pointerup',20+travel,450);
  reverse.settle();
  assert.equal(reverse.getIndex(),0,'Backward drag');
  const vertical=setup(wide);
  vertical.event('pointerdown',x,580);
  vertical.event('pointermove',x,350);
  assert.ok(vertical.getDrag().state,'Vertical-only movement also bends paper');
  const boundary=setup(wide);
  boundary.event('pointerdown',20,20);
  boundary.event('pointermove',150,300);
  assert.equal(boundary.getDrag(),null);
  assert.equal(boundary.stage.capture,null);
  boundary.jump(7);
  assert.equal(boundary.beginTurn(1),null);
  assert.equal(setup(wide,1).beginTurn(1),null);
}
console.log(`PASS: ${samples} fold geometries, binding/finger constraints, paper coverage, completed turns, desktop/mobile pointer direction/cancellation/boundaries.`);
