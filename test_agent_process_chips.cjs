const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    let agents = [{id: 'claude-3', status: 'recent'}];
    let target = null;
    let processes = [{id: 'claude-3', status: 'seat', tree: {children: [{cmd: 'python3 warrant_suite.py'}]}}];
    let failed = false;
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8')});
      if (url.pathname === '/process_cues.js') return route.fulfill({contentType: 'text/javascript', body: fs.readFileSync(path.join(__dirname, 'process_cues.js'), 'utf8')});
      if (url.pathname === '/agency/active') return route.fulfill({json: {ok: true, agents, target}});
      if (url.pathname === '/agency/procs') return route.fulfill({json: {ok: !failed, agents: processes}});
      return route.fulfill({json: {ok: true, agents: [], choices: [], text: null}});
    });
    await page.goto('http://voxterm.test/');
    const chip = page.locator('#agents .ag', {hasText: 'claude-3'});
    const poll = () => page.evaluate(async () => {await refreshAgents(); await refreshProcs();});
    await poll();
    assert.match(await chip.getAttribute('class'), /background/);
    assert.doesNotMatch(await chip.innerText(), /processes running/);
    const outline = await chip.evaluate(el => getComputedStyle(el).outlineColor);
    assert.equal(await chip.evaluate(el => getComputedStyle(el).outlineWidth), '2px');
    // Selection must not mask the process indicator.
    target = {agent: 'claude-3', pinned: true};
    await poll();
    assert.equal(await chip.evaluate(el => getComputedStyle(el).outlineColor), outline);
    assert.match(await chip.innerText(), /pinned/);
    // Recent-list expiry must not drop an agent with observed children.
    target = null; agents = [];
    await poll();
    assert.equal(await chip.count(), 1);
    assert.match(await chip.getAttribute('class'), /background/);
    agents = [{id: 'claude-3', status: 'invoking'}];
    await poll();
    assert.match(await chip.getAttribute('class'), /invoking/);
    assert.doesNotMatch(await chip.getAttribute('class'), /background/);
    // An idle seat is not evidence of background work.
    agents = [{id: 'claude-3', status: 'recent'}];
    processes[0].tree.children = [];
    await poll();
    assert.doesNotMatch(await chip.getAttribute('class'), /background|invoking/);
    processes[0].tree.children = [{cmd: 'sleep 60'}];
    await poll();
    assert.match(await chip.getAttribute('class'), /background/);
    failed = true;
    await poll();
    assert.doesNotMatch(await chip.getAttribute('class'), /background/);
    failed = false; processes = []; agents = [];
    await poll();
    assert.equal(await chip.count(), 0);
    console.log('Agent chips passed: background work, selection, expiry, active turn, child exit, failed poll.');
  } finally {await browser.close();}
})().catch(err => {console.error(err); process.exit(1);});
