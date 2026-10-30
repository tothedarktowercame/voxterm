import json, os, shutil, tempfile
import xiang_cascade as xc
fails=[]
def ck(n,c): print(("PASS " if c else "FAIL ")+n); fails.append(n) if not c else None
root=tempfile.mkdtemp(prefix="voxterm-cascade-")
turns,lib,proj=(os.path.join(root,d) for d in ("turns","library","projects"))
giraffe=os.path.join(root,"giraffe")
saved=(xc.TURN_DIR,xc.LIBRARY,xc.CLAUDE_PROJECTS,xc.GIRAFFE_ROOT)
xc.TURN_DIR,xc.LIBRARY,xc.CLAUDE_PROJECTS,xc.GIRAFFE_ROOT=turns,lib,proj,giraffe
def w(path,obj):
 os.makedirs(os.path.dirname(path),exist_ok=True)
 with open(path,"w",encoding="utf-8") as f: f.write(obj if isinstance(obj,str) else json.dumps(obj,ensure_ascii=False))
def pat(pid,title,refs=""):
 w(os.path.join(lib,pid+".flexiarg"),"@flexiarg %s\n@title %s\n%s\n! conclusion: x\n  + context:\n    mentions body/only here\n"%(pid,title,refs))
def frag(text,intent,refs=(),rej=()):
 return {"text":text,"intent":intent,"start":0,"end":len(text),
         "pattern_refs":[{"id":r,"rationale":"fits "+r} for r in refs],
         "pattern_rejections":[{"id":r,"reason":"near miss","query":"q "+r} for r in rej]}
