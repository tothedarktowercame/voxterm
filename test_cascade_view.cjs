// Diagram tab, Cascade view: the patterns one turn touched, and the link from
// a Turn-view act to its patterns. Record from the xiang-turn-aligned fixture;
// the cascade payload is the shape xiang_cascade.cascade() returns.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const cascade = {ok:true, id:'turn-DHzkds', turn_id:'claude-3-turn-54', analysis:'present',
  prompt_source:'transcript', unjudged_hits:5,
  fragments:[
    {key:'s1-0', sentence:'s1', text:'the labels line up', intent:'qualify'},
    {key:'s1-1', sentence:'s1', text:'the stepper window is blank', intent:'report-problem'},
    {key:'s1-2', sentence:'s1', text:'load it again', intent:'ask-action'},
    {key:'s1-3', sentence:'s1', text:'because the cursor moved', intent:'explain'}],
  nodes:[
    {id:'a/noticed', title:'Noticed', exists:true, kind:'noticed', cites:0, first:false,
     roles:[{role:'noticed', frag:null, score:0.31}]},
    {id:'a/cited', title:'Cited Pattern', exists:true, kind:'cited', cites:4, first:false,
     roles:[{role:'cited', frag:'s1-1', why:'the window is the observation surface'}]},
    {id:'a/first', title:'First Time', exists:true, kind:'cited', cites:1, first:true,
     roles:[{role:'cited', frag:'s1-0', why:'w'}]},
    {id:'a/rejected', title:'Rejected', exists:true, kind:'rejected', cites:2, first:false,
     roles:[{role:'rejected', frag:'s1-2', why:'about planning, not doing', query:'load it again'}]},
    {id:'op/new', title:'A New One', exists:false, kind:'proposed', cites:0, first:false, parent:'a/parent',
     roles:[{role:'proposed', frag:'s1-0'}, {role:'proposed', frag:'s1-1'}, {role:'proposed', frag:'s1-2'},
            {role:'proposed', frag:'s1-3'}]},
    {id:'a/parent', title:'Parent', exists:true, kind:'parent', cites:7, first:false, roles:[]},
    {id:'a/near', title:'Near', exists:true, kind:'neighbour', cites:0, first:false, roles:[]}],
  edges:[{from:'op/new', to:'a/parent', kind:'parent'}, {from:'a/cited', to:'a/near', kind:'library'}]};
