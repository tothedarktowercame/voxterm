// With "mute mic while speaking" on, a reply that starts while Joe is mid-sentence
// must close and send the segment he was speaking, not throw it away (Joe,
// 2026-11-23: "it doesn't save the spoken text that I had submitted before it
// muted"). Drives onFrame() in the real index.html with synthetic frames.
// Run: PLAYWRIGHT_MODULE=.../node_modules/playwright node test_gate_keeps_segment_browser.cjs
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs'), path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage();
  await page.route('**/*', async route => {
    const p = new URL(route.request().url()).pathname;
    if (p === '/') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8')});
    if (p === '/process_cues.js') return route.fulfill({contentType: 'text/javascript', body: fs.readFileSync(path.join(__dirname, 'process_cues.js'), 'utf8')});
    return route.fulfill({json: {ok: true, agents: [], choices: [], text: null}});
  });
  await page.goto('http://voxterm.test/');

  // Speak 1.5 s, then a reply starts while still speaking. Returns how many
  // samples were queued for transcription.
  const run = gate => page.evaluate(gate => {
    mode = 'always'; calibrating = 0; threshold = 0.014; vad = 'silence';
    ring = []; segFrames = []; above = 0; below = 0; framesSinceClose = Infinity;
    speaking = false; queue = [];
    let sent = 0;
    window.drain = () => { for (const q of queue) sent += q.length; queue = []; };
    document.querySelector('#gatemic').checked = gate;
    const loud = () => new Float32Array(FRAME).fill(0.2);
    for (let i = 0; i < 24; i++) onFrame(loud());          // ~1.5 s of speech
    const before = vad;
    speaking = true;                                        // reply begins
    for (let i = 0; i < 10; i++) onFrame(loud());          // mic hears the TTS
    return {before, after: vad, sent, left: segFrames.length};
  }, gate);

  const gated = await run(true);
  assert.equal(gated.before, 'speech');
  assert.equal(gated.after, 'silence');
  assert.ok(gated.sent >= 24 * 1024, 'segment in progress was sent: ' + gated.sent);
  assert.ok(gated.sent <= 24 * 1024, 'no TTS frames were added after the gate: ' + gated.sent);
  assert.equal(gated.left, 0);

  await browser.close();
  console.log('Gate keeps the segment in progress: passed.');
})().catch(e => { console.error(e); process.exit(1); });
