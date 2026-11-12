"""The live agents and complete voice surface precede detailed run telemetry."""
from pathlib import Path


page = Path(__file__).with_name("index.html").read_text(encoding="utf-8")


def position(element_id):
    marker = 'id="%s"' % element_id
    found = page.find(marker)
    assert found >= 0, "missing element %s" % element_id
    assert page.find(marker, found + 1) < 0, "duplicate element %s" % element_id
    return found


agents = position("agents")
telemetry = position("procs")
assert agents < position("rocket") < telemetry
assert agents < position("talk") < telemetry
assert agents < position("processMnemonics") < telemetry
assert agents < position("processGuide") < telemetry
assert agents < position("log") < telemetry
print("ok")
