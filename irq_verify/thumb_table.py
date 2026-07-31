"""
thumb_table.py — Cycle-exact ARM Thumb/Thumb-2 instruction timing tables.

This module provides instruction-level cycle timing data for ARM Cortex-M
microcontrollers based on official ARM Technical Reference Manuals (TRMs).

ACCURACY LEVEL
--------------
These tables provide **cycle-exact** timing at the instruction level for:
  - Base instruction execution time
  - Pipeline effects (load-use hazards, branch penalties)
  - Memory access patterns

NOT INCLUDED (board/configuration-specific):
  - Flash wait states (configurable via --flash-wait-states flag)
  - I-cache/D-cache behavior (architecture-specific, requires simulation)
  - AXI bus contention (system-specific)

REFERENCES
----------
- ARM DDI0432C: Cortex-M0 Technical Reference Manual
- ARM DDI0337I: Cortex-M3 Technical Reference Manual  
- ARM DDI0439D: Cortex-M4 Technical Reference Manual
- ARM DDI0553B: Cortex-M33 Technical Reference Manual
- ARM DDI0489D: Cortex-M7 Technical Reference Manual
- ARM Architecture Reference Manual ARMv6-M/ARMv7-M/ARMv8-M

INSTRUCTION NAMING
------------------
Uses Capstone ARM instruction mnemonics (CS_ARM_INS_*).
"""

from __future__ import annotations

from typing import Final, NamedTuple
from enum import Enum

# ---------------------------------------------------------------------------
# Architecture enumeration
# ---------------------------------------------------------------------------

class ARMArch(Enum):
    """Supported ARM Cortex-M architectures."""
    CORTEX_M0 = "cortex-m0"
    CORTEX_M0PLUS = "cortex-m0+"
    CORTEX_M3 = "cortex-m3"
    CORTEX_M4 = "cortex-m4"
    CORTEX_M33 = "cortex-m33"
    CORTEX_M7 = "cortex-m7"


# ---------------------------------------------------------------------------
# Timing data structure
# ---------------------------------------------------------------------------

class InsnTiming(NamedTuple):
    """Cycle timing for a single instruction."""
    base_cycles: int            # Base execution cycles (no stalls)
    has_load_delay: bool        # True if this is a load instruction (LDR, LDM, POP)
    is_branch: bool             # True if this is a control-flow instruction
    is_multiply: bool           # True if this is a multiply instruction
    note: str = ""              # Human-readable note about timing


# ---------------------------------------------------------------------------
# Cortex-M0 / M0+ instruction timing
# ---------------------------------------------------------------------------
# Reference: ARM DDI0432C §3.3 Table 3-1 "Instruction cycle summary"
#
# Pipeline: 3-stage (Fetch, Decode, Execute), in-order, no branch prediction
# Key timing rules:
#   - Most data processing: 1 cycle
#   - Branches taken: 3 cycles (pipeline flush)
#   - LDR/STR: 2 cycles
#   - MUL: 32 cycles (no hardware multiplier on M0)
#   - PUSH {n regs}: 1 + n cycles
#   - POP {n regs}: 1 + n cycles
#   - Load-use hazard: +1 stall if load result used in next instruction

