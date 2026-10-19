// Diagram tab: the cross-agent dispatch diagram over stubbed dispatch data.
// Style of test_frames_tab.cjs: route-fulfil index.html, stub diagram/dispatch
// with claude-3's saved 1-hour sample.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
 const browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
 const page = await browser.newPage({viewport:{width:390,height:844}});
 const sample = JSON.parse(fs.readFileSync(
   process.env.DIAGRAM_SAMPLE || '/home/joe/notes/packets/ma-sonnet/dispatch-sample-1h.json','utf8'));
 const diagramRequests = [];
 await page.route('**/*', async route => {
  const u = new URL(route.request().url());
  const p = u.pathname;
  if(p==='/') return route.fulfill({contentType:'text/html',body:fs.readFileSync(require('node:path').join(__dirname, 'index.html'),'utf8')});
  if(p==='/process_cues.js') return route.fulfill({contentType:'text/javascript',body:fs.readFileSync(require('node:path').join(__dirname, 'process_cues.js'),'utf8')});
  if(p==='/diagram/dispatch') {
    diagramRequests.push(u.searchParams.get('hours'));
    return route.fulfill({json:sample});
  }
  if(p==='/target') return route.fulfill({json:{ok:true}});
  return route.fulfill({json:{ok:true,agents:[],choices:[],text:null}});
 });
 await page.addInitScript(() => { try { localStorage.removeItem('voxterm-tab'); } catch (_) {} });
 await page.goto('http://voxterm.test/');
 await page.waitForTimeout(300);

 // (e) nothing fetched before the tab is opened.
 assert.equal(diagramRequests.length, 0);

 await page.locator('#tabDiagramBtn').click();
 await page.waitForTimeout(400);
 assert.equal(diagramRequests.length >= 1, true, 'first open fetches');
 assert.equal(diagramRequests[0], '3', 'default window is 3h');
 assert.equal(await page.locator('#viewDiagram').isVisible(), true);

 // (a) one lane label per lane after the system toggle (checked by default);
 //     joe first; system lanes hidden.
 const visibleLanes = await page.locator('#diagramSvg [data-lane]').evaluateAll(
   els => els.map(e => e.dataset.lane));
 const expected = sample.lanes.filter(l => !l.startsWith('system:'));
 assert.equal(visibleLanes[0], 'joe');
 assert.equal(visibleLanes.length, expected.length);
 for (const l of expected) assert.ok(visibleLanes.includes(l), l);
 assert.equal(await page.locator('#diagramSvg [data-lane="system:auto-bellback"]').count(), 0);

 // unchecking shows the system lanes too.
 await page.locator('#diagramHideSystem').uncheck();
 assert.equal(await page.locator('#diagramSvg [data-lane]').count(), sample.lanes.length);
 await page.locator('#diagramHideSystem').check();

 // (b) drawn bells = bells whose lanes are visible; dashed = open among them.
 const hidden = sample.lanes.filter(l => l.startsWith('system:'));
 const inWindow = b => !hidden.includes(b.from) && !hidden.includes(b.to);
 const expectedBells = sample.bells.filter(inWindow);
 const drawn = await page.locator('#diagramSvg .bell').count();
 const drawnIds = await page.locator('#diagramSvg .bell').evaluateAll(
   els => [...new Set(els.map(e => e.dataset.id))]);
 assert.equal(drawnIds.length, expectedBells.length);
 const openDrawn = await page.locator('#diagramSvg path.bell[stroke-dasharray]').count();
 assert.equal(openDrawn, sample.open.filter(id => {
   const b = sample.bells.find(x => x.id === id); return b && inWindow(b);
 }).length);
 assert.match(await page.locator('#diagramHead').innerText(), /lawful — 79 bells, 5 open/);

 // (c) tapping a bell shows its id in the detail box (dispatch a real
 // bubbling click; some drawn paths have sub-pixel hit boxes).
 const someId = drawnIds[0];
 await page.locator(`#diagramSvg [data-id="${someId}"]`).first()
   .evaluate(el => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
 assert.equal(await page.locator('#diagramDetail').isVisible(), true);
 assert.match(await page.locator('#diagramDetail').innerText(), new RegExp(someId));

 // (d) switching window to 3h-ish: pick 1h then back to 3h, assert hours param.
 await page.locator('#diagramControls button[data-hours="1"]').click();
 await page.waitForTimeout(200);
 assert.equal(diagramRequests[diagramRequests.length - 1], '1');
 await page.locator('#diagramControls button[data-hours="3"]').click();
 await page.waitForTimeout(200);
 assert.equal(diagramRequests[diagramRequests.length - 1], '3');

 // lane tap moves the cursor via the existing target POST.
 let targetPost = null;
 await page.route('**/target', route => {
   targetPost = route.request().postDataJSON();
   return route.fulfill({json:{ok:true}});
 });
 await page.locator('#diagramSvg [data-lane="claude-3"]').click();
 await page.waitForTimeout(200);
 assert.deepEqual(targetPost, {agent:'claude-3', mode:'focus'});
 assert.equal(drawn > 0, true);
 await browser.close();
 console.log('test_diagram_tab.cjs: all assertions passed');
 process.exit(0);
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
