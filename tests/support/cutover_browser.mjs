import { chromium } from '@playwright/test';
import fs from 'node:fs';
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const browser=await chromium.launch();
const errors=[];
try {
  const context=await browser.newContext({proxy:{server:input.proxy},viewport:{width:1280,height:800}});
  const page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));
  page.on('response',r=>{if(r.status()>=400 && !input.prepared) errors.push(r.url()+':'+r.status());});
  page.on('requestfailed',r=>errors.push(r.url()+':'+r.failure()?.errorText));
  for (const path of input.paths) {
    const response=await page.goto(input.origin+path,{waitUntil:'networkidle'});
    if(response.status()!==200) throw new Error('route failed: '+path);
    if(path!='/web/login') {
      await page.getByRole('heading',{name:/Isolated Odoo release/}).waitFor();
      if(!(await page.locator('img').evaluateAll(images=>images.every(i=>i.complete && i.naturalWidth>0)))) throw new Error('broken media');
    }
  }
  await page.goto(input.origin+'/',{waitUntil:'networkidle'});
  await page.screenshot({path:input.screenshot,fullPage:true});
  await page.getByRole('link',{name:'Contact',exact:true}).click();
  await page.getByRole('heading',{name:/Isolated Odoo release/}).waitFor();
  if(input.prepared) {
    await page.getByText('We are updating the site.',{exact:false}).waitFor();
    if(await page.locator('input:not([disabled]),button:not([disabled])').count()) throw new Error('unpaused form');

  }
  await page.screenshot({path:input.screenshot.replace('.png','-contact.png'),fullPage:true});
  if(input.prepared) { const personal=await page.goto(input.origin+'/my'); if(personal.status()!==423) throw new Error('personal route not paused'); await page.getByText('Signed-in/editor activity temporarily paused.',{exact:true}).waitFor(); }
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:input.screenshot.replace('.png','-mobile.png'),fullPage:true});
  if(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth)) throw new Error('horizontal overflow');
  if(errors.length) throw new Error(JSON.stringify(errors));
  console.log(JSON.stringify({contract_sha256:input.contract_sha256,status:'pass',paths:input.paths,errors,checked_at:Date.now()/1000}));
} finally {await browser.close();}
