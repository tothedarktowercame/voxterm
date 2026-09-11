"""Read the WM controller's observation; never dispatch or infer worker activity."""
import json
import os


def read_queue_status(path, age, stale_s):
    if path is None:
        return {"state": "unconfigured", "configured": False}
    if not os.path.isfile(path):
        return {"state": "absent", "configured": True, "source": path}
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        def text(x):
            return isinstance(x, str) and bool(x.strip())
        if not isinstance(doc, dict):
            raise ValueError("queue observation must be an object")
        stamp_age = age(doc.get("updated_at"))
        target, roles, flight = (doc.get(k) for k in
                                 ("target", "assigned_roles", "in_flight"))
        valid = (doc.get("schema") == "wm/run4-series-queue-visibility-v1"
                 and text(doc.get("queue_id"))
                 and doc.get("controller_state") in ("running", "held", "stopped")
                 and stamp_age is not None
                 and doc.get("active_actors") == []
                 and (doc.get("hold_reason") is None or text(doc["hold_reason"]))
                 and (target is None or (isinstance(target, dict)
                     and all(text(target.get(k)) for k in
                             ("entry_id", "series_id", "manifest_sha256"))))
                 and (roles is None or (isinstance(roles, dict)
                     and all(text(roles.get(k)) for k in
                             ("author", "reviewer", "repair_reviewer"))))
                 and (flight is None or (isinstance(flight, dict)
                     and text(flight.get("entry-id"))
                     and (flight.get("click-id") is None or text(flight["click-id"])))))
        if not valid:
            raise ValueError("queue observation fields invalid")
        if doc["controller_state"] == "held" and not text(doc.get("hold_reason")):
            raise ValueError("held queue lacks reason")
        return {**doc, "configured": True, "source": path,
                "state": doc["controller_state"] if stamp_age <= stale_s else "stale",
                "fresh": stamp_age <= stale_s, "observation_age_s": stamp_age}
    except (OSError, ValueError, TypeError) as exc:
        return {"state": "invalid", "configured": True, "source": path,
                "error": str(exc)}