(async () => {
 const browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
 const page = await browser.newPage({viewport:{width:390,height:844}});
 const record = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/xiang-turn-aligned.json'),'utf8')).record;
 const cascadeRequests = [];
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
      {id:'turn-DHzkds', 'agent-id':'claude-3', 'created-at':'2026-10-19T01:00:00Z'},
      {id:'older', 'agent-id':'claude-3', 'created-at':'2026-10-19T00:00:00Z'}]}});
  if(p.startsWith('/xiang/turns/'))
    return route.fulfill({json:{ok:true,record:JSON.parse(JSON.stringify(record))}});
  if(p.startsWith('/xiang/cascade/')) {
    const id = p.split('/').pop(); cascadeRequests.push(id);
    return route.fulfill({json: id === 'turn-DHzkds' ? cascade
      : {...cascade, id, analysis:'absent', fragments:[], nodes:[], edges:[], unjudged_hits:0}});
  }
  if(p==='/diagram/dispatch')
    return route.fulfill({json:{ok:true,window:{from:'2026-10-19T00:00:00Z',to:'2026-10-19T03:30:00Z'},
      lanes:['joe','claude-3'],bells:[],open:[],crossings:[],lawful:true,findings:{}}});
  return route.fulfill({json:{ok:true,agents:[],choices:[],text:null,providers:{}}});
 });
 await page.addInitScript(() => {
   try { localStorage.removeItem('voxterm-tab'); localStorage.removeItem('voxterm-diagram-view'); } catch (_) {}
 });
 await page.goto('http://voxterm.test/');
 await page.locator('#tabDiagramBtn').click();
 await page.waitForTimeout(300);
 assert.equal(cascadeRequests.length, 0, 'Turn view does not fetch the cascade');

 // (a) the Cascade button swaps the views and fetches the shown turn's cascade
 await page.locator('#diagViewCascadeBtn').click();
 await page.waitForTimeout(300);
 assert.equal(await page.locator('#cascadeSvg').isVisible(), true);
 assert.equal(await page.locator('#turnSvg').evaluate(el => getComputedStyle(el).display), 'none');
 assert.deepEqual(cascadeRequests, ['turn-DHzkds']);
 assert.equal(await page.locator('#cascadeHead').innerText(),
   '2 cited · 1 considered · 1 proposed · 1 in the prompt · 5 other hits not judged');

 // (b) one box per node, sections top to bottom in reading order
 const kinds = await page.locator('#cascadeSvg .cnode').evaluateAll(els => els.map(e => e.dataset.kind));
 assert.deepEqual(kinds, ['noticed', 'cited', 'cited', 'rejected', 'proposed', 'parent', 'neighbour']);
 const ys = await page.locator('#cascadeSvg .cnode rect').evaluateAll(els => els.map(e => +e.getAttribute('y')));
 assert.deepEqual([...ys].sort((p, q) => p - q), ys, 'boxes run downward');
 const texts = async id => page.locator(`#cascadeSvg .cnode[data-id="${id}"] text`).evaluateAll(els => els.map(e => e.textContent));
 assert.ok((await texts('a/cited')).includes('cited in 4 turns'));
 assert.ok((await texts('a/first')).includes('first cite'));
 assert.ok((await texts('op/new')).includes('new'));
 assert.ok((await texts('a/cited'))[0].startsWith('㊩ a/cited'), 'a node carries its fragment\'s mark');
 // a proposal over four fragments shows two marks and a count of the rest, not a row of marks
 assert.ok((await texts('op/new'))[0].startsWith('㊟㊩+2 op/new'), (await texts('op/new'))[0]);
 assert.equal(await page.locator('#cascadeSvg .cnode[data-id="op/new"] rect[stroke-dasharray]').count(), 1, 'proposal is dashed');

 // (c) the parent edge runs from the proposal's box to the parent's box
 const centre = async id => page.locator(`#cascadeSvg .cnode[data-id="${id}"] rect`)
   .evaluate(el => +el.getAttribute('y') + +el.getAttribute('height') / 2);
 const d = await page.locator('#cascadeSvg path.cedge.parent').getAttribute('d');
 const nums = d.match(/-?[\d.]+/g).map(Number);
 assert.equal(nums[1], await centre('op/new'));
 assert.equal(nums[nums.length - 1], await centre('a/parent'));
 assert.equal(await page.locator('#cascadeSvg path.cedge.library').count(), 1);

 // (d) tapping a box gives its reason
 await page.locator('#cascadeSvg .cnode[data-id="a/rejected"] rect').dispatchEvent('click');
 const detail = await page.locator('#diagramDetail').innerText();
 assert.match(detail, /rejected for 🈸 “load it again”/);
 assert.match(detail, /about planning, not doing/);
 assert.match(detail, /query: load it again/);

 // (e) tapping an act in the Turn view lights that act's patterns here
 await page.locator('#diagViewTurnBtn').click();
 await page.waitForTimeout(200);
 await page.locator('#turnSvg .wire[data-act="claude-3-turn-54-f-s1-1"]').first()
   .evaluate(el => el.dispatchEvent(new MouseEvent('click', {bubbles:true})));
 assert.match(await page.locator('#diagramDetail').innerText(),
   /patterns: cited a\/cited, proposed op\/new — lit in Cascade/);
 await page.locator('#diagViewCascadeBtn').click();
 await page.waitForTimeout(300);
 const op = await page.locator('#cascadeSvg .cnode').evaluateAll(els =>
   Object.fromEntries(els.map(e => [e.dataset.id, e.getAttribute('opacity')])));
 assert.deepEqual(op, {'a/noticed':'0.3','a/cited':'1','a/first':'0.3','a/rejected':'0.3',
                       'op/new':'1','a/parent':'0.3','a/near':'0.3'});
 assert.match(await page.locator('#cascadeHead').innerText(), /lit: ㊩ the stepper window is blank/);
 // tapping the header clears it
 await page.locator('#cascadeHead').click();
 assert.equal(await page.locator('#cascadeSvg .cnode[opacity="0.3"]').count(), 0);

 // (f) ‹ fetches the earlier turn's cascade; one with no analysis says so
 await page.locator('#turnPrevBtn').click();
 await page.waitForTimeout(300);
 assert.equal(cascadeRequests[cascadeRequests.length - 1], 'older');
 assert.equal(await page.locator('#cascadeHead').innerText(), 'not read yet — no patterns judged');
 assert.equal(await page.locator('#cascadeSvg .cnode').count(), 0);

 assert.deepEqual(errors, []);
 await browser.close();
 console.log('test_cascade_view.cjs: all assertions passed');
 process.exit(0);
})().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
