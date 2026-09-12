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
for (const text of ['Do not clock in.', 'We discussed clock in yesterday.',
                    '"Clock in" is a phrase.', 'Clock inside the room.',
                    'Review patterns later.', 'Rocket.', '♪♪ Clock in']) {
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
