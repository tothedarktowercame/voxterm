// Diagram tab, Turn view: one operator turn's string diagram (intent → answer
// mapping) over the xiang-turn-aligned fixture.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
 const page = await browser.newPage({viewport:{width:390,height:844}});
 const record = JSON.parse(fs.readFileSync(__dirname + '/fixtures/xiang-turn-aligned.json','utf8')).record;
 const opActs = record.acts.filter(a => a.author === 'operator' && a.turn === record.turn_id);
 const align = record.alignment.acts;
 const turnRequests = [];
 await page.route('**/*', async route => {
  const u = new URL(route.request().url());
  const p = u.pathname;
  if(p==='/') return route.fulfill({contentType:'text/html',body:fs.readFileSync(require('node:path').join(__dirname, 'index.html'),'utf8')});
  if(p==='/process_cues.js') return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(require('node:path').join(__dirname, 'process_cues.js'),'utf8')});
  if(p==='/agency/active')
    return route.fulfill({json:{ok:true,agents:[],target:{agent:'claude-3',buffer:'*claude-3*'}}});
  if(p==='/xiang/turns' && !u.searchParams.get('limit').includes('record'))
    { turnRequests.push('list'); return route.fulfill({json:{ok:true,turns:[
      // the list endpoint's keys are kebab-case (the record's are snake_case)
      {id:'newer', 'agent-id':'claude-3', 'created-at':'2026-10-19T03:00:00Z'},
      {id:'turn-DHzkds', 'agent-id':'claude-3', 'created-at':'2026-10-19T01:00:00Z'},
      {id:'older', 'agent-id':'claude-3', 'created-at':'2026-10-19T00:00:00Z'}]}}); }
  if(p==='/xiang/turns/turn-DHzkds')
    { turnRequests.push('record:turn-DHzkds'); return route.fulfill({json:{ok:true,record}}); }
  if(p.startsWith('/xiang/turns/'))
    { turnRequests.push('record:' + p.split('/').pop());
      return route.fulfill({json:{ok:true,record:JSON.parse(JSON.stringify(record))}}); }
  if(p==='/diagram/dispatch')
    return route.fulfill({json:{ok:true,window:{from:'2026-10-19T00:00:00Z',to:'2026-10-19T03:30:00Z'},
      lanes:['joe','claude-3','zai-5','zai-6','zai-7'],bells:[
        {id:'b1',from:'claude-3',to:'zai-5',kind:'bell',rung_at:'2026-10-19T02:40:00Z',delivered_at:'2026-10-19T02:41:00Z',replied_at:'2026-10-19T02:50:00Z',state:'done'},
        {id:'b2',from:'claude-3',to:'zai-6',kind:'bell',rung_at:'2026-10-19T02:55:00Z',state:'running'},
        {id:'b3',from:'claude-3',to:'zai-7',kind:'bell',rung_at:'2026-10-19T03:10:00Z',state:'running'}],
      open:['b2','b3'],crossings:[],lawful:true,findings:{}}});
  if(p==='/usage') return route.fulfill({json:{ok:true,providers:{}}});
  if(p==='/target') return route.fulfill({json:{ok:true}});
  return route.fulfill({json:{ok:true,agents:[],choices:[],text:null}});
 });
 await page.addInitScript(() => {
   try { localStorage.removeItem('voxterm-tab'); localStorage.removeItem('voxterm-diagram-view'); } catch (_) {}
 });
 await page.goto('http://voxterm.test/');
 await page.locator('#tabDiagramBtn').click();
 await page.waitForTimeout(400);

 // default view is Turn; record for the newest turn (turn-DHzkds) loaded
 assert.equal(await page.locator('#turnSvg').isVisible(), true);
 assert.equal(await page.locator('#diagramSvg').evaluate(el => getComputedStyle(el).display), 'none');
 // the Dispatch view's window and lane controls are all hidden in Turn view
 for (const sel of ['button[data-hours="1"]', 'button[data-hours="3"]', 'button[data-hours="12"]',
                    'label:has(#diagramHideSystem)'])
   assert.equal(await page.locator('#diagramControls ' + sel).isVisible(), false, sel + ' hidden');

 // (a) one input wire per operator act of the turn
 const wires = await page.locator('#turnSvg .wire').count();
 assert.equal(wires, opActs.length);

 // (b) a wire ends at the box its alignment "by" names: answered acts connect
 const answered = align.filter(x => x.by);
 for (const al of answered) {
   const d = await page.locator(`#turnSvg .wire[data-act="${al.act}"]`).first()
     .evaluate(el => el.getAttribute('d'));
   const boxY = await page.locator(`#turnSvg rect.box[data-id="${al.by}"]`).first()
     .evaluate(el => parseFloat(el.getAttribute('y')) + parseFloat(el.getAttribute('height')) / 2);
   const endY = parseFloat(d.split(' ').pop().split(',')[1]);
   assert.ok(Math.abs(endY - boxY) < 1.5, `${al.act} ends at ${al.by}`);
 }
 // boxes: one per reply paragraph
 const replyActs = record.acts.filter(a => a.id.includes('-r-') && a.turn === record.turn_id);
 assert.equal(await page.locator('#turnSvg rect.box').count(), replyActs.length);
 // r-3 is an ask-action: it gets a "to Joe" output wire
 assert.equal(await page.locator('#turnSvg text[fill="#6cf"]').count() >= 1, true);

 // (c) not-answered acts drop to a rail below every act and every box
 const notAnswered = align.filter(x => !x.by).map(x => x.act);
 const lowest = Math.max(...await page.locator('#turnSvg rect.box').evaluateAll(
   els => els.map(el => parseFloat(el.getAttribute('y')) + parseFloat(el.getAttribute('height')))));
 for (const id of notAnswered) {
   const d = await page.locator(`#turnSvg .wire.loose[data-act="${id}"]`).first()
     .evaluate(el => el.getAttribute('d'));
   const endY = parseFloat(d.split(' ').pop().split(',')[1]);
   assert.ok(endY > lowest, `${id} ends below the last box (${endY} > ${lowest})`);
 }
 // the diagram reads top to bottom: acts in order down the page, each led
 // by its 象 mark; reply paragraphs too, with the bracketed target dropped
 const actYs = [];
 for (const a of opActs)
   actYs.push(await page.locator(`#turnSvg .wire[data-act="${a.id}"]`).first()
     .evaluate(el => parseFloat(el.getAttribute('d').split(',')[1])));
 assert.deepEqual([...actYs].sort((p, q) => p - q), actYs, 'acts run downward in order');
 const texts = await page.locator('#turnSvg text').evaluateAll(els => els.map(el => el.textContent));
 assert.ok(texts.some(t => t.startsWith('㊩ the stepper window')), 'report-problem act carries ㊩');
 assert.ok(texts.some(t => t.startsWith('🈸 Shall I load')), 'ask-action reply carries 🈸, bracket dropped');

 // (d) header counts match the alignment
 const counts = {};
 for (const a of align) counts[a.verdict] = (counts[a.verdict] || 0) + 1;
 const expectHead = '4 acts: ' + Object.entries(counts).map(([k,n]) => n+' '+k).join(', ')
                  + ' — read by 馬-sonnet';
 assert.equal(await page.locator('#turnHead').innerText(), expectHead);

 // tap a wire → detail with verdict + method
 await page.locator('#turnSvg .wire[data-act="claude-3-turn-54-f-s1-1"]').first()
   .evaluate(el => el.dispatchEvent(new MouseEvent('click', {bubbles:true})));
 assert.match(await page.locator('#diagramDetail').innerText(), /answered by claude-3-turn-54-r-0/);
 assert.match(await page.locator('#diagramDetail').innerText(), /quote/);

 // dispatched line: on the newest turn there is no next turn, so every bell
 // rung after it counts, including b3 at 03:10
 assert.equal(await page.locator('#turnDispatched').innerText(),
              'dispatched: zai-5 (done), zai-6 (running), zai-7 (running)');

 // (f) ‹ requests the previous turn's record, › the next
 await page.locator('#turnPrevBtn').click();
 await page.waitForTimeout(200);
 assert.equal(turnRequests[turnRequests.length-1], 'record:turn-DHzkds', '‹ steps to the earlier turn');
 // on turn-DHzkds the next turn starts at 03:00 ('created-at' in the list),
 // so b3 belongs to the next turn and is left out
 assert.equal(await page.locator('#turnDispatched').innerText(),
              'dispatched: zai-5 (done), zai-6 (running)');
 await page.locator('#turnNextBtn').click();
 await page.waitForTimeout(200);
 assert.equal(turnRequests[turnRequests.length-1], 'record:newer', '› steps back to the newest');

 // (e) alignment absent: header says not read yet; every act is a loose end
 await page.route('**/xiang/turns/turn-DHzkds', route =>
   route.fulfill({json:{ok:true,record:(() => {
     const bare = JSON.parse(JSON.stringify(record)); delete bare.alignment; return bare; })()}}));
 await page.evaluate(() => { turnIndex = 1; loadTurnRecord('turn-DHzkds'); });
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#turnHead').innerText(), '4 acts — not read yet');
 assert.equal(await page.locator('#turnSvg .wire.loose').count(), opActs.length);

 // an alignment naming a reply act the record lacks (live turn 59: r-0, the
 // gist paragraph) draws a placeholder box, and the answered wire ends there
 await page.route('**/xiang/turns/turn-DHzkds', route =>
   route.fulfill({json:{ok:true,record:(() => {
     const r = JSON.parse(JSON.stringify(record));
     r.acts = r.acts.filter(a => a.id !== 'claude-3-turn-54-r-0'); return r; })()}}));
 await page.evaluate(() => { loadTurnRecord('turn-DHzkds'); });
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#turnSvg rect.box[data-id="claude-3-turn-54-r-0"][stroke-dasharray]').count(), 1);
 assert.equal(await page.locator('#turnSvg .wire.loose[data-act="claude-3-turn-54-f-s1-1"]').count(), 0);

 // the turn in flight: acts are split out only after the reply, so the
 // header names the status instead of claiming no acts were recorded
 await page.route('**/xiang/turns/turn-DHzkds', route =>
   route.fulfill({json:{ok:true,record:(() => {
     const bare = JSON.parse(JSON.stringify(record)); delete bare.alignment;
     bare.acts = []; bare.analysis_status = 'requested'; return bare; })()}}));
 await page.evaluate(() => { loadTurnRecord('turn-DHzkds'); });
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#turnHead').innerText(), 'not read yet: requested');

 // (g) the Dispatch view still renders the old diagram
 await page.locator('#diagViewDispatchBtn').click();
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#diagramSvg').isVisible(), true);
 assert.equal(await page.locator('#turnSvg').evaluate(el => getComputedStyle(el).display), 'none');
 assert.equal(await page.locator('#diagramSvg path.bell').count(), 3);
 await browser.close();
 console.log('test_turn_view.cjs: all assertions passed');
 process.exit(0);
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
