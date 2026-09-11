import json
import pathlib
import subprocess
import tempfile
import unittest


class OutcomeTest(unittest.TestCase):
    def project(self, terminal=None, bank=None, close=None):
        with tempfile.TemporaryDirectory() as d:
            for rel, text in [('terminal/frame-terminal.edn', terminal),
                              ('terminal/problem-bank.edn', bank), ('live/close-frame.edn', close)]:
                if text is not None:
                    p = pathlib.Path(d)/rel
                    p.parent.mkdir(exist_ok=True)
                    p.write_text(text)
            r = subprocess.run(['bb', 'apm_outcomes.clj', d], capture_output=True, text=True, check=True)
            return json.loads(r.stdout)[d]

    def test_rejected_close_is_not_closed(self):
        r = self.project(close='{:receipt {:receipt/result :partial}}')
        self.assertEqual('closure unconfirmed; guide submitted partial', r['end'])
        self.assertFalse(r['banked'])

    def test_partial_learning_can_have_banked_solver(self):
        t = '{:receipt/type :frame-terminal :receipt/id "t" :frame/result :partial :problem/outcome :solved}'
        b = '{:receipt/type :queued-problem-bank :source/terminal-receipt-id "t" :problem/outcome :solved :solve/pin-status :pinned}'
        r = self.project(t, b)
        self.assertTrue(r['banked'])
        self.assertEqual('partial learning frame; solver banked', r['end'])
        self.assertFalse(self.project(t, b.replace('"t"', '"other"'))['banked'])

    def test_closed_without_bank_is_not_banked(self):
        r = self.project('{:receipt/type :frame-terminal :receipt/id "t" :frame/result :closed}')
        self.assertFalse(r['banked'])
        self.assertEqual('closed; bank unconfirmed', r['end'])

if __name__ == '__main__':
    unittest.main()
