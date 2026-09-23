import pytest

from laya_sdlc.decisions.decider import Decider, LayaPredictor


@pytest.mark.slow
def test_real_laya_triage_wiring() -> None:
    decider = Decider(LayaPredictor(), lambda g, q: None)
    out = decider.decide("triage", {"type": "Bug", "title": "Login button broken",
                                    "description": "Clicking login does nothing.",
                                    "acceptance_criteria": "Login works."})
    assert set(out) == {"kind", "clarity", "touches_protected", "size"}
    assert all(d.shadow and not d.actionable for d in out.values())
