"""The patterns one 象 turn touched, for the Diagram tab's Cascade view.

Everything here is read from files 象 and the seats already write; nothing
is asked of the JVM.

Per turn, beside the record RECORD.json in TURN_DIR:
  RECORD.json.analysis.json    象's reading: fragments, each with
                               pattern_refs (cited) and pattern_rejections
                               (near misses, with reason and query)
  RECORD.json.candidates.json  proposed new patterns, each naming a parent
  RECORD.json.patterns.json    the BM25 hits retrieved per draft clause
The prompt retriever's pick ("Prompt: pattern X (retrieved, score S; also
Y, Z)") is not kept by 象; for a Claude seat it survives in the session
transcript, so it is read from there, and reported as not recorded otherwise.

Attestation (how many turns cite a pattern, and whether this turn was the
first) comes from an index over every analysis file, rebuilt incrementally by
mtime. Library neighbours, drawn for cited and rejected patterns only, are
the existing pattern ids named on a pattern's @-directive lines (@why, @references, @see-also, ...), one step out.
"""
import glob
import json
import os
import re
import threading

TURN_DIR = os.path.expanduser(os.environ.get(
    "VOXTERM_XIANG_TURN_DIR", "~/.emacs-graph/session-turn-analysis"))
LIBRARY = os.path.expanduser(os.environ.get(
    "VOXTERM_PATTERN_LIBRARY", "~/code/futon3/library"))
CLAUDE_PROJECTS = os.path.expanduser(os.environ.get(
    "VOXTERM_CLAUDE_PROJECTS", "~/.claude/projects"))

RECORD_ID = re.compile(r"^turn-[A-Za-z0-9_-]{1,40}$")
PATTERN_ID = re.compile(r"[A-Za-z0-9][\w.-]*/[\w.-]+[\w]")
NEIGHBOURS_MAX = 4


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _artifact(path):
    """A trace witness which is safe to return to the browser."""
    try:
        return {"path": path, "mtime": os.path.getmtime(path)}
    except OSError:
        return None


def processing_trace(base, record, analysis, proposals_doc, retrieved):
    """How far the durable processing of one operator turn has gone.

    This reports witnesses; it does not infer that a later stage happened from
    an earlier one.  In particular a citation is not mission attestation.
    Mission attestation needs a turn->mission link before the mission's
    explicit pattern scope can be joined to this trace.
    """
    draft_path = base + ".draft.json"
    patterns_path = base + ".patterns.json"
    analysis_path = base + ".analysis.json"
    candidates_path = base + ".candidates.json"
    reply = record.get("reply_text")
    mission = (record.get("excursion_id") or record.get("mission_id") or
               record.get("campaign_id") or record.get("clocked_target") or
               record.get("mission"))
    analysis_status = record.get("analysis_status")
    analysis_failure = ((record.get("analysis_dispatch") or {}).get("outcome") or {}).get("reason")

    def stage(stage_id, label, state, evidence=None, detail=None):
        row = {"id": stage_id, "label": label, "state": state}
        if evidence:
            row["evidence"] = evidence
        if detail:
            row["detail"] = detail
        return row

    rows = [
        stage("recorded", "turn recorded", "reached", _artifact(base)),
        stage("drafted", "draft produced",
              "reached" if _artifact(draft_path) else "waiting",
              _artifact(draft_path)),
        stage("retrieved", "patterns retrieved",
              "reached" if _artifact(patterns_path) else "waiting",
              _artifact(patterns_path),
              "%d retrieval clauses" % len(retrieved) if retrieved is not None else None),
        stage("read", "turn read by 象",
              "reached" if analysis is not None else
              "failed" if analysis_status == "failed" else "waiting",
              _artifact(analysis_path),
              analysis_failure if analysis is None and analysis_status == "failed" else None),
        stage("proposed", "proposals recorded",
              "reached" if proposals_doc is not None else "waiting",
              _artifact(candidates_path),
              "%d proposals" % len((proposals_doc or {}).get("candidates") or [])),
        stage("replied", "agent reply attached",
              "reached" if isinstance(reply, str) and reply.strip() else "waiting",
              {"field": "reply_text", "reply_at": record.get("reply_at")}
              if isinstance(reply, str) and reply.strip() else None),
    ]

    if mission:
        rows.append(stage(
            "mission-scope", "mission pattern scope joined", "waiting",
            {"target": mission,
             "basis": "turn clock projection"},
            "clock association found; explicit :applied scope has not been joined"))
    else:
        rows.append(stage(
            "mission-scope", "mission pattern scope joined", "blocked",
            detail="no explicit turn-to-mission link; PSR is not required"))
    rows.append(stage(
        "global", "global candidate state updated", "waiting",
        detail="no versioned global-cascade snapshot exists"))
    rows.append(stage(
        "governed", "candidate disposition governed", "waiting",
        detail="no promotion, merge, revision, or rejection record exists"))

    # The frontier is the first unfinished producer stage.  Downstream stages
    # stay visible even when an earlier optional producer (such as a reply) is
    # absent, so the trace shows the actual system boundary rather than a
    # fictitious linear workflow.
    core = rows[:5]
    frontier = next((r["id"] for r in core if r["state"] != "reached"), "global")
    return {"stages": rows, "frontier": frontier}


