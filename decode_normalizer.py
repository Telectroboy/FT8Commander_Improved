#!/usr/bin/env python3
"""Normalize received FT8 text before the existing segment parser.

MSHV Multi-Answer can omit the sender from a terminal sub-message and expose
it, in angle brackets, in the following sub-message.  For example::

    F4EGM RR73; JA1MLV <CN8NS> -08

The first logical message is therefore ``F4EGM CN8NS RR73``.  Normalization is
deliberately conservative: an omitted sender is reconstructed only for this
precise terminal-plus-angle-bracket form.  All other semicolon-separated text
is returned unchanged for the existing parser to accept or reject.
"""

from __future__ import annotations

import re
from typing import List, Optional


_CALL_RE = re.compile(r"^[A-Z0-9]+(?:/[A-Z0-9]+)*$", re.IGNORECASE)
_TERMINAL_TOKENS = {"RRR", "RR73", "73"}


def _callsign(token: str) -> Optional[str]:
    """Return a normalized callsign-like token, or None when it is invalid."""
    value = str(token or "").strip().upper()
    if not value or not _CALL_RE.fullmatch(value):
        return None
    if not any(char.isalpha() for char in value):
        return None
    if not any(char.isdigit() for char in value):
        return None
    return value


def normalize_message_segments(message: object) -> List[str]:
    """Return parser-ready segments from one received FT8 message.

    Received directed messages keep WSJT-X ordering: destination first,
    sender second.  In the MSHV special form, the sender enclosed in ``<>`` in
    the following segment is copied into the preceding terminal segment.
    """
    segments = [part.strip() for part in str(message or "").split(";")]

    for index in range(len(segments) - 1):
        terminal_tokens = segments[index].upper().split()
        following_tokens = segments[index + 1].upper().split()
        if len(terminal_tokens) != 2 or len(following_tokens) < 3:
            continue
        if terminal_tokens[1] not in _TERMINAL_TOKENS:
            continue

        recipient = _callsign(terminal_tokens[0])
        bracketed_sender = following_tokens[1]
        if not (bracketed_sender.startswith("<") and
                bracketed_sender.endswith(">")):
            continue
        sender = _callsign(bracketed_sender[1:-1])
        if recipient and sender:
            segments[index] = f"{recipient} {sender} {terminal_tokens[1]}"

    return segments