_M0_TIMINGS: dict[str, InsnTiming] = {
    # Data processing (register)
    "MOV": InsnTiming(1, False, False, False, "register move"),
    "MOVS": InsnTiming(1, False, False, False, "move with status update"),
    "MVN": InsnTiming(1, False, False, False, "move NOT"),
    "ADD": InsnTiming(1, False, False, False, "add"),
    "ADDS": InsnTiming(1, False, False, False, "add with status"),
    "ADC": InsnTiming(1, False, False, False, "add with carry"),
    "SUB": InsnTiming(1, False, False, False, "subtract"),
    "SUBS": InsnTiming(1, False, False, False, "subtract with status"),
    "SBC": InsnTiming(1, False, False, False, "subtract with carry"),
    "RSB": InsnTiming(1, False, False, False, "reverse subtract"),
    "AND": InsnTiming(1, False, False, False, "bitwise AND"),
    "ORR": InsnTiming(1, False, False, False, "bitwise OR"),
    "EOR": InsnTiming(1, False, False, False, "bitwise XOR"),
    "BIC": InsnTiming(1, False, False, False, "bit clear"),
    "LSL": InsnTiming(1, False, False, False, "logical shift left"),
    "LSLS": InsnTiming(1, False, False, False, "logical shift left with status"),
    "LSR": InsnTiming(1, False, False, False, "logical shift right"),
    "LSRS": InsnTiming(1, False, False, False, "logical shift right with status"),
    "ASR": InsnTiming(1, False, False, False, "arithmetic shift right"),
    "ROR": InsnTiming(1, False, False, False, "rotate right"),
    "NEG": InsnTiming(1, False, False, False, "negate"),
    
    # Compare / test
    "CMP": InsnTiming(1, False, False, False, "compare"),
    "CMN": InsnTiming(1, False, False, False, "compare negative"),
    "TST": InsnTiming(1, False, False, False, "test bits"),
    
    # Memory access - single register
    "LDR": InsnTiming(2, True, False, False, "load word (2 cy: addr calc + load)"),
    "LDRB": InsnTiming(2, True, False, False, "load byte"),
    "LDRH": InsnTiming(2, True, False, False, "load halfword"),
    "LDRSB": InsnTiming(2, True, False, False, "load signed byte"),
    "LDRSH": InsnTiming(2, True, False, False, "load signed halfword"),
    "STR": InsnTiming(2, False, False, False, "store word"),
    "STRB": InsnTiming(2, False, False, False, "store byte"),
    "STRH": InsnTiming(2, False, False, False, "store halfword"),
    
    # Memory access - multiple registers
    "LDM": InsnTiming(1, True, False, False, "load multiple: 1 + N cycles (N computed at runtime)"),
    "STM": InsnTiming(1, False, False, False, "store multiple: 1 + N cycles"),
    "PUSH": InsnTiming(1, False, False, False, "push: 1 + N cycles"),
    "POP": InsnTiming(1, True, False, False, "pop: 1 + N cycles"),
    
    # Branches
    "B": InsnTiming(3, False, True, False, "unconditional branch (3 cy pipeline flush)"),
    "BEQ": InsnTiming(3, False, True, False, "branch if equal (taken: 3 cy)"),
    "BNE": InsnTiming(3, False, True, False, "branch if not equal"),
    "BCS": InsnTiming(3, False, True, False, "branch if carry set"),
    "BCC": InsnTiming(3, False, True, False, "branch if carry clear"),
    "BMI": InsnTiming(3, False, True, False, "branch if minus"),
    "BPL": InsnTiming(3, False, True, False, "branch if plus"),
    "BVS": InsnTiming(3, False, True, False, "branch if overflow"),
    "BVC": InsnTiming(3, False, True, False, "branch if no overflow"),
    "BHI": InsnTiming(3, False, True, False, "branch if higher"),
    "BLS": InsnTiming(3, False, True, False, "branch if lower or same"),
    "BGE": InsnTiming(3, False, True, False, "branch if greater or equal"),
    "BLT": InsnTiming(3, False, True, False, "branch if less than"),
    "BGT": InsnTiming(3, False, True, False, "branch if greater than"),
    "BLE": InsnTiming(3, False, True, False, "branch if less or equal"),
    "BL": InsnTiming(3, False, True, False, "branch with link (function call)"),
    "BLX": InsnTiming(3, False, True, False, "branch with link and exchange"),
    "BX": InsnTiming(3, False, True, False, "branch and exchange"),
    
    # Multiply (M0 has no hardware multiplier - uses iterative algorithm)
    "MUL": InsnTiming(32, False, False, True, "multiply (32 cy on M0 - no HW multiplier)"),
    "MULS": InsnTiming(32, False, False, True, "multiply with status"),
    
    # Miscellaneous
    "NOP": InsnTiming(1, False, False, False, "no operation"),
    "SEV": InsnTiming(1, False, False, False, "send event"),
    "WFE": InsnTiming(1, False, False, False, "wait for event"),
    "WFI": InsnTiming(1, False, False, False, "wait for interrupt"),
    "BKPT": InsnTiming(1, False, False, False, "breakpoint"),
    "SVC": InsnTiming(1, False, False, False, "supervisor call"),
    "DMB": InsnTiming(1, False, False, False, "data memory barrier"),
    "DSB": InsnTiming(1, False, False, False, "data synchronization barrier"),
    "ISB": InsnTiming(1, False, False, False, "instruction synchronization barrier"),
    "MRS": InsnTiming(1, False, False, False, "move to register from special"),
    "MSR": InsnTiming(1, False, False, False, "move to special from register"),
    "CPSIE": InsnTiming(1, False, False, False, "change processor state - enable interrupts"),
    "CPSID": InsnTiming(1, False, False, False, "change processor state - disable interrupts"),
}

