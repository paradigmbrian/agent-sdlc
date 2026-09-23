"""Record one real Laya response for the triage gate so tests pin the actual output shape."""
import json
from pathlib import Path

from laya import Router

from laya_sdlc.decisions.gates import GATES

STATE = {
    "type": "Bug",
    "title": "Timesheet approve button does nothing on later pending weeks",
    "description": "In Teams > Pending, clicking Approve on any week after the first does "
                   "nothing. Expected: the week is approved and removed from the list.",
    "acceptance_criteria": "Approve works for every pending week; unit test covers week 2+.",
}

out = Router(preload=True).predict(STATE, GATES["triage"])
path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "laya_triage_sample.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({"state": STATE, "response": out}, indent=2, default=str))
print(json.dumps(out["answers"], indent=2, default=str))
