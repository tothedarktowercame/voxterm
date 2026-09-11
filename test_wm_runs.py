import json, os, shutil, tempfile, time
import server
fails=[]
def ck(n,c): print(("PASS " if c else "FAIL ")+n); fails.append(n) if not c else None
root=tempfile.mkdtemp(prefix="voxterm-wm-"); saved=(server.WM_RUN_ROOT,server.WM_STALE_S,server.WM_SOURCE_CONFIG); server.WM_RUN_ROOT,server.WM_STALE_S=root,60
server.WM_SOURCE_CONFIG=os.path.join(root,"source.json")
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
 historical=json.loads(json.dumps(x)); row=historical["trials"][0]; row.update(stage="review", result="pending")
 row["historical_execution"]={"kind":"historical-repair-verification","status":"completed","resolution_status":"awaiting-successor-validation",**{k:k+"-058" for k in ("click_id","run_id","controller_attempt_id","runner_attempt_id","cohort_id","repair_id","verification_id")}}
 row["actual_action"]={"type":"revalidate-historical-repair","repair_id":"repair_id-058"}
 row["requested_task"]={"trial_id":row["trial_id"],"status":"authenticated-not-enacted"}
 row["assigned_roles"]={"author":"codex-10","reviewer":"codex-12","repair_reviewer":"codex-12","active_workers":[]}
 open(p,"w").write(json.dumps(historical)); d=server.wm_run_status()
 ck("historical completion remains pending and requested not enacted",d["trials"][0]["historical_execution"]["status"]=="completed" and d["result"]=="pending" and d["trials"][0]["requested_task"]["status"]=="authenticated-not-enacted")
 row["actual_action"]["repair_id"]="foreign"
 open(p,"w").write(json.dumps(historical)); ck("foreign enacted repair refused",not server.wm_run_status()["ok"])
 row["actual_action"]["repair_id"]="repair_id-058"; row["result"]="passed"
 open(p,"w").write(json.dumps(historical)); ck("historical task success refused",not server.wm_run_status()["ok"])
 for field, bad in (("stage", []), ("result", {})):
  broken=dict(x); broken[field]=bad; open(p,"w").write(json.dumps(broken)); ck("unhashable top "+field+" is invalid",not server.wm_run_status()["ok"])
 for field, bad in (("stage", []), ("result", {})):
  broken=dict(x); broken["trials"]=[dict(x["trials"][0])]; broken["trials"][0][field]=bad; open(p,"w").write(json.dumps(broken)); ck("unhashable trial "+field+" is invalid",not server.wm_run_status()["ok"])
 future=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(time.time()+3600)); broken=dict(x); broken["updated_at"]=future; open(p,"w").write(json.dumps(broken)); ck("future run timestamp is invalid",not server.wm_run_status()["ok"])
 broken=dict(x); broken["trials"]=[dict(x["trials"][0])]; broken["trials"][0]["updated_at"]=future; open(p,"w").write(json.dumps(broken)); ck("future trial timestamp is invalid",not server.wm_run_status()["ok"])
 x["updated_at"]=x["trials"][0]["updated_at"]="2000-01-01T00:00:00Z"; open(p,"w").write(json.dumps(x)); d=server.wm_run_status(); ck("stale never green",d["state"]=="stale" and not d["fresh"])
 selected=os.path.join(root,"enacted"); os.mkdir(selected)
 with open(os.path.join(selected,"run-visibility.json"),"w") as h: json.dump(x,h)
 with open(server.WM_SOURCE_CONFIG,"w") as h: json.dump({"schema":"voxterm/wm-source-v1","root":selected},h)
 d=server.wm_run_status(); ck("explicit enacted source selected without refresh of evidence",d["run_evidence"] and d["source"].startswith(selected) and not d["fresh"])
 with open(server.WM_SOURCE_CONFIG,"w") as h: h.write('{bad')
 ck("bad source does not fall back to preparation",server.wm_run_status()["state"]=="invalid")
 with open(server.WM_SOURCE_CONFIG,"w") as h: json.dump({"schema":"voxterm/wm-source-v1","root":root},h)
 ck("source change read on next poll",server.wm_run_status()["source"]==p)
 del x["trials"][0]["result"]; open(p,"w").write(json.dumps(x)); ck("incomplete trial invalid",not server.wm_run_status()["ok"])
finally: server.WM_RUN_ROOT,server.WM_STALE_S,server.WM_SOURCE_CONFIG=saved; shutil.rmtree(root)
raise SystemExit(1 if fails else 0)
