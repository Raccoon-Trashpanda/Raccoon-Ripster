"""Fair per-requester scheduling (ripster.runner._fair_order, issue #11).

Round-robins the pending queue across requesters (guest session token; owner and
bot-submitted tasks share the empty-string bucket) so one user's bulk batch can't
monopolise a shared service lane ahead of another user's single track. Must be a
strict no-op for 0/1 requester so single-user behaviour stays byte-for-byte FIFO.
"""
import os

from ripster.runner import _fair_order


def _t(tid, token=""):
    return {"id": tid, "_guest_token": token, "status": "queued"}


def _ids(tasks):
    return [t["id"] for t in tasks]


def test_single_requester_is_unchanged_fifo():
    pend = [_t("a"), _t("b"), _t("c")]          # all owner ("")
    assert _ids(_fair_order(pend)) == ["a", "b", "c"]


def test_empty_and_singleton_passthrough():
    assert _fair_order([]) == []
    one = [_t("solo")]
    assert _fair_order(one) is one


def test_two_requesters_round_robin():
    # Owner queued a 3-track batch first; a guest then queued one track. Plain
    # FIFO would run all 3 owner tasks before the guest's. Fair order interleaves.
    pend = [_t("o1"), _t("o2"), _t("o3"), _t("g1", "guestTok")]
    assert _ids(_fair_order(pend)) == ["o1", "g1", "o2", "o3"]


def test_fifo_preserved_within_a_requester():
    pend = [_t("o1"), _t("g1", "G"), _t("o2"), _t("g2", "G"), _t("o3")]
    out = _ids(_fair_order(pend))
    # Relative order within each requester is preserved.
    assert [x for x in out if x.startswith("o")] == ["o1", "o2", "o3"]
    assert [x for x in out if x.startswith("g")] == ["g1", "g2"]
    # And the two requesters are interleaved, not concatenated.
    assert out[0] == "o1" and out[1] == "g1"


def test_three_requesters_interleave():
    pend = [_t("a1", "A"), _t("a2", "A"), _t("b1", "B"), _t("c1", "C")]
    # buckets in first-seen order: A=[a1,a2], B=[b1], C=[c1]
    # round 0: a1,b1,c1 ; round 1: a2
    assert _ids(_fair_order(pend)) == ["a1", "b1", "c1", "a2"]


def test_kill_switch_restores_fifo(monkeypatch):
    monkeypatch.setenv("RIPSTER_FAIR_SCHED", "0")
    pend = [_t("o1"), _t("o2"), _t("g1", "G")]
    assert _ids(_fair_order(pend)) == ["o1", "o2", "g1"]