# --- library ------------------------------------------------------------
_lib_cache = {}


def pattern_info(pid):
    """{'exists', 'title', 'links'} for library pattern PID, cached by mtime."""
    path = os.path.join(LIBRARY, pid + ".flexiarg")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"exists": False, "title": None, "links": []}
    hit = _lib_cache.get(pid)
    if hit and hit[0] == mtime:
        return hit[1]
    title, links = None, []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.startswith("@"):
                    if line.startswith("!"):
                        break           # header ends at the conclusion
                    continue
                if line.startswith("@title "):
                    title = line[len("@title "):].strip()
                elif not line.startswith("@flexiarg "):
                    for m in PATTERN_ID.findall(line):
                        if m != pid and m not in links \
                                and os.path.exists(os.path.join(LIBRARY, m + ".flexiarg")):
                            links.append(m)
    except OSError:
        pass
    info = {"exists": True, "title": title, "links": links}
    _lib_cache[pid] = (mtime, info)
    return info


# --- attestation index --------------------------------------------------
_cite_lock = threading.Lock()
_cite_files = {}     # analysis path -> (mtime, created_at, [cited ids])


def _refresh_cites():
    paths = glob.glob(os.path.join(TURN_DIR, "turn-*.json.analysis.json"))
    live = set(paths)
    for p in list(_cite_files):
        if p not in live:
            del _cite_files[p]
    for p in paths:
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        old = _cite_files.get(p)
        if old and old[0] == mtime:
            continue
        a = _read_json(p) or {}
        ids = []
        for s in a.get("sentences") or []:
            for fr in s.get("fragments") or []:
                for r in fr.get("pattern_refs") or []:
                    rid = r.get("id") if isinstance(r, dict) else r
                    if isinstance(rid, str) and rid not in ids:
                        ids.append(rid)
        _cite_files[p] = (mtime, a.get("created_at") or "", ids)


def attestation(record_id):
    """{pattern id: {'cites': N turns, 'first': record_id was the earliest}}."""
    with _cite_lock:
        _refresh_cites()
        rows = list(_cite_files.items())
    out, earliest = {}, {}
    for path, (_, at, ids) in rows:
        rid = os.path.basename(path)[:-len(".json.analysis.json")]
        for pid in ids:
            out.setdefault(pid, {"cites": 0, "first": False})["cites"] += 1
            if pid not in earliest or at < earliest[pid][0]:
                earliest[pid] = (at, rid)
    for pid, (_, rid) in earliest.items():
        out[pid]["first"] = rid == record_id
    return out


# --- the prompt retriever's pick ----------------------------------------
PROMPT_PATTERN = re.compile(
    r"pattern ~?(\S+) \(retrieved, score ([\d.]+)(?:; also ([^)]*))?\)")


