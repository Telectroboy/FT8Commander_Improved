#!/usr/bin/env python3
"""FT8Commander V10.7.6 terminal-repeat + mandatory-revisit policy.

Terminal QSO rules
------------------
* Directed received messages are parsed in WSJT-X order:
    F4EGM YB7FII RR73 / RRR / 73
  i.e. destination (our call), source (remote call), payload.
* A real ENGAGED QSO remains ENGAGED after RRR/RR73 until our final 73 has
  actually entered and left the WSJT-X transmitting state.
* The 22 s receive grace starts only after that TX has ended.
* A fresh directed RRR/RR73 from the same remote station after each completed
  final-73 TX requests exactly one more final 73. There is no arbitrary retry
  count limit: each further 73 requires a new RF RRR/RR73 after the preceding
  73 completed.
* A directed remote 73 confirms the terminal exchange and closes immediately
  once we are not transmitting.
* A sliding watchdog protects against a stuck pre-final state but is refreshed
  by valid terminal activity; it never imposes a retry-count limit.

Mandatory revisit rules
-----------------------
* A mandatory-revisit QSY cannot be cancelled or delayed by CQ/proactive/wanted
  selection. Proactive start_candidate() is blocked while that QSY is pending.
* An already ENGAGED QSO is left untouched; the runtime safety gates defer QSY.
* A real direct candidate (source=direct) always cancels mandatory revisit.
* Entering MANUAL_OVERRIDE always cancels mandatory revisit.
* Non-priority/safety cancellation reasons remain handled by the base runtime.

This module does not change the V10 TXDF hole planner. Repeated terminal 73s use
its already-prepared current-target TXDF state and require a successful VS1
pre-arm when TXDF is enabled.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

LOG = logging.getLogger(__name__)
MARKER = "FT8Commander V10.7.6 terminal-repeat + mandatory-revisit"
TERMINAL_GRACE = 22.0
TAIL_WATCHDOG = 75.0

# Cancellation reasons known to mean "a normal selected/wanted candidate wants
# the band". While a mandatory revisit is pending these reasons are refused.
_PRIORITY_CANCEL_MARKERS = (
    "fresh priority target",
    "eligible target committed",
    "target selection",
    "wanted target",
    "proactive",
)


def _state_name(obj: Any) -> str:
    state = getattr(obj, "state", None)
    return str(getattr(state, "value", state or "")).upper()


def _norm_call(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    return text


def _is_direct_data(data: Any) -> bool:
    return str((data or {}).get("source") or "").strip().lower() == "direct"


def _render_value(value: Any, depth: int = 0) -> str:
    if depth >= 3:
        return ""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            parts.append(str(key))
            parts.append(_render_value(item, depth + 1))
        return " ".join(parts)
    if isinstance(value, (list, tuple, set)):
        return " ".join(_render_value(item, depth + 1) for item in value)
    try:
        return str(value)
    except Exception:
        return ""


def _qsy_snapshot(obj: Any) -> tuple[bool, str]:
    """Return (pending_like, text) using only qsy-related runtime attributes.

    V6 keeps QSY intent state inside the Sequencer object, while the helper
    functions themselves are closure-local. This deliberately avoids assuming
    one exact attribute spelling across V6/V8/V10.6.1 builds.
    """
    try:
        items = list(vars(obj).items())
    except Exception:
        return False, ""

    pending_like = False
    texts = []
    for name, value in items:
        lname = str(name).lower()
        if "qsy" not in lname:
            continue
        if value in (None, False, "", 0):
            continue
        rendered = _render_value(value)
        if rendered:
            texts.append(f"{name}={rendered}")
        if "pending" in lname or "intent" in lname:
            pending_like = True
    return pending_like, " | ".join(texts)


def mandatory_revisit_pending(obj: Any) -> bool:
    pending_like, text = _qsy_snapshot(obj)
    return bool(pending_like and "mandatory revisit" in text.lower())


def _manual_override_active(obj: Any) -> bool:
    """Detect the V5.5 manual-ownership latch without hard-coding one revision."""
    try:
        items = vars(obj).items()
    except Exception:
        return False
    for name, value in items:
        lname = str(name).lower()
        if "manual" in lname and "override" in lname and bool(value):
            return True
    return False


def _log_preserved_cancel(obj: Any, reason: str) -> None:
    now = time.monotonic()
    last = float(getattr(obj, "v1076_last_qsy_preserve_log", 0.0) or 0.0)
    last_reason = str(getattr(obj, "v1076_last_qsy_preserve_reason", "") or "")
    if reason != last_reason or now - last >= 5.0:
        setattr(obj, "v1076_last_qsy_preserve_log", now)
        setattr(obj, "v1076_last_qsy_preserve_reason", reason)
        LOG.info(
            "V10.7.6 MANDATORY REVISIT PRESERVE: refused priority cancellation (%s)",
            reason,
        )


def qsy_cancel_allowed(obj: Any, reason: Any) -> bool:
    """Guard injected into closure-local v60_cancel_qsy_intent()."""
    if not mandatory_revisit_pending(obj):
        return True

    text = str(reason or "")
    low = text.lower()

    # Explicit overrides required by the user.
    if "v10.7.6 direct incoming" in low or "v10.7.6 manual override" in low:
        LOG.info("V10.7.6 MANDATORY REVISIT CANCEL allowed: %s", text)
        return True

    # Existing base/manual safety reasons are not priority-target cancellations.
    # Refuse only candidate/CQ/proactive ownership paths.
    if any(marker in low for marker in _PRIORITY_CANCEL_MARKERS):
        _log_preserved_cancel(obj, text)
        return False

    return True


def _cancel_qsy(obj: Any, reason: str) -> bool:
    fn = getattr(obj, "v1076_cancel_qsy_intent", None)
    if not callable(fn):
        LOG.error("V10.7.6 QSY CANCEL unavailable: runtime bridge missing (%s)", reason)
        return False
    try:
        result = fn(reason)
    except Exception:
        LOG.exception("V10.7.6 QSY CANCEL failed: %s", reason)
        return False
    return result is not False


def _tail(obj: Any):
    tail = getattr(obj, "v1076_terminal_tail", None)
    if not isinstance(tail, dict):
        return None
    current = getattr(obj, "current", None) or {}
    if _norm_call(current.get("call")) != _norm_call(tail.get("call")):
        return None
    return tail


def _touch_watchdog(tail: dict) -> None:
    tail["watchdog_until"] = time.monotonic() + TAIL_WATCHDOG


def _new_tail(obj: Any, call: str):
    now = time.monotonic()
    tail = {
        "call": _norm_call(call),
        "started_at": now,
        "watchdog_until": now + TAIL_WATCHDOG,
        "last_terminal": None,
        "last_terminal_data": None,
        "terminal_rx_count": 0,
        "final_tx_count": 0,
        "final_tx_active": False,
        "final_tx_started_at": 0.0,
        "last_final_tx_end": 0.0,
        "grace_until": 0.0,
        # One terminal response may request at most one retry for each completed
        # final TX. After the next final TX completes, a new RRR/RR73 may request
        # another one, with no arbitrary count ceiling.
        "retry_pending": False,
        "retry_pending_for_tx": 0,
        "retry_consumed_for_tx": 0,
        "retry_requests": 0,
        "remote_explicit_73": False,
        "logged": False,
        "blocked_clear_count": 0,
    }
    obj.v1076_terminal_tail = tail
    LOG.info("V10.7.6 TERMINAL TAIL %s: ENGAGED retained through real final-73 TX", call)
    return tail


def _ensure_tail(obj: Any, call: str):
    tail = _tail(obj)
    if tail is not None:
        return tail
    return _new_tail(obj, call)


def _terminal_from_packet(obj: Any, packet: Any, expected_call: str):
    """Return a terminal token only for MYCALL REMOTECALL TOKEN.

    Example received from YB7FII to F4EGM:
        F4EGM YB7FII RR73
    Never accept the inverted YB7FII F4EGM RR73 form as a received directed
    message to us.
    """
    message = str(getattr(packet, "Message", "") or "")
    for segment in (part.strip() for part in message.split(";")):
        try:
            kind, match = obj.parse_segment(segment)
        except Exception:
            continue
        if kind != "REPLY" or not match:
            continue
        if _norm_call(match.get("to")) != _norm_call(getattr(obj, "mycall", "")):
            continue
        if _norm_call(match.get("call")) != _norm_call(expected_call):
            continue
        payload = list(match.get("payload") or [])
        token = str(payload[-1]).upper() if payload else ""
        if token not in {"RRR", "RR73", "73"}:
            continue
        data = dict(getattr(obj, "current", None) or {})
        packet_dict = {}
        try:
            packet_dict = packet.as_dict().copy()
        except Exception:
            pass
        data.update({
            "call": _norm_call(expected_call),
            "time": getattr(packet, "Time", None),
            "snr": getattr(packet, "SNR", None),
            "band": getattr(obj, "band", data.get("band")),
            "frequency": getattr(obj, "frequency", data.get("frequency")),
            "packet": packet_dict,
            "source": "terminal-73-retry",
            "proactive": False,
        })
        return token, data
    return None, None


def _is_final_73_tx(obj: Any, packet: Any, call: str) -> bool:
    if not bool(getattr(packet, "Transmitting", False)):
        return False
    message = str(getattr(packet, "TxMessage", "") or "").strip().upper()
    tokens = [_norm_call(tok) for tok in message.split()]
    if not tokens or tokens[-1] != "73":
        return False
    # Outgoing WSJT-X text is REMOTECALL MYCALL 73.
    return _norm_call(call) in tokens and _norm_call(getattr(obj, "mycall", "")) in tokens


def _finish_tail(obj: Any, original_clear: Any, reason: str) -> bool:
    tail = _tail(obj)
    if tail is None or bool(getattr(obj, "transmitting", False)):
        return False
    call = tail["call"]
    try:
        obj.drop_proactive_target(call, "terminal QSO completed")
    except Exception:
        pass
    obj.v1076_terminal_tail = None
    obj.current_terminal_seen = True
    LOG.info(
        "V10.7.6 TERMINAL COMPLETE %s: final73_tx=%d retries=%d reason=%s",
        call,
        int(tail.get("final_tx_count", 0)),
        int(tail.get("retry_requests", 0)),
        reason,
    )
    original_clear(
        obj,
        "terminal message completed; V10.7.6 final-73 tail complete",
        delete_candidate=False,
    )
    if hasattr(obj, "decision_due_at"):
        obj.decision_due_at = time.monotonic() + float(getattr(obj, "decision_settle_time", 0.12))
    return True


def _send_forced_final_retry(obj: Any, tail: dict) -> bool:
    if not bool(tail.get("retry_pending")):
        return False
    if bool(getattr(obj, "transmitting", False)):
        return False

    completed_tx = int(tail.get("retry_pending_for_tx", 0) or 0)
    if completed_tx <= 0 or completed_tx != int(tail.get("final_tx_count", 0) or 0):
        tail["retry_pending"] = False
        tail["retry_pending_for_tx"] = 0
        return False
    if int(tail.get("retry_consumed_for_tx", 0) or 0) >= completed_tx:
        tail["retry_pending"] = False
        tail["retry_pending_for_tx"] = 0
        return False

    data = tail.get("last_terminal_data")
    if not data:
        tail["retry_pending"] = False
        tail["retry_pending_for_tx"] = 0
        LOG.error("V10.7.6 FINAL 73 RETRY %s: missing terminal decode packet", tail.get("call"))
        return False

    armed_here = False
    engine = getattr(obj, "v60_txdf", None)
    if engine is not None and bool(getattr(engine, "enabled", False)):
        prepared = (
            getattr(obj, "v60_saved_radio_state", None) is not None
            and getattr(obj, "v60_txdf_prepared_sub", None) is not None
            and getattr(obj, "v60_txdf_tx_slot", None) is not None
            and _norm_call(getattr(obj, "v60_txdf_prepared_call", "")) == _norm_call(tail.get("call"))
        )
        if not prepared:
            LOG.error(
                "V10.7.6 FINAL 73 RETRY %s withheld: TXDF prepared state no longer matches ENGAGED target",
                tail.get("call"),
            )
            return False
        if not bool(getattr(obj, "v60_txdf_active", False)):
            arm = getattr(obj, "v60_arm_txdf", None)
            if not callable(arm) or not arm("V10.7.6 repeated terminal -> another final 73", prestart=True):
                LOG.error(
                    "V10.7.6 FINAL 73 RETRY %s withheld: TXDF VS1 prearm failed",
                    tail.get("call"),
                )
                return False
            armed_here = True

    next_request = int(tail.get("retry_requests", 0) or 0) + 1
    LOG.info(
        "V10.7.6 FINAL 73 RETRY %s: new directed %s after final TX %d; requesting another 73 (retry=%d)",
        tail.get("call"), tail.get("last_terminal") or "RR73", completed_tx, next_request,
    )

    ok = bool(obj.call_station(
        getattr(obj, "last_ip_from", None),
        data,
        reason="V10.7.6 repeated terminal -> another final 73",
    ))
    if not ok and armed_here:
        disarm = getattr(obj, "v60_disarm_txdf", None)
        if callable(disarm):
            disarm("V10.7.6 WSReply retry rejected")
    if ok:
        # Consume this remote terminal only once. A later retry is allowed only
        # after the newly requested 73 itself completes RF and a *new* RRR/RR73
        # arrives afterwards.
        tail["retry_requests"] = next_request
        tail["retry_consumed_for_tx"] = completed_tx
        tail["retry_pending"] = False
        tail["retry_pending_for_tx"] = 0
        tail["grace_until"] = 0.0
        _touch_watchdog(tail)
    return ok


def install(Sequencer: Any) -> None:
    if getattr(Sequencer, "_v1076_terminal_revisit_installed", False):
        return

    original_init = Sequencer.__init__
    original_mark_engaged = Sequencer.mark_engaged
    original_process_decode = Sequencer.process_decode
    original_process_status = Sequencer.process_status
    original_check_timeouts = Sequencer.check_timeouts
    original_clear = Sequencer.clear_current
    original_start_candidate = getattr(Sequencer, "start_candidate", None)

    def init(self, *args, **kwargs):
        result = original_init(self, *args, **kwargs)
        self.v1076_terminal_tail = None
        self.v1076_last_qsy_preserve_log = 0.0
        self.v1076_last_qsy_preserve_reason = ""
        return result

    def start_candidate(self, data, reason):
        if mandatory_revisit_pending(self):
            call = _norm_call((data or {}).get("call")) or "?"
            current = getattr(self, "current", None) or {}
            engaged_continuation = bool(
                _state_name(self) == "ENGAGED"
                and _norm_call(current.get("call")) == call
            )
            if engaged_continuation:
                # Mandatory revisit is deferred, not cancelled: never obstruct a
                # QSO that has genuinely reached ENGAGED, including terminal-73
                # retransmissions.
                LOG.debug(
                    "V10.7.6 MANDATORY REVISIT DEFER %s: existing ENGAGED QSO continues",
                    call,
                )
            elif _is_direct_data(data):
                _cancel_qsy(self, f"V10.7.6 direct incoming {call}")
                LOG.info(
                    "V10.7.6 DIRECT OVERRIDE %s: mandatory revisit cancelled before direct candidate",
                    call,
                )
            else:
                LOG.info(
                    "V10.7.6 MANDATORY REVISIT BLOCK %s: proactive/wanted candidate cannot delay pending revisit",
                    call,
                )
                return False
        if original_start_candidate is None:
            return False
        return original_start_candidate(self, data, reason)

    def mark_engaged(self, call, payload):
        result = original_mark_engaged(self, call, payload)
        token = str((payload or [""])[-1]).upper() if payload else ""
        current = getattr(self, "current", None) or {}
        if (
            _state_name(self) == "ENGAGED"
            and _norm_call(current.get("call")) == _norm_call(call)
            and token in {"RRR", "RR73", "73"}
        ):
            tail = _ensure_tail(self, call)
            _touch_watchdog(tail)
            # Legacy completion treats terminal + TXDisabled as finished before
            # the queued final 73 actually reaches RF. This overlay owns terminal
            # completion while the tail exists.
            self.current_terminal_seen = False
            self.engaged_at = time.monotonic()
        return result

    def process_decode(self, packet):
        current_before = getattr(self, "current", None) or {}
        call_before = _norm_call(current_before.get("call"))
        token, data = _terminal_from_packet(self, packet, call_before) if call_before else (None, None)

        result = original_process_decode(self, packet)

        if token and data:
            current_after = getattr(self, "current", None) or {}
            if _norm_call(current_after.get("call")) == call_before and _state_name(self) == "ENGAGED":
                tail = _ensure_tail(self, call_before)
                tail["last_terminal"] = token
                tail["last_terminal_data"] = data
                tail["terminal_rx_count"] = int(tail.get("terminal_rx_count", 0) or 0) + 1
                _touch_watchdog(tail)
                self.current_terminal_seen = False
                self.engaged_at = time.monotonic()

                if token == "73":
                    tail["remote_explicit_73"] = True
                    tail["retry_pending"] = False
                    tail["retry_pending_for_tx"] = 0
                    LOG.info(
                        "V10.7.6 TERMINAL RX %s: directed remote 73 confirms QSO completion",
                        call_before,
                    )
                else:
                    completed_tx = int(tail.get("final_tx_count", 0) or 0)
                    final_done = bool(
                        completed_tx >= 1
                        and float(tail.get("last_final_tx_end", 0.0) or 0.0) > 0.0
                        and not bool(tail.get("final_tx_active"))
                    )
                    already_consumed = int(tail.get("retry_consumed_for_tx", 0) or 0) >= completed_tx
                    already_pending = bool(
                        tail.get("retry_pending")
                        and int(tail.get("retry_pending_for_tx", 0) or 0) == completed_tx
                    )
                    if final_done and not already_consumed and not already_pending:
                        tail["retry_pending"] = True
                        tail["retry_pending_for_tx"] = completed_tx
                        tail["grace_until"] = 0.0
                        LOG.info(
                            "V10.7.6 TERMINAL REPEAT %s: new directed %s after final TX %d -> another 73 requested",
                            call_before, token, completed_tx,
                        )
                    elif final_done:
                        LOG.debug(
                            "V10.7.6 TERMINAL REPEAT %s: duplicate %s already consumed/pending for final TX %d",
                            call_before, token, completed_tx,
                        )
                    else:
                        LOG.debug(
                            "V10.7.6 TERMINAL REPEAT %s: directed %s before first final 73 completed; no extra retry yet",
                            call_before, token,
                        )
        return result

    def process_status(self, packet):
        tail_before = _tail(self)
        progress_before = int(getattr(self, "engaged_tx_since_progress", 0) or 0)
        was_final_active = bool(tail_before and tail_before.get("final_tx_active"))
        call = tail_before.get("call") if tail_before else ""
        final_now = bool(tail_before and _is_final_73_tx(self, packet, call))

        if final_now:
            # Final 73 retransmissions do not consume the ordinary ENGAGED
            # no-progress TX budget.
            self.engaged_tx_since_progress = 0
        result = original_process_status(self, packet)

        # Manual ownership has absolute precedence over a pending mandatory revisit.
        if mandatory_revisit_pending(self) and _manual_override_active(self):
            _cancel_qsy(self, "V10.7.6 manual override")
            LOG.info("V10.7.6 MANUAL OVERRIDE: mandatory revisit cancelled")

        tail = _tail(self)
        if tail is None:
            return result
        self.current_terminal_seen = False

        if final_now:
            self.engaged_tx_since_progress = progress_before
            if not was_final_active:
                tail["final_tx_active"] = True
                tail["final_tx_started_at"] = time.monotonic()
                tail["final_tx_count"] = int(tail.get("final_tx_count", 0) or 0) + 1
                tail["grace_until"] = 0.0
                _touch_watchdog(tail)
                LOG.info(
                    "V10.7.6 FINAL 73 TX %s: RF/WSJT-X TX %d observed; QSO remains ENGAGED",
                    tail.get("call"), tail.get("final_tx_count"),
                )
        elif was_final_active and not bool(getattr(packet, "Transmitting", False)):
            now = time.monotonic()
            tail["final_tx_active"] = False
            tail["last_final_tx_end"] = now
            duration = max(0.0, now - float(tail.get("final_tx_started_at", now) or now))
            tail["final_tx_started_at"] = 0.0
            tail["grace_until"] = now + TERMINAL_GRACE
            _touch_watchdog(tail)
            LOG.info(
                "V10.7.6 FINAL 73 WAIT %s: TX %d ended after %.1fs observed; now listening %.0fs for RRR/RR73/73",
                tail.get("call"), tail.get("final_tx_count"), duration, TERMINAL_GRACE,
            )
        return result

    def clear_current(self, reason, delete_candidate=False):
        tail = _tail(self)
        reason_text = str(reason or "")
        if tail is not None and (
            "QSO logged" in reason_text
            or "terminal message completed" in reason_text
        ):
            if "QSO logged" in reason_text:
                tail["logged"] = True
            tail["blocked_clear_count"] = int(tail.get("blocked_clear_count", 0) or 0) + 1
            self.current_terminal_seen = False
            LOG.info(
                "V10.7.6 TERMINAL HOLD %s: deferred premature clear (%s)",
                tail.get("call"), reason_text,
            )
            return None
        return original_clear(self, reason, delete_candidate)

    def check_timeouts(self):
        tail = _tail(self)
        if tail is not None:
            self.current_terminal_seen = False
        result = original_check_timeouts(self)
        tail = _tail(self)
        if tail is None:
            return result
        self.current_terminal_seen = False
        now = time.monotonic()

        if tail.get("remote_explicit_73") and not bool(getattr(self, "transmitting", False)):
            _finish_tail(self, original_clear, "directed remote 73")
            return result

        if tail.get("retry_pending"):
            _send_forced_final_retry(self, tail)
            return result

        if (
            int(tail.get("final_tx_count", 0) or 0) >= 1
            and float(tail.get("grace_until", 0.0) or 0.0) > 0.0
            and now >= float(tail["grace_until"])
            and not bool(getattr(self, "transmitting", False))
        ):
            _finish_tail(self, original_clear, "no repeated terminal during 22s post-TX grace")
            return result

        if now >= float(tail.get("watchdog_until", 0.0) or 0.0):
            LOG.warning(
                "V10.7.6 TERMINAL WATCHDOG %s: %.0fs without valid terminal progress",
                tail.get("call"), TAIL_WATCHDOG,
            )
            _finish_tail(self, original_clear, "sliding terminal watchdog")
        return result

    Sequencer.__init__ = init
    if original_start_candidate is not None:
        Sequencer.start_candidate = start_candidate
    Sequencer.mark_engaged = mark_engaged
    Sequencer.process_decode = process_decode
    Sequencer.process_status = process_status
    Sequencer.check_timeouts = check_timeouts
    Sequencer.clear_current = clear_current
    Sequencer._v1076_terminal_revisit_installed = True
    LOG.info("%s installed", MARKER)


# ------------------------------- self-test -------------------------------

def _self_test() -> None:
    class State:
        value = "ENGAGED"

    class Packet:
        def __init__(self, message="F4EGM YB7FII RR73", tx=False, txmsg=""):
            self.Message = message
            self.Time = 1
            self.SNR = -20
            self.DeltaTime = 0.1
            self.DeltaFrequency = 2308
            self.Mode = "~"
            self.Transmitting = tx
            self.TXEnabled = False
            self.TxMessage = txmsg
            self.DXCall = "YB7FII"

        def as_dict(self):
            return {
                "DeltaTime": self.DeltaTime,
                "DeltaFrequency": self.DeltaFrequency,
                "Mode": self.Mode,
                "Message": self.Message,
            }

    class Fake:
        def __init__(self):
            self.mycall = "F4EGM"
            self.current = {"call": "YB7FII", "source": "proactive", "proactive": True}
            self.state = State()
            self.current_terminal_seen = False
            self.engaged_at = time.monotonic()
            self.engaged_tx_since_progress = 0
            self.transmitting = False
            self.tx_enabled = False
            self.band = 20
            self.frequency = 14074000
            self.last_ip_from = ("127.0.0.1", 2237)
            self.decision_due_at = None
            self.decision_settle_time = 0.12
            self.calls = 0
            self.clears = []
            self.started = []
            self.cancel_reasons = []
            self.v60_txdf = type("E", (), {"enabled": False})()
            self.v60_qsy_intent = None
            self.v55_manual_override = False

        def parse_segment(self, segment):
            t = segment.split()
            if len(t) >= 3:
                return "REPLY", {"to": t[0], "call": t[1], "payload": t[2:]}
            return None, None

        def call_station(self, _ip, _data, reason=""):
            self.calls += 1
            return True

        def drop_proactive_target(self, _call, _reason):
            return None

        def mark_engaged(self, _call, payload):
            if payload and payload[-1] in {"RRR", "RR73", "73"}:
                self.current_terminal_seen = True

        def process_decode(self, packet):
            _kind, m = self.parse_segment(packet.Message)
            if m and m["to"] == self.mycall and m["call"] == self.current["call"]:
                self.mark_engaged(m["call"], m["payload"])

        def process_status(self, packet):
            self.transmitting = bool(packet.Transmitting)
            self.tx_enabled = bool(packet.TXEnabled)
            if self.transmitting:
                self.engaged_tx_since_progress += 1

        def check_timeouts(self):
            if self.current_terminal_seen and not self.transmitting and not self.tx_enabled:
                self.clear_current("terminal message completed; Tx disabled")

        def clear_current(self, reason, delete_candidate=False):
            self.clears.append((reason, delete_candidate))
            self.current = None
            self.state = type("Idle", (), {"value": "IDLE"})()

        def start_candidate(self, data, reason):
            self.started.append((data, reason))
            return True

        def v1076_cancel_qsy_intent(self, reason):
            if not qsy_cancel_allowed(self, reason):
                return False
            self.cancel_reasons.append(reason)
            self.v60_qsy_intent = None
            return True

    # QSY policy tests before method wrapping.
    q = Fake()
    q.v60_qsy_intent = {"from": 20, "to": 17, "reason": "mandatory revisit of 17m age=3689s limit=3600s"}
    assert mandatory_revisit_pending(q)
    assert qsy_cancel_allowed(q, "fresh priority target YB7FII is pending") is False
    assert qsy_cancel_allowed(q, "V10.7.4 eligible target committed YB7FII") is False
    assert qsy_cancel_allowed(q, "some unrelated safety cancellation") is True
    assert qsy_cancel_allowed(q, "V10.7.6 direct incoming JA1ABC") is True
    assert qsy_cancel_allowed(q, "V10.7.6 manual override") is True
    q.v55_manual_override = True
    assert _manual_override_active(q)

    install(Fake)
    f = Fake()

    # Exact received direction: MYCALL REMOTE TOKEN is valid.
    f.process_decode(Packet("F4EGM YB7FII RR73"))
    assert f.v1076_terminal_tail is not None
    assert f.v1076_terminal_tail["terminal_rx_count"] == 1
    assert f.current_terminal_seen is False
    f.check_timeouts()
    assert f.current is not None and not f.clears

    # Inverted received order must not be interpreted as a terminal from YB7FII.
    before = f.v1076_terminal_tail["terminal_rx_count"]
    f.process_decode(Packet("YB7FII F4EGM RR73"))
    assert f.v1076_terminal_tail["terminal_rx_count"] == before

    # First final 73 enters TX, then only TX end opens the 22 s receive window.
    f.process_status(Packet(tx=True, txmsg="YB7FII F4EGM 73"))
    assert f.v1076_terminal_tail["final_tx_count"] == 1
    assert f.v1076_terminal_tail["grace_until"] == 0.0
    f.process_status(Packet(tx=False, txmsg="YB7FII F4EGM 73"))
    assert f.v1076_terminal_tail["last_final_tx_end"] > 0.0
    assert f.v1076_terminal_tail["grace_until"] > time.monotonic()

    # New RR73 after TX1 requests exactly one retry; duplicate before TX2 does not.
    f.process_decode(Packet("F4EGM YB7FII RR73"))
    assert f.v1076_terminal_tail["retry_pending_for_tx"] == 1
    f.process_decode(Packet("F4EGM YB7FII RR73"))
    f.check_timeouts()
    assert f.calls == 1
    assert f.v1076_terminal_tail["retry_requests"] == 1
    f.process_decode(Packet("F4EGM YB7FII RR73"))
    f.check_timeouts()
    assert f.calls == 1

    # TX2 completes. A *new* RR73 now requests TX3: no arbitrary second-73 cap.
    f.process_status(Packet(tx=True, txmsg="YB7FII F4EGM 73"))
    f.process_status(Packet(tx=False, txmsg="YB7FII F4EGM 73"))
    assert f.v1076_terminal_tail["final_tx_count"] == 2
    f.process_decode(Packet("F4EGM YB7FII RRR"))
    f.check_timeouts()
    assert f.calls == 2
    assert f.v1076_terminal_tail["retry_requests"] == 2

    # TX3 completes. Another new RR73 requests TX4, proving retries are unbounded
    # by count and always RF-triggered by the remote station.
    f.process_status(Packet(tx=True, txmsg="YB7FII F4EGM 73"))
    f.process_status(Packet(tx=False, txmsg="YB7FII F4EGM 73"))
    assert f.v1076_terminal_tail["final_tx_count"] == 3
    f.process_decode(Packet("F4EGM YB7FII RR73"))
    f.check_timeouts()
    assert f.calls == 3
    assert f.v1076_terminal_tail["retry_requests"] == 3

    # Complete TX4, then directed remote 73 closes immediately.
    f.process_status(Packet(tx=True, txmsg="YB7FII F4EGM 73"))
    f.process_status(Packet(tx=False, txmsg="YB7FII F4EGM 73"))
    f.process_decode(Packet("F4EGM YB7FII 73"))
    f.check_timeouts()
    assert f.current is None
    assert len(f.clears) == 1
    assert "V10.7.6 final-73 tail complete" in f.clears[0][0]

    # Mandatory revisit: proactive cannot start/delay it; direct always cancels.
    g = Fake()
    g.state = type("Idle", (), {"value": "IDLE"})()
    g.current = None
    g.v60_qsy_intent = {"reason": "mandatory revisit of 17m age=3700s limit=3600s"}
    proactive = {"call": "YB7FII", "source": "proactive", "proactive": True}
    assert g.start_candidate(proactive, "test") is False
    assert g.v60_qsy_intent is not None
    assert not g.started

    # A genuinely ENGAGED QSO is allowed to continue while mandatory revisit
    # remains pending; the revisit is deferred, not cancelled.
    e = Fake()
    e.v60_qsy_intent = {"reason": "mandatory revisit of 17m age=3700s limit=3600s"}
    continuation = {"call": "YB7FII", "source": "terminal-73-retry", "proactive": False}
    assert e.start_candidate(continuation, "engaged continuation") is True
    assert e.v60_qsy_intent is not None
    assert e.started and e.started[-1][0]["call"] == "YB7FII"

    direct = {"call": "JA1ABC", "source": "direct", "proactive": False}
    assert g.start_candidate(direct, "test direct") is True
    assert g.v60_qsy_intent is None
    assert g.cancel_reasons[-1] == "V10.7.6 direct incoming JA1ABC"
    assert g.started and g.started[-1][0]["call"] == "JA1ABC"

    # Manual mode cancellation is explicit from process_status.
    h = Fake()
    h.state = type("Idle", (), {"value": "IDLE"})()
    h.current = None
    h.v60_qsy_intent = {"reason": "mandatory revisit of 12m age=3900s limit=3600s"}
    h.v55_manual_override = True
    h.process_status(Packet(tx=False))
    assert h.v60_qsy_intent is None
    assert h.cancel_reasons[-1] == "V10.7.6 manual override"

    print("V10.7.6 self-test: OK")


if __name__ == "__main__":
    _self_test()
