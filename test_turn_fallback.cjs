// Diagram tab, Turn view: a record is marked analyzed before its acts are
// split out, so when the newest turn has no operator acts yet the view shows
// the last turn that has them, says so, and moves on once the acts arrive.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
 const page = await browser.newPage({viewport:{width:390,height:844}});
 const record = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/xiang-turn-aligned.json'),'utf8')).record;
 // the newest turn, analyzed but with no acts yet (live: turn-XI5XUg, 03:05Z)
 const unread = {...JSON.parse(JSON.stringify(record)), acts:[], alignment:undefined,
                 turn_id:'claude-3-turn-55', analysis_status:'analyzed'};
 let newestHasActs = false;
 const errors = [];
 page.on('pageerror', e => errors.push(e.message));
 await page.route('**/*', async route => {
  const u = new URL(route.request().url());
  const p = u.pathname;
  if(p==='/') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(__dirname, 'index.html'),'utf8')});
  if(p==='/process_cues.js') return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(path.join(__dirname, 'process_cues.js'),'utf8')});
  if(p==='/agency/active')
    return route.fulfill({json:{ok:true,agents:[],target:{agent:'claude-3',buffer:'*claude-3*'}}});
  if(p==='/xiang/turns')
    return route.fulfill({json:{ok:true,turns:[
      {id:'turn-new', 'agent-id':'claude-3', 'created-at':'2026-10-19T03:05:00Z'},
      {id:'turn-DHzkds', 'agent-id':'claude-3', 'created-at':'2026-10-19T01:00:00Z'}]}});
  if(p==='/xiang/turns/turn-new') {
    const r = newestHasActs
      ? {...JSON.parse(JSON.stringify(record)), turn_id:'claude-3-turn-54'}
      : JSON.parse(JSON.stringify(unread));
    return route.fulfill({json:{ok:true,record:r}});
  }
  if(p==='/xiang/turns/turn-DHzkds') return route.fulfill({json:{ok:true,record}});
  return route.fulfill({json:{ok:true,agents:[],choices:[],text:null,providers:{},bells:[],lanes:[]}});
 });
 await page.addInitScript(() => {
   try { localStorage.removeItem('voxterm-tab'); localStorage.removeItem('voxterm-diagram-view'); } catch (_) {}
 });
 await page.goto('http://voxterm.test/');
 await page.locator('#tabDiagramBtn').click();
 await page.waitForTimeout(400);
 const shown = () => page.evaluate(() => turnList[turnIndex].id);
 const head = () => page.locator('#turnHead').innerText();

 // (a) newest has no acts: the last read turn is shown, and the header says why
 assert.equal(await shown(), 'turn-DHzkds');
 assert.match(await head(), /^newest turn not read yet — showing the last one read\n4 acts: /);
 assert.equal(await page.locator('#turnSvg .wire').count(), 4);

 // (b) a refresh while the newest is still unread keeps the last read turn
 await page.evaluate(() => { turnListAt = 0; turnEnsure(); });
 await page.waitForTimeout(300);
 assert.equal(await shown(), 'turn-DHzkds');

 // (c) once its acts arrive, the next refresh moves to the newest turn
 newestHasActs = true;
 await page.evaluate(() => { turnListAt = 0; turnEnsure(); });
 await page.waitForTimeout(300);
 assert.equal(await shown(), 'turn-new');
 assert.doesNotMatch(await head(), /not read yet/);

 // (d) stepping by hand to an unread newest shows it as it is, and a refresh
 // reloads it in place rather than falling back again
 newestHasActs = false;
 await page.locator('#turnPrevBtn').click(); await page.waitForTimeout(200);
 await page.locator('#turnNextBtn').click(); await page.waitForTimeout(300);
 assert.equal(await shown(), 'turn-new');
 assert.equal(await head(), 'no operator acts recorded');
 await page.evaluate(() => { turnListAt = 0; turnEnsure(); });
 await page.waitForTimeout(300);
 assert.equal(await shown(), 'turn-new');
 // ... and when its acts land, that same refresh draws them
 newestHasActs = true;
 await page.evaluate(() => { turnListAt = 0; turnEnsure(); });
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#turnSvg .wire').count(), 4);

 assert.deepEqual(errors, []);
 await browser.close();
 console.log('test_turn_fallback.cjs: all assertions passed');
 process.exit(0);
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
