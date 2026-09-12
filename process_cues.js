/* Voxterm process vocabulary v1. Text requests, never completion receipts.
 * Shared by voice dispatch, keyboard input, the guide, and Node checks. */
(function (root) {
  'use strict';
  const cues = [
    {id: 'clock-in', phrase: 'clock in', symbol: '🕒',
     meaning: 'Request task entry. Identify the task, pattern/version, intended state transition and witness conditions; use the existing mission clock and report its receipt. Missing task information needs clarification. Clocking in earns no completion credit.'},
    {id: 'clock-out', phrase: 'clock out', symbol: '🏁',
     meaning: 'Request task departure. Record the actual outcome, evidence, witness verdict, pattern clause used and unresolved work; report the clock receipt. Departure does not assert success or closure.'},
    {id: 'pattern-card', phrase: 'pattern card', symbol: '🎒',
     meaning: 'Request a pattern card for this task. Name the pattern and version, relevant clause and intended use; use existing backpack/PSR facilities where available. Carriage alone is not evidence of use.'},
    {id: 'record-refusal', phrase: 'record refusal', symbol: '⛔',
     meaning: 'Request capture of a refusal. Record the attempted action, noticed tension, observation or detector receipt, withheld action and reopening condition; link an existing pattern or candidate. A detector error is not automatically a domain breach.'},
    {id: 'review-pattern', phrase: 'review pattern', symbol: '🔎',
     meaning: 'Request Layer 4 pattern review. Compare observed use and outcome with the cited clause; consider contrasting cases and affected cascade links. Propose retention, revision or retirement with evidence. Joe currently adopts workshop-family rules; a proposal enacts no sanction.'}
  ];
  function translate(text, enabled = true) {
    if (!enabled) return {text, cue: null};
    // Voice cannot express capitalization reliably. Require an utterance-leading
    // phrase; discussion, negation, quoted phrases and partial words stay literal.
    if (/^\s*literal\s+/i.test(text))
      return {text: text.replace(/^(\s*)literal\s+/i, '$1'), cue: null};
    for (const cue of cues) {
      const token = cue.symbol + ' ' + cue.phrase.toUpperCase();
      if (text === token || text.startsWith(token + ' ') || text.startsWith(token + ':'))
        return {text, cue};
      const pattern = new RegExp('^\\s*' + cue.phrase.replace(/ /g, '[\\s-]+') + '(?=$|[\\s:,.!?;])', 'i');
      if (pattern.test(text)) return {text: text.replace(pattern, token), cue};
    }
    return {text, cue: null};
  }
  function message(result) {
    if (!result.cue) return result.text;
    return result.text + '\n\n[Voxterm process cue v1: ' + result.cue.id + ' — request]\n' +
      result.cue.meaning + '\nPreserve existing gates and operator rulings. Report execution evidence or an explicit blocker; this cue itself is not evidence that the action occurred.';
  }
  root.ProcessCues = {cues, translate, message};
  if (typeof module !== 'undefined') module.exports = root.ProcessCues;
})(globalThis);
