const {chromium}=require('playwright');
(async()=>{const [,,model,out,list]=process.argv;const b=await chromium.launch({args:['--use-gl=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist']});
const p=await b.newPage({viewport:{width:420,height:600}});const errs=[];p.on('pageerror',e=>errs.push(e.message));p.on('console',m=>{if(m.type()==='error')errs.push(m.text().slice(0,300))});
await p.goto('http://localhost:8775/view/anim.html?m='+encodeURIComponent(model)+(process.env.Q||''));await p.waitForFunction(()=>window.__ready||null,{timeout:120000}).catch(e=>errs.push('timeout'));
console.log(JSON.stringify(await p.evaluate(()=>({clips:window.__clips,ms:window.__ms}))));
const shots=[];for(const item of list.split(';')){const [n,t,v]=item.split(',');await p.evaluate(([n,t,v])=>__show(n,+t,v),[n,t,v]);await p.waitForTimeout(100);const f=out+'/a_'+shots.length+'.png';await p.screenshot({path:f});shots.push(f);}
console.log('errors',JSON.stringify(errs));await b.close()})();