# M0+ has identical timing to M0 for the instructions we model
_M0PLUS_TIMINGS = dict(_M0_TIMINGS)


# ---------------------------------------------------------------------------
# Cortex-M3 instruction timing
# ---------------------------------------------------------------------------
# Reference: ARM DDI0337I §3.3 "Instruction set summary"
#
# Pipeline: 3-stage, in-order, WITH branch prediction
# Key differences from M0:
#   - Hardware branch predictor (but we use worst-case = mispredicted)
#   - Faster multiply: MUL = 1 cycle (single-cycle 32x32→32)
#   - UMULL/SMULL = 3-5 cycles (32x32→64)

_M3_TIMINGS = dict(_M0_TIMINGS)  # Start with M0 baseline
_M3_TIMINGS.update({
    # Multiply - M3 has hardware multiplier
    "MUL": InsnTiming(1, False, False, True, "multiply (1 cy on M3 - hardware multiplier)"),
    "MULS": InsnTiming(1, False, False, True, "multiply with status"),
    "MLA": InsnTiming(2, False, False, True, "multiply-accumulate (2 cy)"),
    "MLS": InsnTiming(2, False, False, True, "multiply-subtract (2 cy)"),
    "UMULL": InsnTiming(5, False, False, True, "unsigned multiply long (32x32→64, 5 cy worst-case)"),
    "SMULL": InsnTiming(5, False, False, True, "signed multiply long"),
    "UMLAL": InsnTiming(5, False, False, True, "unsigned multiply-accumulate long"),
    "SMLAL": InsnTiming(5, False, False, True, "signed multiply-accumulate long"),
    
    # Divide (M3 has optional hardware divider)
    "UDIV": InsnTiming(12, False, False, False, "unsigned divide (2-12 cy, worst-case 12)"),
    "SDIV": InsnTiming(12, False, False, False, "signed divide (2-12 cy, worst-case 12)"),
})


# ---------------------------------------------------------------------------
# Cortex-M4 / M33 instruction timing
# ---------------------------------------------------------------------------
# Reference: ARM DDI0439D §3.3, ARM DDI0553B §3.3
#
# M4: ARMv7-M with DSP extensions and optional FPU
# M33: ARMv8-M with TrustZone, DSP, optional FPU
# Key additions:
#   - DSP instructions (SIMD): SADD8, QADD, etc. = 1 cycle
#   - Single-precision FPU (if present): VADD.F32 = 1 cycle, VMUL.F32 = 1 cycle
#   - Saturating arithmetic: 1 cycle

_M4_TIMINGS = dict(_M3_TIMINGS)  # M4 inherits M3 integer timing
_M4_TIMINGS.update({
    # DSP instructions (SIMD on packed data)
    "SADD8": InsnTiming(1, False, False, False, "signed add 8-bit SIMD"),
    "SADD16": InsnTiming(1, False, False, False, "signed add 16-bit SIMD"),
    "SSUB8": InsnTiming(1, False, False, False, "signed subtract 8-bit SIMD"),
    "SSUB16": InsnTiming(1, False, False, False, "signed subtract 16-bit SIMD"),
    "QADD": InsnTiming(1, False, False, False, "saturating add"),
    "QSUB": InsnTiming(1, False, False, False, "saturating subtract"),
    "QDADD": InsnTiming(1, False, False, False, "saturating double and add"),
    "QDSUB": InsnTiming(1, False, False, False, "saturating double and subtract"),
    "SMLAD": InsnTiming(1, False, False, True, "signed multiply-accumulate dual"),
    "SMUAD": InsnTiming(1, False, False, True, "signed multiply dual"),
    
    # FPU instructions (single-precision, if FPU present)
    # NOTE: These assume FPU is enabled and no denormals.
    # Denormal inputs/outputs can add 10+ cycles of microcode.
    "VADD.F32": InsnTiming(1, False, False, False, "FP add (1 cy for normal numbers)"),
    "VSUB.F32": InsnTiming(1, False, False, False, "FP subtract"),
    "VMUL.F32": InsnTiming(1, False, False, False, "FP multiply"),
    "VDIV.F32": InsnTiming(14, False, False, False, "FP divide (14 cy)"),
    "VSQRT.F32": InsnTiming(14, False, False, False, "FP square root (14 cy)"),
    "VLDR": InsnTiming(2, True, False, False, "FP load"),
    "VSTR": InsnTiming(2, False, False, False, "FP store"),
})

