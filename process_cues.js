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
    {id: 'review-pattern', phrase: 'review pattern', aliases: ['examine pattern'], symbol: '🔎',
     meaning: 'Request Layer 4 pattern review. Compare observed use and outcome with the cited clause; consider contrasting cases and affected cascade links. Propose retention, revision or retirement with evidence. Joe currently adopts workshop-family rules; a proposal enacts no sanction.'}
  ];
  function translate(text, enabled = true) {
    if (!enabled) return {text, cue: null};
    // A leading "literal" escapes the whole message. Elsewhere it escapes
    // the immediately following cue. Keep raw input separately in the UI.
    if (/^\s*literal\s+/i.test(text))
      return {text: text.replace(/^(\s*)literal\s+/i, '$1'), cue: null};
    const phrases = cues.flatMap(cue => [cue.phrase, ...(cue.aliases || [])]
      .map(phrase => ({cue, phrase})));
    const pattern = new RegExp('\\b(?:literal\\s+)?(?:' + phrases.map(p =>
      p.phrase.replace(/ /g, '[\\s-]+')).join('|') + ')\\b', 'gi');
    const found = new Map();
    const rendered = text.replace(pattern, (match, offset) => {
      if (/^literal\s+/i.test(match)) return match.replace(/^literal\s+/i, '');
      const before = text.slice(0, offset), after = text.slice(offset + match.length);
      if (/(?:\bdo not|\bdon't|\bnot|\bnever)\s+$/i.test(before) ||
          /["“]$/.test(before) || /^["”]/.test(after)) return match;
      const normalized = match.toLowerCase().replace(/[\s-]+/g, ' ');
      const cue = phrases.find(p => p.phrase === normalized).cue;
      found.set(cue.id, cue);
      // A rendered cue pasted back in must not acquire another emoji.
      if (before.endsWith(cue.symbol + ' ')) return cue.phrase.toUpperCase();
      return cue.symbol + ' ' + cue.phrase.toUpperCase();
    });
    const matched = [...found.values()];
    return matched.length ? {text: rendered, cue: matched[0], cues: matched}
      : {text: rendered, cue: null};
  }
  function message(result) {
    if (!result.cue) return result.text;
    const matched = result.cues || [result.cue];
    return result.text + matched.map(cue =>
      '\n\n[Voxterm process cue v1: ' + cue.id + ' — request]\n' + cue.meaning).join('') +
      '\nInterpret cues in the surrounding message: discussion, examples and negation do not request execution. Preserve existing gates and operator rulings. For an actual request, report execution evidence or an explicit blocker; this cue itself is not evidence that the action occurred.';
  }
  root.ProcessCues = {cues, translate, message};
  if (typeof module !== 'undefined') module.exports = root.ProcessCues;
})(globalThis);
