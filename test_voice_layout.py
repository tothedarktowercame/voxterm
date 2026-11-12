"""The live agents and complete voice surface precede detailed run telemetry."""
from pathlib import Path


page = Path(__file__).with_name("index.html").read_text(encoding="utf-8")


def position(element_id):
    marker = 'id="%s"' % element_id
    found = page.find(marker)
    assert found >= 0, "missing element %s" % element_id
    assert page.find(marker, found + 1) < 0, "duplicate element %s" % element_id
    return found


voice_modes = position("voiceModes")
agents = position("agents")
telemetry = position("procs")
assert voice_modes < agents < telemetry
assert voice_modes < position("rocket") < agents
assert voice_modes < position("talk") < agents
assert agents < position("processMnemonics") < telemetry
assert agents < position("processGuide") < telemetry
assert agents < position("log") < telemetry

for retired in ("tabs", "tabAgentsBtn", "tabFramesBtn", "tabDiagramBtn",
                "viewFrames", "viewDiagram", "framesFrame", "diagramSvg"):
    assert 'id="%s"' % retired not in page, "retired view remains: %s" % retired

server = Path(__file__).with_name("server.py").read_text(encoding="utf-8")
for retired_path in ("/diagram/dispatch", "/xiang/cascade/", "/xiang/turns",
                     "/xiang/card"):
    assert retired_path not in server, "retired route remains: %s" % retired_path
print("ok")