# M33 timing is essentially identical to M4 for the instructions we model
_M33_TIMINGS = dict(_M4_TIMINGS)


# ---------------------------------------------------------------------------
# Cortex-M7 instruction timing
# ---------------------------------------------------------------------------
# Reference: ARM DDI0489D §3.3 "Instruction timing"
#
# Pipeline: 6-stage, dual-issue superscalar, out-of-order execution
# Key features:
#   - Dual-issue: two independent instructions can execute in 1 cycle
#   - Branch prediction: well-predicted branches = 1 cycle penalty
#   - Mispredicted branch: 7-13 cycles (we use 12 as worst-case)
#   - TCM memory: 1 cycle latency
#   - AXI memory (flash/SRAM): 3+ cycles (depends on wait states)
#   - I-cache: 64-byte line, 4-way associative
#
# CONSERVATIVE UPPER BOUNDS for WCET:
#   - ALU ops: 1 cycle (best case = 0.5 cy if dual-issued, worst = 1)
#   - Load/Store (TCM): 1 cycle, (AXI): 3 cycles (we use 3)
#   - Branches: 12 cycles (misprediction worst-case)

_M7_TIMINGS = dict(_M4_TIMINGS)  # Start with M4 baseline
_M7_TIMINGS.update({
    # Memory access - M7 can be faster (TCM) or slower (AXI)
    # We use conservative 3-cycle AXI timing for WCET
    "LDR": InsnTiming(3, True, False, False, "load word (3 cy AXI worst-case)"),
    "LDRB": InsnTiming(3, True, False, False, "load byte"),
    "LDRH": InsnTiming(3, True, False, False, "load halfword"),
    "LDRSB": InsnTiming(3, True, False, False, "load signed byte"),
    "LDRSH": InsnTiming(3, True, False, False, "load signed halfword"),
    "STR": InsnTiming(3, False, False, False, "store word (3 cy AXI)"),
    "STRB": InsnTiming(3, False, False, False, "store byte"),
    "STRH": InsnTiming(3, False, False, False, "store halfword"),
    "VLDR": InsnTiming(3, True, False, False, "FP load (AXI)"),
    "VSTR": InsnTiming(3, False, False, False, "FP store (AXI)"),
    
    # Branches - M7 worst-case misprediction = 12 cycles (6-stage pipeline flush)
    "B": InsnTiming(12, False, True, False, "branch (12 cy mispredicted)"),
    "BEQ": InsnTiming(12, False, True, False, "branch if equal (worst-case misprediction)"),
    "BNE": InsnTiming(12, False, True, False, "branch if not equal"),
    "BCS": InsnTiming(12, False, True, False, "branch if carry set"),
    "BCC": InsnTiming(12, False, True, False, "branch if carry clear"),
    "BMI": InsnTiming(12, False, True, False, "branch if minus"),
    "BPL": InsnTiming(12, False, True, False, "branch if plus"),
    "BVS": InsnTiming(12, False, True, False, "branch if overflow"),
    "BVC": InsnTiming(12, False, True, False, "branch if no overflow"),
    "BHI": InsnTiming(12, False, True, False, "branch if higher"),
    "BLS": InsnTiming(12, False, True, False, "branch if lower or same"),
    "BGE": InsnTiming(12, False, True, False, "branch if >= (signed)"),
    "BLT": InsnTiming(12, False, True, False, "branch if < (signed)"),
    "BGT": InsnTiming(12, False, True, False, "branch if > (signed)"),
    "BLE": InsnTiming(12, False, True, False, "branch if <= (signed)"),
    "BL": InsnTiming(12, False, True, False, "branch with link"),
    "BLX": InsnTiming(12, False, True, False, "branch with link and exchange"),
    "BX": InsnTiming(12, False, True, False, "branch and exchange"),
})