def prompt_patterns(record):
    """(source, [{'id', 'score'}]) from the seat's transcript, if one is kept."""
    sid, text = record.get("session_id"), record.get("source_text") or ""
    if not sid or not re.match(r"^[0-9a-f-]{8,64}$", str(sid)) or not text:
        return "not recorded", []
    files = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", sid + ".jsonl"))
    if not files:
        return "not recorded", []
    head = text[:60]
    needles = {json.dumps(head, ensure_ascii=False)[1:-1],
               json.dumps(head)[1:-1]}
    try:
        with open(files[0], encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Prompt: pattern" not in line or not any(n in line for n in needles):
                    continue
                m = PROMPT_PATTERN.search(line)
                if not m:
                    continue
                rows = [{"id": m.group(1), "score": float(m.group(2))}]
                for also in (m.group(3) or "").split(","):
                    also = also.strip().lstrip("~")
                    if also:
                        rows.append({"id": also, "score": None})
                return "transcript", rows
    except OSError:
        return "not recorded", []
    return "not in transcript", []


# --- one turn -----------------------------------------------------------
ROLE_ORDER = ["cited", "proposed", "rejected", "noticed"]


def cascade(record_id):
    if not RECORD_ID.match(record_id or ""):
        return {"ok": False, "error": "bad record id"}
    base = os.path.join(TURN_DIR, record_id + ".json")
    record = _read_json(base)
    if record is None:
        return {"ok": False, "error": "no such record"}
    analysis = _read_json(base + ".analysis.json")
    proposals_doc = _read_json(base + ".candidates.json")
    proposals = (proposals_doc or {}).get("candidates") or []
    retrieved_doc = _read_json(base + ".patterns.json")
    retrieved = retrieved_doc or {}

    fragments, by_sentence = [], {}
    nodes = {}

    def node(pid, title=None):
        n = nodes.get(pid)
        if n is None:
            info = pattern_info(pid)
            n = nodes[pid] = {"id": pid, "title": info["title"] or title,
                              "exists": info["exists"], "roles": [], "kind": None}
        elif not n["title"] and title:
            n["title"] = title
        return n

    for s in (analysis or {}).get("sentences") or []:
        for i, fr in enumerate(s.get("fragments") or []):
            key = "%s-%d" % (s.get("id"), i)
            fragments.append({"key": key, "sentence": s.get("id"),
                              "start": fr.get("start"), "end": fr.get("end"),
                              "text": fr.get("text"), "intent": fr.get("intent"),
                              "target": fr.get("target")})
            by_sentence.setdefault(s.get("id"), []).append(key)
            for r in fr.get("pattern_refs") or []:
                r = r if isinstance(r, dict) else {"id": r}
                if isinstance(r.get("id"), str):
                    node(r["id"])["roles"].append(
                        {"role": "cited", "frag": key, "why": r.get("rationale")})
            for r in fr.get("pattern_rejections") or []:
                if isinstance(r, dict) and isinstance(r.get("id"), str):
                    node(r["id"])["roles"].append(
                        {"role": "rejected", "frag": key, "why": r.get("reason"),
                         "query": r.get("query")})

    edges = []
    for c in proposals:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            continue
        n = node(c["id"], c.get("title"))
        frag = c.get("fragment")
        keys = by_sentence.get(frag) or [f["key"] for f in fragments
                                         if frag in (f["key"], f["text"])]
        why = " ".join(x for x in ("If " + c["if"] if c.get("if") else "",
                                   "Then " + c["then"] if c.get("then") else "") if x)
        for k in keys or [None]:
            n["roles"].append({"role": "proposed", "frag": k, "why": why or None})
        n["parent"] = c.get("parent")
        if isinstance(c.get("parent"), str):
            p = node(c["parent"])
            edges.append({"from": c["id"], "to": p["id"], "kind": "parent"})

    source, picks = prompt_patterns(record)
    for p in picks:
        node(p["id"])["roles"].append({"role": "noticed", "frag": None,
                                       "score": p["score"]})

    # retrieval hits that 象 neither cited nor rejected: counted, not drawn
    unjudged = 0
    for hits in retrieved.values():
        for h in hits or []:
            if isinstance(h, dict) and h.get("id") not in nodes:
                unjudged += 1

    att = attestation(record_id)
    # neighbours are drawn only for what 象 judged (cited or rejected); the
    # prompt picks and proposals would otherwise bury them
    focus = [n for n in nodes.values()
             if any(r["role"] in ("cited", "rejected") for r in n["roles"])]
    for n in nodes.values():
        roles = [r["role"] for r in n["roles"]]
        n["kind"] = next((k for k in ROLE_ORDER if k in roles), "parent")
        a = att.get(n["id"], {"cites": 0, "first": False})
        n["cites"], n["first"] = a["cites"], a["first"] and "cited" in roles
    # library neighbours of the judged patterns, one step out
    for n in focus:
        if not n["exists"]:
            continue
        for m in pattern_info(n["id"])["links"][:NEIGHBOURS_MAX]:
            if m not in nodes:
                nodes[m] = {"id": m, "title": pattern_info(m)["title"], "exists": True,
                            "roles": [], "kind": "neighbour",
                            "cites": att.get(m, {}).get("cites", 0), "first": False}
            if not any(e["from"] == n["id"] and e["to"] == m for e in edges):
                edges.append({"from": n["id"], "to": m, "kind": "library"})

    return {"ok": True, "id": record_id, "turn_id": record.get("turn_id"),
            "analysis": "present" if analysis else "absent",
            "fragments": fragments, "nodes": list(nodes.values()), "edges": edges,
            "prompt_source": source, "unjudged_hits": unjudged,
            "trace": processing_trace(base, record, analysis, proposals_doc,
                                      retrieved_doc)}
