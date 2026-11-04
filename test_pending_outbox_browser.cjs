// A route request may still be awaiting acknowledgement when the next speech
// segment arrives.  The submitted draft must not remain the prefix of that
// new composition (live regression, claude-6, 2026-11-04).
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs'), path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage();
  const routed = [], acknowledgements = [];
  await page.route('**/*', async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8')});
    if (pathname === '/process_cues.js') return route.fulfill({contentType: 'text/javascript', body: fs.readFileSync(path.join(__dirname, 'process_cues.js'), 'utf8')});
    if (pathname === '/route') {
      routed.push(route.request().postDataJSON().text);
      await new Promise(resolve => acknowledgements.push(resolve));
      return route.fulfill({json: {ok: true}});
    }
    return route.fulfill({json: {ok: true, agents: [], choices: [], text: null}});
  });
  await page.goto('http://voxterm.test/');
  await page.evaluate(() => { localStorage.clear(); setPending(''); });
  const say = text => page.evaluate(text => handle({text, audio_sec: 1}, 1, true), text);

  await say('The first complete turn.');
  await say('Rocket.');
  await page.waitForFunction(() => pendingOutbox.length === 1);
  assert.equal(await page.evaluate(() => pendingRaw), '');

  // A stray one-word tail such as the live "eh" is now an opener and is
  // dropped by the existing noise rule; a real next turn starts clean too.
  await say('Eh.');
  assert.equal(await page.evaluate(() => pendingRaw), '');
  await say('A real next turn.');
  assert.equal(await page.evaluate(() => pendingRaw), 'A real next turn.');
  await say('Rocket.');
  await page.waitForFunction(() => pendingOutbox.length === 2);
  assert.deepEqual(routed, ['The first complete turn.', 'A real next turn.']);

  acknowledgements.splice(0).forEach(resolve => resolve());
  await page.waitForFunction(() => pendingOutbox.length === 0);
  assert.equal(await page.evaluate(() => localStorage.getItem('voxterm.pending.v1')), null);
  await browser.close();
  console.log('Pending outbox browser check passed.');
})().catch(error => { console.error(error); process.exit(1); });
