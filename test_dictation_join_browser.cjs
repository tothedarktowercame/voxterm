// Dictated chunks join without the stop whisper puts at each pause, and the
// retract button teaches the page which phrases whisper invents in silence.
// Drives the real index.html: handle() gets transcripts as the server returns
// them, and the assertions read what reaches /route. Chunks are from Joe's
// dictation of 2026-09-25.
// Run: PLAYWRIGHT_MODULE=.../node_modules/playwright node test_dictation_join_browser.cjs
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs'), path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage();
  const routed = [];
  await page.route('**/*', async route => {
    const p = new URL(route.request().url()).pathname;
    if (p === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8')});
    if (p === '/process_cues.js') return route.fulfill({contentType: 'text/javascript', body: fs.readFileSync(path.join(__dirname, 'process_cues.js'), 'utf8')});
    if (p === '/route') { routed.push(route.request().postDataJSON().text); return route.fulfill({json: {ok: true}}); }
    return route.fulfill({json: {ok: true, agents: [], choices: [], text: null}});
  });
  await page.goto('http://voxterm.test/');
  await page.evaluate(() => { localStorage.clear(); phantoms = []; renderPhantoms(); });
  const say = t => page.evaluate(t => handle({text: t, audio_sec: 1}, 1, true), t);
  const sent = async () => { await page.waitForTimeout(50); return routed.at(-1); };

  // 1. joins: stop removed, next word lowered, names and ? kept, last stop kept
  await say("And that's a known problem and I'm not sure much could be done because the.");
  await say('Model hallucinates that they exist.');
  await say('I handed it to.');
  await say('Kimi and asked why?');
  await say('It worked. Rocket.');
  assert.equal(await sent(), "And that's a known problem and I'm not sure much could be done because the model hallucinates that they exist I handed it to Kimi and asked why? It worked.");

  // 2. an ellipsis is a real trail-off, not an invented stop
  await say('And then...');
  await say('Nothing. Rocket.');
  assert.equal(await sent(), 'And then... Nothing.');

  // 3. retract learns the short chunks, not the long real one
  await say('Thanks for listening, bye.');
  await say('This is a long real sentence that I changed my mind about sending.');
  await page.dispatchEvent('#retract', 'click');
  assert.deepEqual(await page.evaluate(() => phantoms), ['thanks for listening bye']);
  assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('voxterm.phantoms'))), ['thanks for listening bye']);
  assert.equal(await page.locator('#phantomCount').innerText(), '1');

  // 4. the learned phrase is dropped whole; the same words inside dictation survive
  await say('Thanks for listening. Bye!');
  assert.equal(await page.evaluate(() => pendingRaw), '');
  await say('I said thanks for listening, bye. Rocket.');
  assert.equal(await sent(), 'I said thanks for listening, bye.');

  // 5. tapping the entry forgets it
  await page.dispatchEvent('#phantomList button', 'click');
  assert.deepEqual(await page.evaluate(() => phantoms), []);
  await say('Thanks for listening, bye.');
  assert.equal(await page.evaluate(() => pendingRaw), 'Thanks for listening, bye.');

  await browser.close();
  console.log('Dictation join + blocked-phrase browser checks passed.');
})().catch(e => { console.error(e); process.exit(1); });
