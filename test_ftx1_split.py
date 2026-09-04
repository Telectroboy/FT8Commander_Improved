#!/usr/bin/env python3
"""Obsolete FT/ ST split validator.

FT8Commander V5 TX-DF uses the field-validated FB + VS1/VS0 method with ST0.
This file deliberately performs no CAT write so the old ST1/FT1 probe cannot
be launched accidentally.
"""
raise SystemExit(
    "Obsolete validator: TX-DF now uses FB + VS1/VS0 with ST0. "
    "Use the V5 runtime and its CAT baseline checks instead."
)
