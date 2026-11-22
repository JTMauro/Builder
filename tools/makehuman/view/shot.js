const {chromium}=require('playwright');
(async()=>{const [,,model,outdir,views='front,three,face,back']=process.argv;const b=await chromium.launch({args:['--use-gl=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist']});
const p=await b.newPage({viewport:{width:600,height:800}});const errs=[];p.on('pageerror',e=>errs.push(e.message));p.on('console',m=>{if(m.type()==='error')errs.push(m.text().slice(0,200))});
await p.goto('http://localhost:8775/view/view.html?m='+encodeURIComponent(model));await p.waitForFunction(()=>window.__info,{timeout:90000});
console.log(JSON.stringify(await p.evaluate(()=>window.__info)));
for(const v of views.split(',')){await p.evaluate(v=>__view(v),v);await p.waitForTimeout(200);await p.screenshot({path:outdir+'/'+v+'.png'});}
console.log('errors',JSON.stringify(errs));await b.close()})();
