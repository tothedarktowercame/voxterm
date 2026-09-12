const assert = require('node:assert/strict');
const {cues, translate, message} = require('./process_cues.js');
for (const cue of cues) {
  const r = translate(cue.phrase.toUpperCase() + ' on row 6.');
  assert.equal(r.cue.id, cue.id);
  assert.equal(r.text, cue.symbol + ' ' + cue.phrase.toUpperCase() + ' on row 6.');
  assert.ok(message(r).includes(cue.meaning));
  assert.ok(message(r).includes('— request]'));
  assert.ok(message(r).includes('not evidence that the action occurred'));
}
for (const text of ['Do not clock in.',
                    '"Clock in" is a phrase.', 'Clock inside the room.',
                    'Review patterns later.', 'Rocket.']) {
  assert.deepEqual(translate(text), {text, cue: null});
}
assert.equal(translate('Clock-in: row 6').cue.id, 'clock-in');
assert.equal(translate('Clock in.').cue.id, 'clock-in');
assert.equal(translate('clock\nin on row 6').cue.id, 'clock-in');
assert.deepEqual(translate('Literal clock in on row 6'), {text: 'clock in on row 6', cue: null});
assert.deepEqual(translate('Clock in', false), {text: 'Clock in', cue: null});
assert.deepEqual(translate(translate('Clock in on row 6').text), translate('Clock in on row 6'));
assert.equal(message(translate('ordinary text')), 'ordinary text');
console.log('Process cue tests passed: requests, meanings, contrasts, literal escape and disabled mode.');

const multi = translate('Okay, clock in on row 6, then pattern card. Examine pattern. Pattern card.');
assert.equal(multi.text, 'Okay, 🕒 CLOCK IN on row 6, then 🎒 PATTERN CARD. 🔎 REVIEW PATTERN. 🎒 PATTERN CARD.');
assert.deepEqual(multi.cues.map(c => c.id), ['clock-in', 'pattern-card', 'review-pattern']);
assert.equal((message(multi).match(/pattern-card — request/g) || []).length, 1);
assert.equal(translate('Please say literal pattern card, then examine pattern.').text,
 'Please say pattern card, then 🔎 REVIEW PATTERN.');
assert.equal(translate('♪♪ Clock in').text, '♪♪ 🕒 CLOCK IN');
assert.deepEqual(translate(multi.text), multi);
