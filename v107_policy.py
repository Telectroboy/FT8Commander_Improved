#!/usr/bin/env python3
"""
FT8Commander V10.7.4 policy correction.

Purpose
-------
1. A proactive ATTEMPT that transmitted but never received a direct answer
   becomes a real TargetPolicy failure.  The existing 300/600/1200/1800 s
   backoff is therefore authoritative.
2. CQ/RRR/RR73/73 cannot re-arm a never-answered proactive ATTEMPT while this
   backoff is active.  The existing ENGAGED-QSO continuation path is untouched.
3. A proactive ATTEMPT heard working another station is failed/backed off
   instead of pinning the band with a 90 s pursuit busy hold.
4. QSY_PENDING is cancelled only after a candidate has passed all final gates
   and start_candidate() has actually succeeded.

This module deliberately layers on top of V10.6.1 instead of duplicating the
TXDF/CAT logic.
"""

from __future__ import annotations

import logging
import time
from typing import Any

LOG = logging.getLogger(__name__)
MARKER = "FT8Commander V10.7.4 proactive backoff + transactional QSY"

_FAILURE_REASONS = {
    "proactive TX window complete",
    "proactive four-TX burst complete",
    "proactive DX short-term success probability too low",
    "V6 pursuit target busy; wait for terminal/CQ",
}


def _state_name(obj: Any) -> str:
    state = getattr(obj, "state", None)
    return str(getattr(state, "value", state or "")).upper()


def _profile(obj: Any) -> str:
    # TargetPolicy in the current runtime uses the numeric profile/band key.
    band = getattr(obj, "band", "")
    text = str(band or "").lower()
    if text.endswith("m"):
        text = text[:-1]
    return text


def _is_direct(policy: Any, data: Any) -> bool:
    if not data:
        return False
    try:
        return bool(policy.is_direct(data))
    except Exception:
        source = str((data or {}).get("source") or "").lower()
        return source == "direct"


def _policy_backoff(obj: Any, data: Any) -> tuple[bool, float, str]:
    """Return (blocked, remaining, reason), considering TargetPolicy and local fence."""
    if not data:
        return False, 0.0, ""
    now = time.monotonic()
    until = float((data or {}).get("v107_backoff_until")
                  or (data or {}).get("v55_blocked_until") or 0.0)
    if until > now:
        return True, until - now, "V10.7.4 proactive backoff"

    policy = getattr(obj, "v55_target_policy", None)
    if policy is None or _is_direct(policy, data):
        return False, 0.0, ""
    try:
        ok, reason, remaining = policy.eligible(data, _profile(obj))
    except Exception:
        return False, 0.0, ""
    reason_text = str(reason or "")
    if not ok and "backoff" in reason_text.lower():
        return True, max(0.0, float(remaining or 0.0)), reason_text
    return False, 0.0, ""


