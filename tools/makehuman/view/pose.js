const {chromium}=require('playwright');const fs=require('fs');
(async()=>{const [,,model,outdir,views,A,exportTo]=process.argv;const b=await chromium.launch({args:['--use-gl=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist']});
const p=await b.newPage({viewport:{width:600,height:800}});const errs=[];p.on('pageerror',e=>errs.push(e.message));p.on('console',m=>{if(m.type()==='error')errs.push(m.text().slice(0,200))});
await p.goto('http://localhost:8775/view/pose.html?m='+encodeURIComponent(model));await p.waitForFunction(()=>window.__ready,{timeout:90000});
console.log(JSON.stringify(await p.evaluate(()=>window.__ready)));
await p.evaluate(a=>__pose(a),JSON.parse(A||'{}'));
for(const v of views.split(',')){await p.evaluate(v=>__view(v),v);await p.waitForTimeout(200);await p.screenshot({path:outdir+'/p_'+v+'.png'});}
if(exportTo){const b64=await p.evaluate(()=>__export());fs.writeFileSync(exportTo,Buffer.from(b64,'base64'));console.log('exported',exportTo);}
console.log('errors',JSON.stringify(errs));await b.close()})();
