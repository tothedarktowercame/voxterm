"""Regression test: resume seats use known owners, never departed cached names."""
import io,json,os,sys,types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

JVM=999
S1="b2de8138-7b54-421d-80f2-17930ec1ae7b"   # claude-1
S2="f7568a13-a03e-40cf-863b-90460a091b91"   # claude-11

def make_agents(include=("claude-1","claude-11")):
    a={}
    if "claude-1" in include:
        a["k1"]={"id":{"id/value":"claude-1"},"session-id":S1,"status":"idle",
                 "registered-at":"2026-08-31T15:45:26.955487461Z"}
    if "claude-11" in include:
        a["k2"]={"id":{"id/value":"claude-11"},"session-id":S2,"status":"invoking",
                 "registered-at":"2026-09-05T03:48:29.236998767Z"}
    return {"agents":a}

def install(ps_lines, agents_payload):
    class FakeResp:
        def __init__(self,obj): self.obj=obj
        def read(self): return json.dumps(self.obj).encode()
        def __enter__(self): return self
        def __exit__(self,*a): return False
    def fake_urlopen(url,timeout=None):
        if "/agents" in url: return FakeResp(agents_payload)
        if "/invoke/jobs" in url: return FakeResp({"jobs":[]})
        raise AssertionError(url)
    def fake_run(cmd,**kw):
        out=""
        if cmd[0]=="ss": out="LISTEN 0 50 0.0.0.0:7070 0.0.0.0:* users:((\"java\",pid=%d,fd=1))\n"%JVM
        elif cmd[0]=="ps": out=ps_lines
        return types.SimpleNamespace(stdout=out,returncode=0)
    server.urlopen=fake_urlopen
    server.subprocess=types.SimpleNamespace(run=fake_run)
    # json.load(r) is called on our FakeResp -> give json.load a shim
    server.json=types.SimpleNamespace(load=lambda r: json.loads(r.read()),
                                      loads=json.loads,dumps=json.dumps)

SEAT="claude --print --input-format stream-json --permission-mode bypassPermissions --resume %s"
def ps_for(rows):
    # pid ppid etimes pcpu comm args
    return "".join("%d %d %d 0.0 %s %s\n"%(r[0],r[1],r[2],r[3],r[4]) for r in rows)

fails=[]
def check(name,cond,detail=""):
    print(("  PASS  " if cond else "  FAIL  ")+name+(""if cond else"  <- "+detail))
    if not cond: fails.append(name)

print("scenario A: one agent with TWO live seats (old turn + new turn)")
server._SESSION_OWNERS.clear()
install(ps_for([(JVM,1,9999,"java","java -jar agency.jar"),
                (100,JVM,1800,"claude",SEAT%S1),
                (101,JVM,   5,"claude",SEAT%S1)]), make_agents())
r=server.agency_procs()
ids=sorted(x["id"] for x in r["agents"]); um=r["unmatched"]
check("both seats attributed to claude-1", ids==["claude-1","claude-1"], str(ids))
check("nothing unmatched", um==[], json.dumps(um)[:200])

# Reverses the earlier "retain attribution through a registry blink" expectation.
# On 2026-09-07, claude-10 was displayed for /tmp/f10-unblock-watch.sh even
# though it had left the roster; its b2de8138 session (S1 here) had been taken
# over by claude-1. The known_agent docstring inside server.agency_procs explains
# the ruling: naming a departed agent is worse than leaving a visible gap.
# A single poll cannot distinguish a blink from retirement, so cache alone
# must not preserve the missing name.
print("scenario B: departed roster owner is not resurrected from the session memo")
server._SESSION_OWNERS.clear()
install(ps_for([(JVM,1,9999,"java","java -jar agency.jar"),
                (100,JVM,1735,"claude",SEAT%S1)]), make_agents())
server.agency_procs()                       # warm poll: memo learns S1 -> claude-1
install(ps_for([(JVM,1,9999,"java","java -jar agency.jar"),
                (100,JVM,1740,"claude",SEAT%S1)]), make_agents(include=("claude-11",)))
r=server.agency_procs()
named=[x["id"] for x in r["agents"]]
likely=[t.get("likely-agent") for t in r["unmatched"]]
check("departed owner is not named from the session memo",
      named==[], "agents=%s"%named)
check("departed owner's seat remains unmatched without a guessed owner",
      len(r["unmatched"])==1 and likely==[None],
      "unmatched=%s"%json.dumps(r["unmatched"])[:200])

print("scenario C: genuinely unknown session (agent not in registry, cold memo)")
server._SESSION_OWNERS.clear()
install(ps_for([(JVM,1,9999,"java","java -jar agency.jar"),
                (100,JVM,1740,"claude",SEAT%"deadbeef-0000-0000-0000-000000000000")]),
        make_agents(include=()))
r=server.agency_procs()
check("unknown session still degrades to unmatched (no fabrication)",
      len(r["unmatched"])==1 and r["unmatched"][0].get("likely-agent") is None,
      json.dumps(r["unmatched"])[:200])

print("scenario D: JVM-started resident service is named, not treated as a seat")
server._SESSION_OWNERS.clear()
install(ps_for([(JVM,1,9999,"java","java -jar agency.jar"),
                (200,JVM,157000,"python3","/x/.venv/bin/python3 -u /x/scripts/notions_search.py --resident --embeddings e.json")]),
        make_agents())
r=server.agency_procs()
um=r["unmatched"]
check("service row carries its name",
      len(um)==1 and (um[0].get("service") or {}).get("name")=="pattern search", json.dumps(um)[:200])
check("service is not attributed to an agent", r["agents"]==[], str(r["agents"]))

print()
print("RESULT:", "ALL PASS" if not fails else "FAILURES: %s"%fails)
sys.exit(1 if fails else 0)
