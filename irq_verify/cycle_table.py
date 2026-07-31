"""
cycle_table.py — Worst-case cycle-cost table for ARM Cortex-M0.

IMPORTANT LIMITATION
--------------------
These costs are **C-source-level approximations**, not cycle-exact assembly
counts.  The tool works on the C AST, not compiled machine code, so it cannot
account for:

  * Exact instruction selection by the compiler
  * Pipeline stalls or hazards (the Cortex-M0 is 3-stage in-order, so hazards
    are minimal but not zero)
  * Flash wait states / instruction fetch latency (highly board-specific)
  * Branch prediction (the Cortex-M0 has none — it always pays the flush cost)
  * Alignment penalties on unaligned memory accesses

The figures below are deliberately *conservative* (upper-bound) estimates
derived from the ARM Cortex-M0 Technical Reference Manual (DDI0432C) and the
ARMv6-M Architecture Reference Manual instruction timing tables.

WHY THIS IS STILL USEFUL
-------------------------
Even with these limitations, the tool reliably catches obvious budget
violations: e.g. a spin-lock loop inside a critical section, or hundreds of
sequential register writes.  False positives (flagging compliant code) are
possible but false negatives (missing a real violation) are minimised by the
conservative upper-bound approach.

SWAPPABLE TABLE
---------------
A custom table can be supplied via the ``--cycle-table`` CLI flag (JSON file).
The JSON must be a flat object whose keys match the string constants defined in
this module (see ``COST_KEYS``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Key constants — use these everywhere rather than raw strings.
# ---------------------------------------------------------------------------

ASSIGN: Final = "assign"          # simple assignment  (a = b)
ARITH: Final = "arith"            # arithmetic / bitwise operation
COMPARE: Final = "compare"        # comparison  (<, >, ==, !=, etc.)
CALL_OVERHEAD: Final = "call_overhead"  # function call + return (prologue/epilogue)
MEM_READ: Final = "mem_read"      # memory / MMIO register read  (*ptr or array[i])
MEM_WRITE: Final = "mem_write"    # memory / MMIO register write
BRANCH: Final = "branch"          # branch instruction (if/else decision)
LOOP_ITER: Final = "loop_iter"    # per-iteration overhead (increment + compare + branch)
UNARY: Final = "unary"            # unary op (!, ~, ++)

COST_KEYS: Final[tuple[str, ...]] = (
    ASSIGN,
    ARITH,
    COMPARE,
    CALL_OVERHEAD,
    MEM_READ,
    MEM_WRITE,
    BRANCH,
    LOOP_ITER,
    UNARY,
)

# ---------------------------------------------------------------------------
# Built-in Cortex-M0 table (conservative upper-bound estimates).
# ---------------------------------------------------------------------------
#
# Reference: ARM DDI0432C §3.3 "Instruction set summary", Table 3-1.
#
#   MOV / MOVS (register-to-register)  : 1 cycle
#   ADD / SUB / AND / ORR etc.          : 1 cycle
#   CMP / CMN / TST                     : 1 cycle
#   B (unconditional, taken)            : 3 cycles  (pipeline flush)
#   B<cond> (taken)                     : 3 cycles
#   BL / BLX                            : 3 cycles + PUSH/POP overhead
#   LDR / STR (register offset)         : 2 cycles
#   PUSH / POP (N registers)            : 1 + N cycles
#
# Approximations at C level:
#
#   assign       → MOV                 → 1 cycle  (we use 2 to cover LDR/STR variant)
#   arith        → ADD/SUB/AND/ORR     → 1 cycle
#   compare      → CMP                 → 1 cycle
#   call_overhead→ see detailed derivation below
#   mem_read     → LDR                 → 2 cycles
#   mem_write    → STR                 → 2 cycles
#   branch       → B<cond>             → 3 cycles
#   loop_iter    → ADD + CMP + B<cond> → 1+1+3 = 5 cycles
#   unary        → single ALU op       → 1 cycle
#
# CALL_OVERHEAD — worst-case derivation (genuine upper bound):
#
#   This tool works at the C AST level and never sees the compiler's register
#   allocation decisions.  The number of callee-saved registers pushed in a
#   function prologue (r4–r11 on ARMv6-M) is only determined after compilation
#   and depends on register pressure inside that function.  Using a typical-case
#   figure (e.g. PUSH {r4–r7}) would risk under-counting the overhead for
#   register-heavy callees and producing a false negative — the one failure mode
#   this tool must avoid.
#
#   Therefore CALL_OVERHEAD is set to the absolute worst case for Cortex-M0:
#   a callee that saves ALL eight callee-saved registers (r4–r11) on entry and
#   restores them (plus PC) on exit.
#
#     BL                              :  3 cycles  (pipeline flush)
#     PUSH {r4, r5, r6, r7, r8, r9,
#           r10, r11}   (8 registers) :  1 + 8 = 9 cycles
#     POP  {r4, r5, r6, r7, r8, r9,
#           r10, r11, pc} (9 items)   :  1 + 9 = 10 cycles
#                                       ─────────────────
#     Total                           :  22 cycles
#
#   This deliberately over-counts leaf functions (which may push nothing) and
#   typical inner functions (which commonly push 2–4 registers).  That produces
#   conservative false-positive budget failures for very simple callees, but
#   guarantees no false negatives for register-heavy ones.  Users who know their
#   compiler's actual output for a specific callee can reduce this value via
#   --cycle-table if needed.

_BUILTIN_CORTEX_M0: dict[str, int] = {
    ASSIGN: 2,
    ARITH: 1,
    COMPARE: 1,
    CALL_OVERHEAD: 22,
    MEM_READ: 2,
    MEM_WRITE: 2,
    BRANCH: 3,
    LOOP_ITER: 5,
    UNARY: 1,
}

# ---------------------------------------------------------------------------
# Cortex-M3 table (ARMv7-M, 3-stage in-order pipeline)
# ---------------------------------------------------------------------------
#
# Reference: ARM DDI0337H Cortex-M3 TRM, §3.3 "Instruction set summary".
#
#   Most ALU ops (ADD, SUB, AND, etc.)   : 1 cycle
#   LDR / STR (register)                 : 2 cycles
#   B<cond> taken                        : 1-3 cycles (branch predictor may help,
#                                          conservative = 3)
#   BL                                   : 3 + PUSH/POP overhead
#   MUL (32×32)                          : 3–5 cycles (we use 5, worst-case)
#
# The M3 pipeline has a 3-stage pipeline very similar to M0 but with a
# hardware branch predictor and a faster multiply unit.  Conservative upper
# bounds are essentially the same as M0 for the constructs we model.

_BUILTIN_CORTEX_M3: dict[str, int] = {
    ASSIGN: 2,
    ARITH: 1,
    COMPARE: 1,
    CALL_OVERHEAD: 22,   # same worst-case PUSH/POP as M0
    MEM_READ: 2,
    MEM_WRITE: 2,
    BRANCH: 3,           # conservative (branch prediction helps, but not guaranteed)
    LOOP_ITER: 5,        # ADD + CMP + B = 1+1+3
    UNARY: 1,
}

# ---------------------------------------------------------------------------
# Cortex-M4 / M33 table (ARMv7-M / ARMv8-M, 3-stage with FPU/DSP)
# ---------------------------------------------------------------------------
#
# Reference: ARM DDI0439D Cortex-M4 TRM, §3.3; ARM DDI0553B Cortex-M33 TRM.
#
#   Integer ALU ops                      : 1 cycle  (same as M3)
#   LDR / STR                            : 2 cycles (same)
#   SMUL (32×32 → 32) / MUL             : 1 cycle  (hardware DSP, much faster than M0)
#   B<cond> taken                        : 1-3 cycles (conservative = 3)
#   BL                                   : 3 + PUSH/POP
#
# For C-level analysis the integer ops are identical to M3.  The FPU speed-up
# is not modelled because we do not track float vs integer separately yet.

_BUILTIN_CORTEX_M4: dict[str, int] = {
    ASSIGN: 2,
    ARITH: 1,
    COMPARE: 1,
    CALL_OVERHEAD: 22,
    MEM_READ: 2,
    MEM_WRITE: 2,
    BRANCH: 3,
    LOOP_ITER: 5,
    UNARY: 1,
}

# M33 has the same pipeline structure as M4 for the instructions we model.
_BUILTIN_CORTEX_M33: dict[str, int] = dict(_BUILTIN_CORTEX_M4)

# ---------------------------------------------------------------------------
# Cortex-M7 table (ARMv7-M, 6-stage dual-issue superscalar pipeline)
# ---------------------------------------------------------------------------
#
# Reference: ARM DDI0489D Cortex-M7 TRM, §3.3.
#
# The M7 is a superscalar out-of-order capable core with a 6-stage pipeline,
# instruction and data TCMs, 512-bit AXI bus, and a branch predictor.
# In the best case a pair of independent ALU instructions execute in 1 cycle.
# In the worst case (e.g. load-use hazard, branch misprediction) latency is
# higher than M0.
#
# For WCET purposes (conservative upper bound):
#   ALU op pair (best)                   : 1 cycle total → 0.5 cy/op → we use 1
#   LDR / STR (TCM)                      : 1 cycle (on TCM, or 2+ on AXI)
#   LDR / STR (AXI / flash)             : 3+ cycles (cache miss worst-case)
#   B<cond> mispredicted                 : up to 12 cycles pipeline flush
#   BL worst case                        : 12 (branch) + 22 (PUSH/POP) = ~34
#
# We use upper bounds to be conservative:

_BUILTIN_CORTEX_M7: dict[str, int] = {
    ASSIGN: 3,           # covers both TCM (1 cy) and AXI worst-case (3 cy)
    ARITH: 1,
    COMPARE: 1,
    CALL_OVERHEAD: 34,   # BL pipeline flush (12) + worst-case PUSH/POP (22)
    MEM_READ: 3,         # AXI bus without cache hit; TCM = 1 cy (best case)
    MEM_WRITE: 3,
    BRANCH: 12,          # worst-case branch misprediction flush (6-stage pipeline)
    LOOP_ITER: 14,       # ADD(1) + CMP(1) + B<cond> mispredicted(12)
    UNARY: 1,
}

# ---------------------------------------------------------------------------
# Registry of all built-in architectures
# ---------------------------------------------------------------------------

_BUILTIN_TABLES: dict[str, dict[str, int]] = {
    "cortex-m0":  _BUILTIN_CORTEX_M0,
    "cortex-m0+": _BUILTIN_CORTEX_M0,   # M0+ has the same pipeline for our purposes
    "cortex-m3":  _BUILTIN_CORTEX_M3,
    "cortex-m4":  _BUILTIN_CORTEX_M4,
    "cortex-m33": _BUILTIN_CORTEX_M33,
    "cortex-m7":  _BUILTIN_CORTEX_M7,
}

SUPPORTED_ARCHS: Final[tuple[str, ...]] = tuple(sorted(_BUILTIN_TABLES.keys()))
DEFAULT_ARCH: Final[str] = "cortex-m0"


def load_cycle_table(json_path: Path | None) -> dict[str, int]:
    """Return a cycle-cost table, optionally overriding defaults from *json_path*.

    If *json_path* is ``None`` the built-in Cortex-M0 table is returned.

    If a JSON file is supplied, its keys override the built-in defaults so that
    a partial override (specifying only the keys you care about) is valid.

    Raises
    ------
    ValueError
        If the JSON file contains unknown keys or non-integer values.
    """
    table = dict(_BUILTIN_CORTEX_M0)  # always start from the built-in defaults

    if json_path is None:
        return table

    raw = json.loads(json_path.read_text(encoding="utf-8"))

    if not isinstance(raw, dict):
        raise ValueError(f"Cycle table JSON must be a JSON object, got {type(raw).__name__}")

    for key, value in raw.items():
        if key not in COST_KEYS:
            raise ValueError(
                f"Unknown cost key '{key}' in {json_path}. "
                f"Valid keys: {', '.join(COST_KEYS)}"
            )
        if not isinstance(value, int) or value < 0:
            raise ValueError(
                f"Cost for '{key}' must be a non-negative integer, got {value!r}"
            )
        table[key] = value

    return table


def load_cycle_table_for_arch(
    arch: str,
    json_override: Path | None = None,
) -> dict[str, int]:
    """Return a cycle-cost table for *arch*, optionally overriding keys from *json_override*.

    Parameters
    ----------
    arch:
        One of the architecture strings in :data:`SUPPORTED_ARCHS`
        (e.g. ``"cortex-m4"``).  Case-insensitive.
    json_override:
        Optional path to a JSON file whose keys override the built-in table.

    Raises
    ------
    ValueError
        If *arch* is not in :data:`SUPPORTED_ARCHS`.
    """
    key = arch.lower().strip()
    if key not in _BUILTIN_TABLES:
        valid = ", ".join(SUPPORTED_ARCHS)
        raise ValueError(
            f"Unknown architecture '{arch}'. "
            f"Supported: {valid}"
        )

    table = dict(_BUILTIN_TABLES[key])  # copy

    if json_override is not None:
        raw = json.loads(json_override.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(
                f"Cycle table JSON must be a JSON object, got {type(raw).__name__}"
            )
        for k, v in raw.items():
            if k not in COST_KEYS:
                raise ValueError(
                    f"Unknown cost key '{k}' in {json_override}. "
                    f"Valid keys: {', '.join(COST_KEYS)}"
                )
            if not isinstance(v, int) or v < 0:
                raise ValueError(
                    f"Cost for '{k}' must be a non-negative integer, got {v!r}"
                )
            table[k] = v

    return table


def get_cost(table: dict[str, int], key: str) -> int:
    """Return the cycle cost for *key*, falling back to the built-in table."""
    if key in table:
        return table[key]
    if key in _BUILTIN_CORTEX_M0:
        return _BUILTIN_CORTEX_M0[key]
    raise KeyError(f"No cycle cost defined for operation type '{key}'")

