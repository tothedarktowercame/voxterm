// Turn view work line: 馬's checked work record, one line under the header.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
 const page = await browser.newPage({viewport:{width:390,height:844}});
 const record = JSON.parse(fs.readFileSync(__dirname + '/fixtures/xiang-turn-aligned.json','utf8')).record;
 record.evidence_id = 'emacs-8b01487d799a38b3f1af36069283676e';   // the item card
 record.session_id = 'sess-1';
 const cards = JSON.parse(fs.readFileSync(__dirname + '/fixtures/ma-card-sample.json','utf8')).entries;
 const workOf = e => e['evidence/body'].card.fresh.work;
 let cardAnswer = {ok:true, work: workOf(cards[0])};
 let cardQueries = [], failCard = false;
 await page.route('**/*', async route => {
  const u = new URL(route.request().url());
  const p = u.pathname;
  if(p==='/') return route.fulfill({contentType:'text/html',body:fs.readFileSync(require('node:path').join(__dirname, 'index.html'),'utf8')});
  if(p==='/process_cues.js') return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(require('node:path').join(__dirname, 'process_cues.js'),'utf8')});
  if(p==='/agency/active') return route.fulfill({json:{ok:true,agents:[],target:{agent:'claude-3',buffer:'*x*'}}});
  if(p==='/usage') return route.fulfill({json:{ok:true,providers:{}}});
  if(p==='/xiang/turns') return route.fulfill({json:{ok:true,turns:[
    {id:'t-main', agent:'claude-3', created_at:'2026-10-19T03:00:00Z'},
    {id:'older', agent:'claude-3', created_at:'2026-10-19T02:00:00Z'}]}});
  if(p==='/xiang/turns/t-main') return route.fulfill({json:{ok:true,record}});
  if(p==='/xiang/turns/older') return route.fulfill({json:{ok:true,record:
    Object.assign(JSON.parse(JSON.stringify(record)), {evidence_id:'no-card-turn', created_at:'2026-10-19T02:00:00Z'})}});
  if(p==='/xiang/card') {
    cardQueries.push(u.searchParams.get('turn'));
    if (failCard) return route.fulfill({status:500, body:'x'});
    return route.fulfill({json: cardAnswer});
  }
  if(p==='/diagram/dispatch') return route.fulfill({json:{ok:true,window:{from:'2026-10-19T00:00:00Z',to:'2026-10-19T04:00:00Z'},lanes:['joe'],bells:[],open:[],crossings:[],lawful:true,findings:{}}});
  if(p==='/target') return route.fulfill({json:{ok:true}});
  return route.fulfill({json:{ok:true,agents:[],choices:[],text:null}});
 });
 await page.addInitScript(() => {
   try { localStorage.removeItem('voxterm-tab'); localStorage.removeItem('voxterm-diagram-view'); } catch (_) {}
 });
 await page.goto('http://voxterm.test/');
 await page.locator('#tabDiagramBtn').click();
 await page.waitForTimeout(500);

  // (a) item record draws item + quote
 const line = () => page.locator('#turnWork').innerText();
 assert.match(await line(), /^Work: /);
 assert.match(await line(), /Awaiting user's go on the frames-tab stale-purpose fix/);
 assert.match(await line(), /“logically important fix to make regarding the purpose not being carried forward”/);
 // tap shows the full item
 await page.locator('#turnWork').click();
 assert.match(await page.locator('#diagramDetail').innerText(), /Work item: Awaiting user's go/);

 // (b) none record
 cardAnswer = {ok:true, work: workOf(cards[1])};
 await page.evaluate(() => loadTurnWork());
 await page.waitForTimeout(200);
 assert.match(await line(), /^Work: none — /);
 assert.match(await line(), /not on the known-work list/);

 // (c) rejected record draws the warning with the reason
 cardAnswer = {ok:true, work: {rejected: 'item not on card', item: 'fix everything', quote: 'please'}};
 await page.evaluate(() => loadTurnWork());
 await page.waitForTimeout(200);
 assert.match(await line(), /馬’s answer rejected \(item not on card\)/);
 const color = await page.locator('#turnWork').evaluate(el => el.style.color);
 assert.ok(['#f80','rgb(255, 136, 0)'].includes(color), 'rejected line is styled as a warning');
 // tap shows the model's original fields
 await page.locator('#turnWork').click();
 assert.match(await page.locator('#diagramDetail').innerText(), /rejected: item not on card/);
 assert.match(await page.locator('#diagramDetail').innerText(), /fix everything/);

 // (d)+(e) stepping ‹ to a turn with no card replaces the line
 cardAnswer = {ok:true, work: null};
 await page.locator('#turnPrevBtn').click();
 await page.waitForTimeout(300);
 assert.equal(await line(), 'Work: not read yet');
 assert.equal(cardQueries[cardQueries.length-1], 'no-card-turn');
 // and a failed fetch is (unavailable), never a stale line
 await page.waitForTimeout(300);
 failCard = true;
 await page.locator('#turnNextBtn').click();   // back to t-main: its card fetch now fails
 await page.waitForTimeout(500);
 assert.equal(await line(), 'Work: (unavailable)');
 await browser.close();
 console.log('test_work_line.cjs: all assertions passed');
 process.exit(0);
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