try:
 pat("a/cited","Cited One","@references [a/near a/missing-target]")
 pat("a/near","Near One")
 pat("a/rejected","Rejected One")
 pat("a/parent","Parent One")
 pat("a/noticed","Noticed One","@see-also a/noticed-friend")
 pat("a/noticed-friend","Noticed Friend")
 pat("body/only","Body Only")
 sid="0123abcd-0000-4000-8000-000000000000"
 src="Please draw the cascade, and say which patterns were new"
 w(os.path.join(turns,"turn-T1.json"),{"turn_id":"x-turn-1","session_id":sid,"source_text":src})
 w(os.path.join(turns,"turn-T1.json.analysis.json"),{"created_at":"2026-10-19T02:00:00Z","sentences":[
   {"id":"s1","fragments":[frag("Please draw the cascade","ask-action",refs=["a/cited"],rej=["a/rejected"]),
                           frag("and say which patterns were new","clarify")]}]})
 w(os.path.join(turns,"turn-T1.json.candidates.json"),{"candidates":[
   {"id":"op/new-one","title":"New One","fragment":"s1","if":"i","then":"t","parent":"a/parent"}]})
 w(os.path.join(turns,"turn-T1.json.patterns.json"),{"Please draw":[{"id":"a/cited","score":9},{"id":"z/unjudged","score":3}]})
 # an earlier turn also cites a/cited: attestation counts 2, and T1 is not first
 w(os.path.join(turns,"turn-T0.json"),{"turn_id":"x-turn-0"})
 w(os.path.join(turns,"turn-T0.json.analysis.json"),{"created_at":"2026-10-18T00:00:00Z","sentences":[
   {"id":"s1","fragments":[frag("earlier","report",refs=["a/cited","a/near"])]}]})
 w(os.path.join(proj,"-home-x",sid+".jsonl"),
   json.dumps({"type":"user","message":{"content":"Prompt: pattern ~a/noticed (retrieved, score 0.3023; also a/ghost, a/rejected)\n---\n"+src}})+"\n")

 d=xc.cascade("turn-T1"); N={n["id"]:n for n in d["nodes"]}
 ck("fragments keyed sentence-index",[f["key"] for f in d["fragments"]]==["s1-0","s1-1"])
 ck("cited node, fragment s1-0, with rationale",N["a/cited"]["kind"]=="cited" and N["a/cited"]["roles"][0]["frag"]=="s1-0" and N["a/cited"]["roles"][0]["why"]=="fits a/cited")
 ck("cited in 2 turns, not first here",N["a/cited"]["cites"]==2 and not N["a/cited"]["first"])
 ck("rejected keeps reason and query",any(r["role"]=="rejected" and r["query"]=="q a/rejected" for r in N["a/rejected"]["roles"]))
 ck("a pattern both rejected and in the prompt draws as rejected",N["a/rejected"]["kind"]=="rejected" and any(r["role"]=="noticed" for r in N["a/rejected"]["roles"]))
 ck("proposal is new and spans the sentence's fragments",N["op/new-one"]["kind"]=="proposed" and not N["op/new-one"]["exists"]
    and sorted(r["frag"] for r in N["op/new-one"]["roles"])==["s1-0","s1-1"])
 ck("proposal -> parent edge",{"from":"op/new-one","to":"a/parent","kind":"parent"} in d["edges"] and N["a/parent"]["kind"]=="parent")
 ck("prompt picks read from the transcript",d["prompt_source"]=="transcript" and N["a/noticed"]["kind"]=="noticed"
    and N["a/noticed"]["roles"][0]["score"]==0.3023 and N["a/ghost"]["exists"] is False)
 ck("neighbour of a cited pattern: existing ids on @-lines only",{"from":"a/cited","to":"a/near","kind":"library"} in d["edges"]
    and "a/missing-target" not in N and "body/only" not in N and N["a/near"]["kind"]=="neighbour" and N["a/near"]["cites"]==1)
 ck("no neighbours drawn for a prompt pick","a/noticed-friend" not in N)
 ck("retrieval hits neither cited nor rejected are counted",d["unjudged_hits"]==1)
 trace={s["id"]:s for s in d["trace"]["stages"]}
 ck("trace witnesses the existing producer artifacts",
    all(trace[s]["state"]=="reached" for s in ("recorded","retrieved","read","proposed"))
    and trace["proposed"]["detail"]=="1 proposals")
 ck("trace does not substitute PSR or citation for mission attestation",
    trace["mission-scope"]["state"]=="blocked"
    and "turn-to-mission" in trace["mission-scope"]["detail"]
    and "PSR is not required" in trace["mission-scope"]["detail"])
 clocked=xc.processing_trace(os.path.join(turns,"turn-T1.json"),
   {"excursion_id":"E-implementing-G-over-cascades"},None,None,None)
 clocked_scope={s["id"]:s for s in clocked["stages"]}["mission-scope"]
 ck("trace carries a clocked excursion as lossy association evidence",
    clocked_scope["state"]=="waiting"
    and clocked_scope["evidence"]=={"target":"E-implementing-G-over-cascades",
                                    "basis":"turn clock projection"})
 w(os.path.join(giraffe,"run-1","work-clusters.json"),
   {"run_id":"run-1","by_item":{"E-implementing-G-over-cascades":"work-cluster/abc"},
    "clusters":[{"id":"work-cluster/abc",
                 "members":["E-implementing-G-over-cascades","M-G-over-cascades"],
                 "basis-counts":{"declared":1}}]})
 clustered=xc.processing_trace(os.path.join(turns,"turn-T1.json"),
   {"excursion_id":"E-implementing-G-over-cascades"},None,None,None)
 cluster_stage={s["id"]:s for s in clustered["stages"]}["work-cluster"]
 ck("trace joins the clocked excursion to Giraffe's evidenced cluster",
    cluster_stage["state"]=="reached"
    and cluster_stage["evidence"]["members"]==
      ["E-implementing-G-over-cascades","M-G-over-cascades"]
    and cluster_stage["evidence"]["basis_counts"]=={"declared":1})
 ck("global snapshot is the reached turn's downstream frontier",
    d["trace"]["frontier"]=="drafted" and trace["global"]["state"]=="waiting")
 failed=xc.processing_trace(os.path.join(turns,"turn-T1.json"),
   {"analysis_status":"failed","analysis_dispatch":{"outcome":{"reason":"request timed out"}}},
   None,None,None)
 failed_read={s["id"]:s for s in failed["stages"]}["read"]
 ck("failed analysis is visible rather than reported as merely absent",
    failed_read["state"]=="failed" and failed_read["detail"]=="request timed out")
 # first cite: T0 is the earliest record citing a/near
 d0=xc.cascade("turn-T0"); ck("first cite flagged on the earliest turn",{n["id"]:n for n in d0["nodes"]}["a/near"]["first"])
 ck("no transcript -> not recorded",d0["prompt_source"]=="not recorded")
 ck("record with no analysis yet",xc.cascade("turn-T0")["analysis"]=="present"
    and (w(os.path.join(turns,"turn-T2.json"),{"turn_id":"x"}) or xc.cascade("turn-T2")["analysis"]=="absent"))
 for bad in ("../etc/passwd","turn-../x","turn-a/b",""):
  ck("refuses id %r"%bad,xc.cascade(bad)=={"ok":False,"error":"bad record id"})
 ck("unknown record",xc.cascade("turn-none")["error"]=="no such record")
 # the index notices a new citation on the next call (mtime refresh)
 w(os.path.join(turns,"turn-T3.json.analysis.json"),{"created_at":"2026-10-19T03:00:00Z","sentences":[
   {"id":"s1","fragments":[frag("later","report",refs=["a/cited"])]}]})
 ck("attestation picks up a new analysis",xc.attestation("turn-T1")["a/cited"]["cites"]==3)
finally:
 xc.TURN_DIR,xc.LIBRARY,xc.CLAUDE_PROJECTS,xc.GIRAFFE_ROOT=saved; shutil.rmtree(root)
print("ALL PASS" if not fails else "FAILED: %s"%fails); raise SystemExit(1 if fails else 0)