def before_clear(obj: Any, reason: str) -> None:
    """Called from the *base* clear_current so V6 captured original_clear sees it too."""
    current = getattr(obj, "current", None)
    if not current or not current.get("proactive"):
        return
    if _state_name(obj) != "ATTEMPT":
        # Critical: never convert an ENGAGED continuation/failure into the
        # proactive no-answer policy.
        return

    reason_text = str(reason or "")
    if reason_text not in _FAILURE_REASONS:
        return

    actual_tx = int(getattr(obj, "current_tx_attempts", 0) or 0)
    if actual_tx < 1:
        # No RF sent => no radio failure.
        return

    call = str(current.get("call") or "").upper()
    if not call:
        return

    attempt_key = (
        call,
        float(getattr(obj, "current_started_at", 0.0) or 0.0),
    )
    if getattr(obj, "_v107_last_failed_attempt_key", None) == attempt_key:
        return
    obj._v107_last_failed_attempt_key = attempt_key

    policy = getattr(obj, "v55_target_policy", None)
    if policy is None:
        LOG.error("V10.7.4 cannot record proactive failure for %s: TargetPolicy missing", call)
        return

    # v55_clear() runs before this base clear hook. If actual_tx already meets
    # TargetPolicy.min_failed_tx, V10.6.1 has therefore recorded the failure
    # itself; do not count it twice. The V10.7.4 gap is specifically the short
    # 1/2-TX proactive window that V10.6.1 classifies as neutral/interrupted.
    min_failed_tx = max(1, int(getattr(policy, "min_failed_tx", 1) or 1))
    delay = 0.0
    if actual_tx >= min_failed_tx:
        try:
            ok, why, remaining = policy.eligible(current, _profile(obj))
            if not ok and "backoff" in str(why or "").lower():
                delay = max(0.0, float(remaining or 0.0))
        except Exception:
            LOG.exception("V10.7.4 could not read existing TargetPolicy backoff for %s", call)
    else:
        # Adapt the short V10.6.1 RF window to the inherited legacy threshold.
        # This records exactly one failure while retaining actual_tx in the log.
        logical_tx = max(actual_tx, min_failed_tx)
        try:
            delay = float(policy.note_failure(
                call,
                _profile(obj),
                logical_tx,
                f"V10.7.4 proactive unanswered: actual_tx={actual_tx}; {reason_text}",
            ) or 0.0)
        except Exception:
            LOG.exception("V10.7.4 TargetPolicy.note_failure failed for %s", call)
            return

    # Defensive local fence. Normally TargetPolicy now provides the current
    # cooldown. If not, use only the first schedule item; never synthesize a
    # second failure just to obtain a delay.
    if delay <= 0.0:
        schedule = list(getattr(policy, "backoff_schedule", ()) or ())
        delay = float(schedule[0] if schedule else 300.0)

    now = time.monotonic()
    until = now + delay

    target = getattr(obj, "proactive_targets", {}).get(call)
    if target is not None:
        target["waiting_event"] = True
        target["rearm_after_burst"] = False
        target["v55_blocked_until"] = until
        target["v107_backoff_until"] = until
        try:
            obj._remove_proactive_from_queue(call)
        except Exception:
            pass

    # Mark this pursuit exhausted and erase the busy hold.  In the existing
    # V10.6 runtime v60_begin_busy_wait_band_hold() refuses an exhausted record,
    # so the later ATTEMPT-busy code cannot pin the band for another 90 s.
    rec = getattr(obj, "v60_pursuit", {}).get(call)
    if rec is not None:
        rec["exhausted"] = True
        rec["waiting"] = False
        rec["busy_hold_started"] = 0.0
        rec["busy_hold_until"] = 0.0
        rec["busy_band"] = None
        rec["busy_hold_log_at"] = 0.0

    fresh = getattr(obj, "v60_fresh_rf_priority", None)
    if isinstance(fresh, dict) and str(fresh.get("call") or "").upper() == call:
        obj.v60_fresh_rf_priority = None

    LOG.warning(
        "V10.7.4 PROACTIVE BACKOFF %s: no direct reply after %d actual TX; "
        "cooldown=%.0fs reason=%s",
        call, actual_tx, delay, reason_text,
    )


def install(Sequencer: Any) -> None:
    """Install the post-V10.6.1 policy wrappers exactly once.

    QSY transaction ordering is patched directly in v60_runtime.py by the
    installer. This module intentionally does not introspect or mutate the
    runtime closure.
    """
    if getattr(Sequencer, "_v107_policy_installed", False):
        return

    original_queue = getattr(Sequencer, "queue_proactive_target", None)
    if original_queue is not None:
        def queue_proactive_target(self, call, *args, **kwargs):
            call_key = str(call or "").upper()
            target = getattr(self, "proactive_targets", {}).get(call_key)
            blocked, remaining, why = _policy_backoff(self, target)
            if blocked:
                try:
                    self._remove_proactive_from_queue(call_key)
                except Exception:
                    pass
                if target is not None:
                    target["waiting_event"] = True
                    target["rearm_after_burst"] = False
                LOG.info(
                    "V10.7.4 REARM BLOCK %s: %s remaining=%.0fs",
                    call_key, why, remaining,
                )
                return False
            return original_queue(self, call, *args, **kwargs)
        Sequencer.queue_proactive_target = queue_proactive_target

    original_start = getattr(Sequencer, "start_candidate", None)
    if original_start is not None:
        def start_candidate(self, data, reason):
            policy = getattr(self, "v55_target_policy", None)
            if data and data.get("proactive") and policy is not None and not _is_direct(policy, data):
                target = getattr(self, "proactive_targets", {}).get(
                    str(data.get("call") or "").upper()
                )
                fence_data = target if target is not None else data
                blocked, remaining, why = _policy_backoff(self, fence_data)
                if blocked:
                    LOG.info(
                        "V10.7.4 START BLOCK %s: %s remaining=%.0fs; QSY preserved",
                        data.get("call"), why, remaining,
                    )
                    return False
            return original_start(self, data, reason)
        Sequencer.start_candidate = start_candidate

    original_mark_engaged = getattr(Sequencer, "mark_engaged", None)
    if original_mark_engaged is not None:
        def mark_engaged(self, *args, **kwargs):
            call_before = str(((getattr(self, "current", None) or {}).get("call")) or "").upper()
            result = original_mark_engaged(self, *args, **kwargs)
            if _state_name(self) == "ENGAGED" and call_before:
                target = getattr(self, "proactive_targets", {}).get(call_before)
                if target is not None:
                    target.pop("v107_backoff_until", None)
                    target["v107_was_engaged"] = True
                rec = getattr(self, "v60_pursuit", {}).get(call_before)
                if rec is not None:
                    rec["v107_was_engaged"] = True
            return result
        Sequencer.mark_engaged = mark_engaged

    Sequencer._v107_policy_installed = True
    LOG.info("%s installed", MARKER)


