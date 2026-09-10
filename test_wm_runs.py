import json, os, shutil, tempfile, time
import server
fails=[]
def ck(n,c): print(("PASS " if c else "FAIL ")+n); fails.append(n) if not c else None
root=tempfile.mkdtemp(prefix="voxterm-wm-"); saved=(server.WM_RUN_ROOT,server.WM_STALE_S); server.WM_RUN_ROOT,server.WM_STALE_S=root,60
try:
 d=server.wm_run_status(); ck("missing says no run evidence",d["ok"] and d["message"]=="no run evidence" and not d["run_evidence"])
 open(os.path.join(root,"PREPARATION.md"),"w").write("PREPARING")
 d=server.wm_run_status(); ck("preparation is separate",d["preparation_evidence"] and d["state"]=="absent")
 p=os.path.join(root,server.WM_RUN_STATUS_FILE); open(p,"w").write("{bad")
 ck("malformed is invalid",not server.wm_run_status()["ok"])
 for top in ([], None):
  open(p,"w").write(json.dumps(top)); ck("nonobject top level is invalid",not server.wm_run_status()["ok"])
 now=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()); x={"schema":"wm/run-visibility-v1","run_id":"RUN4-x","stage":"working","updated_at":now,"worker":"codex-3","reviewer":"codex-9","result":"pending","trials":[{"trial_id":"trial-1","stage":"review","updated_at":now,"worker":"zai-2","reviewer":"codex-8","result":"pending"}]}
 open(p,"w").write(json.dumps(x)); d=server.wm_run_status(); ck("fresh source fields survive",d["fresh"] and d["trials"][0]["worker"]=="zai-2")
 for field, bad in (("stage", []), ("result", {})):
  broken=dict(x); broken[field]=bad; open(p,"w").write(json.dumps(broken)); ck("unhashable top "+field+" is invalid",not server.wm_run_status()["ok"])
 for field, bad in (("stage", []), ("result", {})):
  broken=dict(x); broken["trials"]=[dict(x["trials"][0])]; broken["trials"][0][field]=bad; open(p,"w").write(json.dumps(broken)); ck("unhashable trial "+field+" is invalid",not server.wm_run_status()["ok"])
 future=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(time.time()+3600)); broken=dict(x); broken["updated_at"]=future; open(p,"w").write(json.dumps(broken)); ck("future run timestamp is invalid",not server.wm_run_status()["ok"])
 broken=dict(x); broken["trials"]=[dict(x["trials"][0])]; broken["trials"][0]["updated_at"]=future; open(p,"w").write(json.dumps(broken)); ck("future trial timestamp is invalid",not server.wm_run_status()["ok"])
 x["updated_at"]=x["trials"][0]["updated_at"]="2000-01-01T00:00:00Z"; open(p,"w").write(json.dumps(x)); d=server.wm_run_status(); ck("stale never green",d["state"]=="stale" and not d["fresh"])
 del x["trials"][0]["result"]; open(p,"w").write(json.dumps(x)); ck("incomplete trial invalid",not server.wm_run_status()["ok"])
finally: server.WM_RUN_ROOT,server.WM_STALE_S=saved; shutil.rmtree(root)
raise SystemExit(1 if fails else 0)
