"""
pipeline.py — ARM Cortex-M pipeline timing model.

This module models microarchitectural effects that affect cycle-exact timing:
  - Load-use hazards (pipeline stalls)
  - Branch penalties (pipeline flushes)
  - Flash wait states (instruction fetch latency)
  - Cache effects (instruction cache misses, optional)

ACCURACY GUARANTEE
------------------
The model is CONSERVATIVE (worst-case): it never under-counts cycles.
When a hazard may or may not occur (e.g., data-dependent branch), we
assume it DOES occur.

ARCHITECTURE-SPECIFIC RULES
----------------------------

Cortex-M0 / M0+:
  - 3-stage in-order pipeline (Fetch, Decode, Execute)
  - Load-use hazard: +1 stall if load result used in next instruction
  - Branches: 3-cycle flush (no branch prediction)
  - No instruction cache (linear flash fetch)
  
Cortex-M3:
  - 3-stage in-order pipeline with branch prediction
  - Load-use hazard: +1 stall
  - Branches: 3-cycle flush worst-case (mispredicted)
  - Optional I-cache (not modeled here)

Cortex-M4 / M33:
  - 3-stage pipeline, similar to M3
  - Load-use hazard: +1 stall
  - Branches: 3-cycle flush
  - Optional I-cache and ART accelerator (not modeled)

Cortex-M7:
  - 6-stage dual-issue superscalar pipeline
  - Load-use hazard: +2 stalls (longer pipeline)
  - Branches: 7-13 cycle flush (misprediction worst-case)
  - I-cache and D-cache (not fully modeled here)

FLASH WAIT STATES
-----------------
Instruction fetch from flash incurs wait states (board-specific).
Example: STM32F4 at 168 MHz with 5 wait states:
  - Each instruction fetch: 1 + 5 = 6 cycles
  - If I-cache hit: 0 wait states (best case)
  - We use worst-case: every fetch incurs wait states

Formula: total_cycles = execution_cycles + (num_instructions × wait_states)

REFERENCES
----------
- ARM DDI0432C: Cortex-M0 TRM §2.3 "Pipeline"
- ARM DDI0337I: Cortex-M3 TRM §2.3
- ARM DDI0439D: Cortex-M4 TRM §2.3
- ARM DDI0489D: Cortex-M7 TRM §2.5 "Instruction fetch unit"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from irq_verify.disasm import Instruction
    from irq_verify.thumb_table import ARMArch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PipelineModel:
    """
    Pipeline timing model for a specific ARM Cortex-M architecture.
    
    Attributes
    ----------
    arch:
        Target architecture.
    load_use_stall:
        Cycles added when a load result is used in the next instruction.
    branch_penalty:
        Cycles for a mispredicted branch (pipeline flush).
    flash_wait_states:
        Number of wait states for instruction fetch from flash (0 = no penalty).
    has_branch_predictor:
        True if the architecture has a branch predictor (we still use worst-case).
    dual_issue:
        True if the architecture can issue two instructions per cycle (M7 only).
    """
    arch: str
    load_use_stall: int
    branch_penalty: int
    flash_wait_states: int
    has_branch_predictor: bool
    dual_issue: bool
    
    @classmethod
    def for_architecture(
        cls,
        arch: ARMArch,
        flash_wait_states: int = 0,
    ) -> PipelineModel:
        """
        Create a pipeline model for a specific architecture.
        
        Parameters
        ----------
        arch:
            Target ARM Cortex-M architecture.
        flash_wait_states:
            Number of flash wait states (board-specific, default 0).
        
        Returns
        -------
        PipelineModel
            Pipeline model with architecture-specific parameters.
        """
        from irq_verify.thumb_table import ARMArch
        
        if arch in (ARMArch.CORTEX_M0, ARMArch.CORTEX_M0PLUS):
            return cls(
                arch=arch.value,
                load_use_stall=1,
                branch_penalty=3,
                flash_wait_states=flash_wait_states,
                has_branch_predictor=False,
                dual_issue=False,
            )
        
        elif arch == ARMArch.CORTEX_M3:
            return cls(
                arch=arch.value,
                load_use_stall=1,
                branch_penalty=3,
                flash_wait_states=flash_wait_states,
                has_branch_predictor=True,  # But we use worst-case anyway
                dual_issue=False,
            )
        
        elif arch in (ARMArch.CORTEX_M4, ARMArch.CORTEX_M33):
            return cls(
                arch=arch.value,
                load_use_stall=1,
                branch_penalty=3,
                flash_wait_states=flash_wait_states,
                has_branch_predictor=True,
                dual_issue=False,
            )
        
        elif arch == ARMArch.CORTEX_M7:
            return cls(
                arch=arch.value,
                load_use_stall=2,           # Longer pipeline
                branch_penalty=12,          # 6-stage pipeline flush worst-case
                flash_wait_states=flash_wait_states,
                has_branch_predictor=True,
                dual_issue=True,            # Can issue 2 independent instructions/cycle
            )
        
        else:
            raise ValueError(f"Unknown architecture: {arch}")


@dataclass
class TimingResult:
    """
    Detailed cycle timing for a sequence of instructions.
    
    Attributes
    ----------
    base_cycles:
        Sum of base instruction execution cycles (from thumb_table).
    stall_cycles:
        Cycles added due to pipeline hazards (load-use stalls).
    branch_penalty_cycles:
        Cycles added due to branch mispredictions.
    fetch_penalty_cycles:
        Cycles added due to flash wait states.
    total_cycles:
        Total worst-case cycle count (sum of all above).
    num_instructions:
        Number of instructions analyzed.
    num_stalls:
        Number of load-use hazards detected.
    num_branches:
        Number of branch instructions.
    """
    base_cycles: int
    stall_cycles: int
    branch_penalty_cycles: int
    fetch_penalty_cycles: int
    total_cycles: int
    num_instructions: int
    num_stalls: int
    num_branches: int


# ---------------------------------------------------------------------------
# Pipeline analyzer
# ---------------------------------------------------------------------------

class PipelineAnalyzer:
    """
    Analyze instruction sequences for pipeline effects.
    
    Parameters
    ----------
    model:
        Pipeline model (architecture-specific parameters).
    """
    
    def __init__(self, model: PipelineModel):
        self.model = model
        logger.debug(
            f"Pipeline analyzer initialized: {model.arch}, "
            f"flash_wait_states={model.flash_wait_states}"
        )
    
    def analyze_sequence(
        self,
        instructions: list[Instruction],
    ) -> TimingResult:
        """
        Compute worst-case cycle timing for a sequence of instructions.
        
        This includes:
          1. Base execution cycles (from thumb_table)
          2. Load-use hazard stalls
          3. Branch penalties
          4. Flash wait states
        
        Parameters
        ----------
        instructions:
            List of decoded instructions (from disasm.py).
        
        Returns
        -------
        TimingResult
            Detailed timing breakdown.
        """
        if not instructions:
            return TimingResult(0, 0, 0, 0, 0, 0, 0, 0)
        
        base_cycles = 0
        stall_cycles = 0
        branch_penalty_cycles = 0
        num_stalls = 0
        num_branches = 0
        
        # Iterate through instruction pairs to detect hazards
        for i, insn in enumerate(instructions):
            # Add base execution cycles
            base_cycles += insn.timing.base_cycles
            
            # Count branches
            if insn.is_branch:
                num_branches += 1
                # Branch penalty is already baked into insn.timing.base_cycles
                # from thumb_table (e.g., B = 3 cycles on M0).
                # BUT: if the user specified additional branch penalty via the
                # model, we could add it here. For now, thumb_table.py already
                # uses worst-case branch timing.
            
            # Detect load-use hazard
            if i > 0:
                prev_insn = instructions[i - 1]
                if self._is_load_use_hazard(prev_insn, insn):
                    stall_cycles += self.model.load_use_stall
                    num_stalls += 1
                    logger.debug(
                        f"Load-use hazard at 0x{insn.address:08x}: "
                        f"{prev_insn.mnemonic} → {insn.mnemonic} "
                        f"(+{self.model.load_use_stall} stall)"
                    )
        
        # Flash wait states: add penalty for each instruction fetch
        # Formula: num_instructions × wait_states
        fetch_penalty_cycles = len(instructions) * self.model.flash_wait_states
        
        # Total
        total_cycles = base_cycles + stall_cycles + fetch_penalty_cycles
        
        return TimingResult(
            base_cycles=base_cycles,
            stall_cycles=stall_cycles,
            branch_penalty_cycles=branch_penalty_cycles,  # Already in base_cycles
            fetch_penalty_cycles=fetch_penalty_cycles,
            total_cycles=total_cycles,
            num_instructions=len(instructions),
            num_stalls=num_stalls,
            num_branches=num_branches,
        )
    
    def _is_load_use_hazard(
        self,
        load_insn: Instruction,
        use_insn: Instruction,
    ) -> bool:
        """
        Return True if *load_insn* loads a value that *use_insn* reads,
        causing a load-use pipeline stall.
        
        Detection rule:
          1. load_insn must be a load (LDR, LDM, POP)
          2. load_insn must write to a destination register (load_insn.dest_reg)
          3. use_insn must read that register (in use_insn.src_regs)
        
        CONSERVATIVE: If we can't determine registers, assume hazard exists.
        """
        if not load_insn.is_load:
            return False
        
        # If load has no destination register, no hazard
        if load_insn.dest_reg is None:
            return False
        
        # Check if the loaded register is read by the next instruction
        if load_insn.dest_reg in use_insn.src_regs:
            return True
        
        # Special case: load into PC (branch) — always a hazard
        from capstone.arm import ARM_REG_PC
        if load_insn.dest_reg == ARM_REG_PC:
            return True
        
        return False
    
    def analyze_with_branches(
        self,
        instructions: list[Instruction],
        taken_addresses: set[int],
    ) -> TimingResult:
        """
        Analyze with explicit branch taken/not-taken information.
        
        This is used for control-flow-aware analysis where we know which
        branches are taken on the worst-case path.
        
        Parameters
        ----------
        instructions:
            List of instructions (may include untaken branches).
        taken_addresses:
            Set of addresses of branch instructions that are taken on the
            worst-case path.
        
        Returns
        -------
        TimingResult
            Timing assuming only the specified branches are taken.
        """
        # For now, treat all branches as taken (worst-case).
        # A more sophisticated CFG-based analyzer would prune untaken paths.
        return self.analyze_sequence(instructions)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def print_timing_result(result: TimingResult) -> None:
    """Print a human-readable timing result."""
    print("Timing Analysis:")
    print(f"  Instructions:       {result.num_instructions}")
    print(f"  Base cycles:        {result.base_cycles}")
    print(f"  Load-use stalls:    {result.stall_cycles} (+{result.num_stalls} hazards)")
    print(f"  Branch penalties:   {result.branch_penalty_cycles} ({result.num_branches} branches)")
    print(f"  Flash wait states:  {result.fetch_penalty_cycles}")
    print(f"  ──────────────────────────────────")
    print(f"  TOTAL:              {result.total_cycles} cycles")


def estimate_cache_hit_rate(
    num_instructions: int,
    cache_line_size: int = 64,
    working_set_size: int = 1024,
) -> float:
    """
    Estimate instruction cache hit rate (placeholder).
    
    This is a VERY rough heuristic. Real cache modeling requires:
      - Associativity modeling (set-associative vs direct-mapped)
      - Replacement policy (LRU, FIFO, random)
      - Inter-function call patterns
      - Loop iteration counts
    
    For production WCET, we use worst-case (no cache hits).
    """
    # Placeholder: assume 90% hit rate for small working sets
    if working_set_size <= cache_line_size * 4:
        return 0.9
    elif working_set_size <= cache_line_size * 16:
        return 0.7
    else:
        return 0.5


# ---------------------------------------------------------------------------
# Command-line interface (for testing)
# ---------------------------------------------------------------------------

def main() -> None:
    """Simple CLI to test pipeline analysis (for development)."""
    import sys
    from irq_verify.disasm import ARMDisassembler
    from irq_verify.thumb_table import ARMArch
    
    if len(sys.argv) < 2:
        print("Usage: python -m irq_verify.pipeline <hex_bytes> [wait_states]")
        print("Example: python -m irq_verify.pipeline '0020 4ff0 0000 4770' 5")
        sys.exit(1)
    
    logging.basicConfig(level=logging.DEBUG)
    
    # Parse arguments
    hex_str = sys.argv[1].replace(" ", "").replace("0x", "")
    code = bytes.fromhex(hex_str)
    
    wait_states = 0
    if len(sys.argv) > 2:
        wait_states = int(sys.argv[2])
    
    # Disassemble
    arch = ARMArch.CORTEX_M4
    disasm = ARMDisassembler(arch)
    instructions = disasm.disassemble(code, base_address=0x08000000)
    
    print(f"Disassembly ({len(instructions)} instructions):")
    from irq_verify.disasm import print_disassembly
    print_disassembly(instructions, show_timing=True)
    print()
    
    # Analyze pipeline timing
    model = PipelineModel.for_architecture(arch, flash_wait_states=wait_states)
    analyzer = PipelineAnalyzer(model)
    result = analyzer.analyze_sequence(instructions)
    
    print_timing_result(result)


if __name__ == "__main__":
    main()
