"""
disasm.py — ARM Thumb/Thumb-2 disassembly engine using Capstone.

This module wraps the Capstone disassembly library to provide ARM-specific
instruction decoding with metadata needed for cycle-exact WCET analysis.

FEATURES
--------
- Disassemble ARM Thumb and Thumb-2 instruction streams
- Extract register operands (for load-use hazard detection)
- Identify instruction types (branch, load, store, arithmetic)
- Map instruction addresses to source lines (via DWARF)

CAPSTONE PRIMER
---------------
Capstone is a lightweight multi-architecture disassembly framework.
For ARM, it supports:
  - Thumb mode (16-bit instructions)
  - Thumb-2 mode (mixed 16/32-bit instructions)
  - ARM mode (32-bit instructions, not used on Cortex-M)

Installation: pip install capstone
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

try:
    import capstone
    from capstone import CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_LITTLE_ENDIAN
    from capstone.arm import (
        ARM_OP_REG, ARM_OP_IMM, ARM_OP_MEM,
        ARM_REG_INVALID, ARM_REG_PC, ARM_REG_LR, ARM_REG_SP,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Capstone is required for binary mode disassembly.\n"
        "Install it with: pip install capstone"
    ) from exc

from irq_verify.thumb_table import (
    get_instruction_timing,
    ARMArch,
    InsnTiming,
    get_multi_register_cycles,
    is_load_instruction,
    is_branch_instruction,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Instruction:
    """
    Decoded ARM instruction with cycle timing and operand metadata.
    
    Attributes
    ----------
    address:
        Virtual memory address of this instruction.
    mnemonic:
        Instruction mnemonic (e.g., "ldr", "add", "b").
    op_str:
        Operand string (e.g., "r0, [r1, #4]").
    size:
        Instruction size in bytes (2 for 16-bit Thumb, 4 for 32-bit Thumb-2).
    bytes:
        Raw instruction bytes.
    timing:
        Cycle timing information from thumb_table.
    dest_reg:
        Destination register (if any), e.g., R0 for "LDR R0, [R1]".
    src_regs:
        List of source registers read by this instruction.
    is_load:
        True if this is a load instruction (LDR, LDM, POP).
    is_store:
        True if this is a store instruction (STR, STM, PUSH).
    is_branch:
        True if this is a branch/control-flow instruction.
    branch_target:
        Target address for direct branches (if computable), else None.
    """
    address: int
    mnemonic: str
    op_str: str
    size: int
    bytes: bytes
    timing: InsnTiming
    dest_reg: Optional[int] = None
    src_regs: list[int] = None  # type: ignore
    is_load: bool = False
    is_store: bool = False
    is_branch: bool = False
    branch_target: Optional[int] = None
    
    def __post_init__(self) -> None:
        if self.src_regs is None:
            self.src_regs = []
    
    def __str__(self) -> str:
        return f"0x{self.address:08x}: {self.mnemonic:8s} {self.op_str}"


# ---------------------------------------------------------------------------
# Disassembler
# ---------------------------------------------------------------------------

class ARMDisassembler:
    """
    ARM Thumb/Thumb-2 disassembler with cycle timing analysis.
    
    Parameters
    ----------
    arch:
        Target ARM architecture (for timing table selection).
    """
    
    def __init__(self, arch: ARMArch = ARMArch.CORTEX_M0):
        self.arch = arch
        
        # Create Capstone disassembler instance
        # CS_MODE_THUMB: Thumb + Thumb-2 mixed mode (standard for Cortex-M)
        self.cs = capstone.Cs(
            CS_ARCH_ARM,
            CS_MODE_THUMB | CS_MODE_LITTLE_ENDIAN
        )
        
        # Enable detailed instruction info
        self.cs.detail = True
        
        logger.debug(f"ARM disassembler initialized for {arch.value}")
    
    def disassemble(
        self,
        code: bytes,
        base_address: int = 0x0,
    ) -> list[Instruction]:
        """
        Disassemble a block of ARM Thumb code.
        
        Parameters
        ----------
        code:
            Raw instruction bytes.
        base_address:
            Virtual memory address of the first byte (for address labeling).
        
        Returns
        -------
        list of Instruction
            Decoded instructions with timing metadata.
        
        Raises
        ------
        ValueError
            If disassembly fails (invalid instruction stream).
        """
        instructions: list[Instruction] = []
        
        try:
            for cs_insn in self.cs.disasm(code, base_address):
                insn = self._decode_instruction(cs_insn)
                instructions.append(insn)
        except capstone.CsError as exc:
            raise ValueError(f"Disassembly failed: {exc}") from exc
        
        return instructions
    
    def disassemble_one(
        self,
        code: bytes,
        base_address: int = 0x0,
    ) -> Optional[Instruction]:
        """
        Disassemble a single instruction at the start of *code*.
        
        Returns None if disassembly fails.
        """
        try:
            insns = self.disassemble(code[:4], base_address)  # Max 4 bytes for Thumb-2
            return insns[0] if insns else None
        except ValueError:
            return None
    
    def _decode_instruction(self, cs_insn: any) -> Instruction:
        """
        Convert a Capstone instruction object into our Instruction dataclass.
        
        Parameters
        ----------
        cs_insn:
            Capstone CsInsn object (from cs.disasm()).
        
        Returns
        -------
        Instruction
            Decoded instruction with timing metadata.
        """
        mnemonic = cs_insn.mnemonic.upper()
        op_str = cs_insn.op_str
        address = cs_insn.address
        size = cs_insn.size
        raw_bytes = cs_insn.bytes
        
        # Look up cycle timing from thumb_table
        try:
            timing = get_instruction_timing(mnemonic, self.arch)
        except KeyError:
            # Unknown instruction — log warning and use conservative 1 cycle
            logger.warning(
                f"Unknown instruction '{mnemonic}' at 0x{address:08x}. "
                f"Using 1-cycle default."
            )
            timing = InsnTiming(1, False, False, False, "unknown instruction")
        
        # Extract operand metadata (register reads/writes)
        dest_reg = None
        src_regs = []
        
        if cs_insn.operands:
            # First operand is often the destination (for data-processing)
            first_op = cs_insn.operands[0]
            if first_op.type == ARM_OP_REG and mnemonic not in ("CMP", "TST", "CMN"):
                dest_reg = first_op.reg
            
            # Remaining operands are sources
            for op in cs_insn.operands[1:]:
                if op.type == ARM_OP_REG:
                    src_regs.append(op.reg)
                elif op.type == ARM_OP_MEM:
                    # Memory operand: [base + index + offset]
                    # Both base and index registers are read
                    if op.mem.base != ARM_REG_INVALID:
                        src_regs.append(op.mem.base)
                    if op.mem.index != ARM_REG_INVALID:
                        src_regs.append(op.mem.index)
        
        # Classify instruction type
        is_load = is_load_instruction(mnemonic)
        is_store = mnemonic in ("STR", "STRB", "STRH", "STM", "PUSH", "VSTR")
        is_branch_type = is_branch_instruction(mnemonic)
        
        # Compute branch target for direct branches
        branch_target = None
        if is_branch_type and cs_insn.operands:
            first_op = cs_insn.operands[0]
            if first_op.type == ARM_OP_IMM:
                branch_target = first_op.imm
        
        # Adjust timing for multi-register operations (LDM, STM, PUSH, POP)
        if mnemonic in ("LDM", "STM", "PUSH", "POP"):
            # Count number of registers in operand list
            num_regs = sum(1 for op in cs_insn.operands if op.type == ARM_OP_REG)
            adjusted_cycles = get_multi_register_cycles(timing, num_regs)
            # Create a new timing object with adjusted cycles
            timing = InsnTiming(
                adjusted_cycles,
                timing.has_load_delay,
                timing.is_branch,
                timing.is_multiply,
                f"{timing.note} ({num_regs} registers)"
            )
        
        return Instruction(
            address=address,
            mnemonic=mnemonic,
            op_str=op_str,
            size=size,
            bytes=raw_bytes,
            timing=timing,
            dest_reg=dest_reg,
            src_regs=src_regs,
            is_load=is_load,
            is_store=is_store,
            is_branch=is_branch_type,
            branch_target=branch_target,
        )
    
    def disassemble_function(
        self,
        code: bytes,
        base_address: int,
    ) -> list[Instruction]:
        """
        Disassemble a complete function body.
        
        This is a convenience wrapper around disassemble() that handles
        function-level concerns like detecting BX LR (return) instructions.
        
        Parameters
        ----------
        code:
            Function machine code bytes.
        base_address:
            Starting address of the function.
        
        Returns
        -------
        list of Instruction
            All instructions up to and including the first return (BX LR).
        """
        instructions = self.disassemble(code, base_address)
        
        # Find first BX LR (return) — truncate there if found
        for i, insn in enumerate(instructions):
            if insn.mnemonic == "BX" and "lr" in insn.op_str.lower():
                return instructions[:i+1]
        
        # No explicit return found — return all instructions
        return instructions


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def format_disassembly(
    instructions: list[Instruction],
    show_timing: bool = True,
) -> str:
    """
    Format a list of instructions as human-readable disassembly listing.
    
    Parameters
    ----------
    instructions:
        Decoded instructions.
    show_timing:
        If True, show cycle timing in a column.
    
    Returns
    -------
    str
        Formatted disassembly text.
    """
    lines = []
    for insn in instructions:
        addr_str = f"{insn.address:08x}"
        bytes_str = insn.bytes.hex().ljust(8)
        asm_str = f"{insn.mnemonic.lower():8s} {insn.op_str}"
        
        if show_timing:
            timing_str = f"[{insn.timing.base_cycles:2d} cy]"
            lines.append(f"{addr_str}  {bytes_str}  {asm_str:40s}  {timing_str}")
        else:
            lines.append(f"{addr_str}  {bytes_str}  {asm_str}")
    
    return "\n".join(lines)


def print_disassembly(
    instructions: list[Instruction],
    show_timing: bool = True,
) -> None:
    """Print disassembly to stdout."""
    print(format_disassembly(instructions, show_timing))


# ---------------------------------------------------------------------------
# Command-line interface (for testing)
# ---------------------------------------------------------------------------

def main() -> None:
    """Simple CLI to disassemble raw hex bytes (for development/testing)."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python -m irq_verify.disasm <hex_bytes>")
        print("Example: python -m irq_verify.disasm '0020 4ff0 0000'")
        sys.exit(1)
    
    logging.basicConfig(level=logging.DEBUG)
    
    # Parse hex string
    hex_str = sys.argv[1].replace(" ", "").replace("0x", "")
    code = bytes.fromhex(hex_str)
    
    # Disassemble
    disasm = ARMDisassembler(ARMArch.CORTEX_M0)
    instructions = disasm.disassemble(code, base_address=0x08000000)
    
    print(f"Disassembly of {len(code)} bytes:")
    print_disassembly(instructions, show_timing=True)


if __name__ == "__main__":
    main()