# ---------------------------------------------------------------------------
# Architecture table registry
# ---------------------------------------------------------------------------

_ARCH_TABLES: Final[dict[ARMArch, dict[str, InsnTiming]]] = {
    ARMArch.CORTEX_M0: _M0_TIMINGS,
    ARMArch.CORTEX_M0PLUS: _M0PLUS_TIMINGS,
    ARMArch.CORTEX_M3: _M3_TIMINGS,
    ARMArch.CORTEX_M4: _M4_TIMINGS,
    ARMArch.CORTEX_M33: _M33_TIMINGS,
    ARMArch.CORTEX_M7: _M7_TIMINGS,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_instruction_timing(
    mnemonic: str,
    arch: ARMArch = ARMArch.CORTEX_M0,
) -> InsnTiming:
    """
    Return cycle timing for an ARM instruction mnemonic.
    
    Parameters
    ----------
    mnemonic:
        ARM instruction mnemonic (e.g., "LDR", "ADD", "B").
        Case-insensitive.
    arch:
        Target ARM Cortex-M architecture.
    
    Returns
    -------
    InsnTiming
        Timing information for the instruction.
    
    Raises
    ------
    KeyError
        If the instruction mnemonic is not recognized for this architecture.
    """
    table = _ARCH_TABLES[arch]
    key = mnemonic.upper().strip()
    
    if key not in table:
        # Try without condition suffix (e.g., "ADDEQ" → "ADD")
        base = key.rstrip("EQNECSCCMIPLVSVCHILSGEGEBGTLTLE")
        if base in table:
            return table[base]
        raise KeyError(
            f"Unknown instruction '{mnemonic}' for {arch.value}. "
            f"This may be a rare instruction not yet in the timing table."
        )
    
    return table[key]


def get_multi_register_cycles(
    base_timing: InsnTiming,
    num_registers: int,
) -> int:
    """
    Compute total cycles for multi-register load/store (LDM, STM, PUSH, POP).
    
    Formula: 1 + N cycles, where N is the number of registers.
    
    Parameters
    ----------
    base_timing:
        The InsnTiming for LDM/STM/PUSH/POP (base_cycles should be 1).
    num_registers:
        Number of registers transferred.
    
    Returns
    -------
    int
        Total cycle count for the operation.
    """
    return base_timing.base_cycles + num_registers


def arch_from_string(arch_str: str) -> ARMArch:
    """
    Parse an architecture string (e.g., "cortex-m4") into an ARMArch enum.
    
    Raises
    ------
    ValueError
        If the architecture string is not recognized.
    """
    key = arch_str.lower().strip()
    for arch in ARMArch:
        if arch.value == key:
            return arch
    
    valid = ", ".join(a.value for a in ARMArch)
    raise ValueError(
        f"Unknown architecture '{arch_str}'. "
        f"Supported: {valid}"
    )


def get_supported_architectures() -> list[str]:
    """Return list of supported architecture names."""
    return [arch.value for arch in ARMArch]


# ---------------------------------------------------------------------------
# Load-use hazard detection (used by pipeline.py)
# ---------------------------------------------------------------------------

def is_load_instruction(mnemonic: str) -> bool:
    """
    Return True if *mnemonic* is a load instruction that introduces
    a load-use hazard if the loaded value is used in the next instruction.
    """
    key = mnemonic.upper().strip()
    return key in ("LDR", "LDRB", "LDRH", "LDRSB", "LDRSH", "LDM", "POP", "VLDR")


def is_branch_instruction(mnemonic: str) -> bool:
    """Return True if *mnemonic* is a branch/control-flow instruction."""
    key = mnemonic.upper().strip()
    # Check both exact match and base form (B, BEQ, BNE, etc.)
    if key in ("B", "BL", "BLX", "BX"):
        return True
    # Conditional branches
    base = key.rstrip("EQNECSCCMIPLVSVCHILSGEGEBGTLTLE")
    return base == "B"
