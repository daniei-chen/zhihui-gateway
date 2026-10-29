// Probe3: red-packet list item structure + security page sections.
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-diag-lists/v1','diaglst');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
await H.login(page);
// red-packets: how is one packet rendered?
await page.goto(base.origin+'/red-packets/',{waitUntil:'domcontentloaded'});
await page.waitForTimeout(2500);
const rp=await page.evaluate(()=>{
 const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
 const hit=[...document.querySelectorAll('*')].filter(el=>el.textContent?.includes('qa-ui12')&&el.children.length<8&&vis(el)).slice(0,3)
  .map(el=>({tag:el.tagName,cls:String(el.className).slice(0,80),text:(el.textContent||'').trim().slice(0,100)}));
 const container=hit[0]?hit[0]:null;
 let itemBtns=[];
 if(container){let p=container;for(let i=0;i<4&&p;i++){p=p.parentElement;
  if(p&&p.querySelectorAll('button').length){itemBtns=[...p.querySelectorAll('button:visible,[title]')].map(b=>({t:(b.textContent||'').trim().slice(0,12),title:b.title||'',vis:vis(b)})).filter(x=>x.t||x.title).slice(0,10);break;}}}
 return {hit,itemBtns,rows:document.querySelectorAll('[role=row]').length,
  headings:[...document.querySelectorAll('h1,h2,h3')].filter(vis).map(h=>h.textContent.trim()).slice(0,8)};
});
H.record('redpacket_list',rp);
// claim page as anonymous: what's on /claim?code=...
const code='6ZwDSzGeOwp_-mV5sT5o-g';
await page.goto(base.origin+'/claim?code='+code,{waitUntil:'domcontentloaded'}).catch(async e=>{
 await page.goto(base.origin+'/claim/?code='+code,{waitUntil:'domcontentloaded'});});
await page.waitForTimeout(2000);
const claim=await page.evaluate(()=>({url:location.pathname+location.search,
 text:document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,12),
 buttons:[...document.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(Boolean).slice(0,8)}));
H.record('claim_page',{...claim,url_after:claim.url});
// security: sections for logs/audit
await page.goto(base.origin+'/security/',{waitUntil:'domcontentloaded'});
await page.waitForTimeout(2200);
const sec=await page.evaluate(()=>{
 const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
 return {headings:[...document.querySelectorAll('h1,h2,h3,h4,[class*=section] h2')].filter(vis).map(h=>h.textContent.trim()).filter(Boolean).slice(0,12),
  tabsLike:[...document.querySelectorAll('[role=tab],[data-state]')].filter(vis).slice(0,8).map(t=>({role:t.getAttribute('role'),state:t.getAttribute('data-state'),text:(t.textContent||'').trim().slice(0,20)})),
  bodySniff:document.body.innerText.split('\n').map(t=>t.trim()).filter(l=>/日志|审计|记录|规则/.test(l)).slice(0,10)};
});
H.record('security_sections',sec);
console.log(JSON.stringify(H.report.interactions,null,1).slice(0,3500));
await ctx.close();
await H.finish();
