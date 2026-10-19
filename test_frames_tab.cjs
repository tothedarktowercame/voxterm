// Frames tab: the 象 stepper iframe follows agency/active's `target`.
// Route-fulfil index.html like test_process_cues_browser.cjs; stub agency/active.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
 const page = await browser.newPage({viewport:{width:390,height:844}});
 let active = {ok:true, agents:[{id:'claude-3',status:'idle'}], target:{agent:'claude-3',buffer:'*claude-3*'}};
 const frameLoads = [];
 await page.route('**/*', async route => {
  const u = new URL(route.request().url());
  const p = u.pathname;
  if(p==='/') return route.fulfill({contentType:'text/html',body:fs.readFileSync(require('node:path').join(__dirname, 'index.html'),'utf8')});
  if(p==='/process_cues.js') return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(require('node:path').join(__dirname, 'process_cues.js'),'utf8')});
  if(p==='/agency/active') return route.fulfill({json:active});
  if(p==='/wip/stepper/') { frameLoads.push(u.search); return route.fulfill({contentType:'text/html',body:'<html><body>stepper stub</body></html>'}); }
  return route.fulfill({json:{ok:true,agents:[],choices:[],text:null}});
 });
 await page.addInitScript(() => { try { localStorage.removeItem('voxterm-tab'); } catch (_) {} });
 await page.goto('http://voxterm.test/');
 // (d) precondition sanity + lazy load: on the Agents tab no iframe exists yet.
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#framesFrame').isVisible(), false);

 // (a) Frames tab shows the stepper iframe for the stubbed target claude-3.
 await page.locator('#tabFramesBtn').click();
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#framesFrame').isVisible(), true);
 assert.ok((await page.locator('#framesFrame').getAttribute('src')).endsWith('?agent=claude-3'));
 assert.equal(await page.locator('#framesNote').isVisible(), false);
 assert.equal(await page.locator('#viewAgents').isVisible(), false);

 // (c) same target across polls: src unchanged, no extra iframe navigation.
 const srcBefore = await page.locator('#framesFrame').getAttribute('src');
 const navCount0 = frameLoads.length;
 await page.evaluate(() => refreshAgents());
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#framesFrame').getAttribute('src'), srcBefore);
 assert.equal(frameLoads.length, navCount0);

 // (b) target changes to zai-2: src follows.
 active = {ok:true, agents:[{id:'zai-2',status:'invoking'}], target:{agent:'zai-2',buffer:'*zai-2*'}};
 await page.evaluate(() => refreshAgents());
 await page.waitForTimeout(300);
 assert.ok((await page.locator('#framesFrame').getAttribute('src')).endsWith('?agent=zai-2'));

 // (d) no target: message shows, no iframe.
 active = {ok:true, agents:[], target:null};
 await page.evaluate(() => refreshAgents());
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#framesNote').isVisible(), true);
 assert.equal(await page.locator('#framesFrame').isVisible(), false);

 // Agents tab still carries the chips view and nothing broke.
 await page.locator('#tabAgentsBtn').click();
 assert.equal(await page.locator('#viewAgents').isVisible(), true);
 active = {ok:true, agents:[{id:'claude-3',status:'idle'}], target:{agent:'claude-3',buffer:'*claude-3*'}};
 await page.evaluate(() => refreshAgents());
 await page.waitForTimeout(300);
 assert.match(await page.locator('#agents').innerText(), /claude-3|zai-2|nothing invoking/);
 await browser.close();
 console.log('test_frames_tab.cjs: all assertions passed');
 process.exit(0);
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