# ------------------------------- self-test -------------------------------

def _self_test() -> None:
    class FakeState:
        value = "ATTEMPT"

    class FakePolicy:
        backoff_schedule = [300, 600, 1200, 1800]
        min_failed_tx = 4

        def __init__(self):
            self.failures = 0
            self.until = 0.0

        def is_direct(self, data):
            return str((data or {}).get("source") or "") == "direct"

        def note_failure(self, call, profile, tx_count, reason):
            self.failures += 1
            delay = self.backoff_schedule[min(self.failures - 1, len(self.backoff_schedule) - 1)]
            self.until = time.monotonic() + delay
            return delay

        def eligible(self, data, profile):
            remaining = self.until - time.monotonic()
            if remaining > 0:
                return False, "backoff", remaining
            return True, "ok", 0.0

    class FakeSequencer:
        def __init__(self):
            self.state = FakeState()
            self.band = "12"
            self.tx_retries = 4
            self.current_tx_attempts = 2
            self.current_started_at = 123.0
            self.current = {"call": "CO8LY", "proactive": True}
            self.proactive_targets = {
                "CO8LY": {"call": "CO8LY", "proactive": True, "band": "12"}
            }
            self.proactive_queue = ["CO8LY"]
            self.v55_target_policy = FakePolicy()
            self.v60_pursuit = {
                "CO8LY": {
                    "exhausted": False,
                    "waiting": True,
                    "busy_hold_started": 1.0,
                    "busy_hold_until": time.monotonic() + 90.0,
                    "busy_band": "12",
                }
            }
            self.v60_fresh_rf_priority = {"call": "CO8LY"}

        def _remove_proactive_from_queue(self, call):
            self.proactive_queue = [c for c in self.proactive_queue if c != call]

        def queue_proactive_target(self, call, *args, **kwargs):
            self.proactive_queue.append(call)
            return True

        def start_candidate(self, data, reason):
            return True

        def mark_engaged(self, *args, **kwargs):
            self.state.value = "ENGAGED"
            return True

    install(FakeSequencer)
    s = FakeSequencer()

    before_clear(s, "proactive TX window complete")
    assert s.v55_target_policy.failures == 1
    assert s.v60_pursuit["CO8LY"]["exhausted"] is True
    assert s.v60_pursuit["CO8LY"]["busy_hold_until"] == 0.0
    assert s.queue_proactive_target("CO8LY") is False
    assert s.start_candidate(s.proactive_targets["CO8LY"], "test") is False

    failures_before = s.v55_target_policy.failures
    s.state.value = "ENGAGED"
    s.current = {"call": "ENGAGEDDX", "proactive": True}
    s.current_tx_attempts = 7
    before_clear(s, "proactive TX window complete")
    assert s.v55_target_policy.failures == failures_before

    print("V10.7.4 self-test: OK")


if __name__ == "__main__":
    _self_test()
