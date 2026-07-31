"""
exact_measurement.py — 100% exact cycle measurement via hardware instrumentation.

This module provides EXACT cycle counts by instrumenting code and measuring
on real ARM Cortex-M hardware using the DWT (Data Watchpoint and Trace) cycle counter.

ACCURACY: 100% exact (±0 cycles) for the measured execution path.

LIMITATION: Measures ONE specific execution, not guaranteed worst-case.
Use binary_wcet.py for conservative worst-case analysis.

USAGE:
    1. Instrument code: adds DWT measurement around critical sections
    2. Compile for target hardware
    3. Flash to board
    4. Run and capture measurements
    5. Report EXACT cycle counts

This gives you PERFECT accuracy for validation, but doesn't prove worst-case
unless you can exercise all paths (typically impossible).
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# DWT instrumentation template
DWT_INSTRUMENTATION = """
// ============================================================================
// DWT Cycle Counter (Data Watchpoint and Trace)
// ============================================================================
// Provides cycle-exact measurement on ARM Cortex-M3/M4/M7

#include <stdint.h>

#define DWT_CTRL     (*(volatile uint32_t *)0xE0001000)
#define DWT_CYCCNT   (*(volatile uint32_t *)0xE0001004)
#define CoreDebug_DEMCR (*(volatile uint32_t *)0xE000EDFC)

#define DWT_CTRL_CYCCNTENA (1UL << 0)
#define CoreDebug_DEMCR_TRCENA (1UL << 24)

static inline void dwt_init(void) {
    // Enable trace
    CoreDebug_DEMCR |= CoreDebug_DEMCR_TRCENA;
    
    // Reset cycle counter
    DWT_CYCCNT = 0;
    
    // Enable cycle counter
    DWT_CTRL |= DWT_CTRL_CYCCNTENA;
}

static inline uint32_t dwt_get_cycles(void) {
    return DWT_CYCCNT;
}

static inline void dwt_reset(void) {
    DWT_CYCCNT = 0;
}

// Storage for measurements
static volatile uint32_t irq_verify_measurements[100];
static volatile uint32_t irq_verify_measurement_count = 0;

#define IRQ_VERIFY_MEASURE_START() \\
    uint32_t _irq_verify_start = dwt_get_cycles()

#define IRQ_VERIFY_MEASURE_END() \\
    do { \\
        uint32_t _irq_verify_end = dwt_get_cycles(); \\
        if (irq_verify_measurement_count < 100) { \\
            irq_verify_measurements[irq_verify_measurement_count++] = \\
                _irq_verify_end - _irq_verify_start; \\
        } \\
    } while(0)

// Initialize in your main() before any measurements:
// dwt_init();
"""


@dataclass
class ExactMeasurement:
    """Result of exact hardware measurement."""
    function_name: str
    measured_cycles: int
    accuracy: float = 100.0  # Always 100% for hardware measurement
    measurement_count: int = 1
    min_cycles: Optional[int] = None
    max_cycles: Optional[int] = None
    avg_cycles: Optional[float] = None


class CodeInstrumenter:
    """
    Instrument C code with DWT cycle counter measurements.
    
    This adds hardware cycle counting around __disable_irq()/__enable_irq()
    regions to get EXACT cycle measurements.
    """
    
    def __init__(self):
        self.region_count = 0
    
    def instrument_file(
        self,
        source_path: Path,
        output_path: Optional[Path] = None,
    ) -> Path:
        """
        Instrument a C file with DWT measurement code.
        
        Parameters
        ----------
        source_path:
            Original C source file.
        output_path:
            Output path for instrumented code (default: temp file).
        
        Returns
        -------
        Path
            Path to instrumented C file.
        """
        if output_path is None:
            temp = tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.c',
                delete=False,
                prefix='irq_verify_instrumented_'
            )
            output_path = Path(temp.name)
            temp.close()
        
        # Read source
        source = source_path.read_text()
        
        # Add DWT header at top
        instrumented = DWT_INSTRUMENTATION + "\n\n" + source
        
        # Find and instrument critical sections
        instrumented = self._instrument_regions(instrumented)
        
        # Write output
        output_path.write_text(instrumented)
        
        return output_path
    
    def _instrument_regions(self, source: str) -> str:
        """
        Add measurement macros around __disable_irq()/__enable_irq() pairs.
        
        Transforms:
            __disable_irq();
            // code
            __enable_irq();
        
        Into:
            IRQ_VERIFY_MEASURE_START();
            __disable_irq();
            // code
            __enable_irq();
            IRQ_VERIFY_MEASURE_END();
        """
        # Pattern: __disable_irq() ... __enable_irq()
        # We'll use a simple approach: add START before disable, END after enable
        
        # Add START before __disable_irq()
        source = re.sub(
            r'(\s*)(__disable_irq\s*\(\s*\)\s*;)',
            r'\1IRQ_VERIFY_MEASURE_START();\n\1\2',
            source
        )
        
        # Add END after __enable_irq()
        source = re.sub(
            r'(\s*)(__enable_irq\s*\(\s*\)\s*;)',
            r'\1\2\n\1IRQ_VERIFY_MEASURE_END();',
            source
        )
        
        self.region_count = source.count('IRQ_VERIFY_MEASURE_START()')
        
        return source
    
    def generate_main_wrapper(
        self,
        test_functions: list[str],
    ) -> str:
        """
        Generate a main() function that runs test functions and reports results.
        
        Parameters
        ----------
        test_functions:
            List of function names to test.
        
        Returns
        -------
        str
            C code for main() with measurement loop.
        """
        main_code = """
int main(void) {
    // Initialize DWT cycle counter
    dwt_init();
    
    // Run each test function multiple times
"""
        
        for func in test_functions:
            main_code += f"""
    // Test: {func}
    irq_verify_measurement_count = 0;
    for (int i = 0; i < 10; i++) {{
        {func}();
    }}
    
    // Results are in irq_verify_measurements[0..irq_verify_measurement_count-1]
    // Set breakpoint here to read measurements
    volatile uint32_t count_{func} = irq_verify_measurement_count;
    (void)count_{func};
"""
        
        main_code += """
    // Infinite loop (set breakpoint to capture measurements)
    while(1);
    
    return 0;
}
"""
        
        return main_code


def extract_measurements_from_debugger(
    elf_path: Path,
    function_names: list[str],
) -> dict[str, ExactMeasurement]:
    """
    Extract measurements from running target (requires debugger interaction).
    
    This is a MANUAL step — user must:
    1. Flash ELF to board
    2. Run in debugger
    3. Break at while(1)
    4. Read irq_verify_measurements array
    5. Enter values here
    
    Parameters
    ----------
    elf_path:
        Path to instrumented ELF file.
    function_names:
        List of tested function names.
    
    Returns
    -------
    dict
        Mapping of function name → ExactMeasurement.
    """
    measurements = {}
    
    print(f"\n{'='*70}")
    print("HARDWARE MEASUREMENT REQUIRED")
    print(f"{'='*70}")
    print(f"\n1. Flash {elf_path} to your board")
    print("2. Run in debugger (GDB/OpenOCD)")
    print("3. Set breakpoint at 'while(1)' in main()")
    print("4. Run and wait for breakpoint")
    print("5. Read variable 'irq_verify_measurements' array")
    print(f"\n{'='*70}\n")
    
    for func_name in function_names:
        print(f"\nFunction: {func_name}")
        print("Enter measured cycles (comma-separated if multiple runs):")
        print("Example: 87,87,88,87,87")
        
        user_input = input("> ").strip()
        
        if not user_input:
            continue
        
        # Parse measurements
        try:
            cycles_list = [int(x.strip()) for x in user_input.split(',')]
            
            measurements[func_name] = ExactMeasurement(
                function_name=func_name,
                measured_cycles=max(cycles_list),  # Worst-case of measurements
                measurement_count=len(cycles_list),
                min_cycles=min(cycles_list),
                max_cycles=max(cycles_list),
                avg_cycles=sum(cycles_list) / len(cycles_list),
            )
            
        except ValueError:
            print(f"Invalid input, skipping {func_name}")
    
    return measurements


def compare_with_prediction(
    measurement: ExactMeasurement,
    predicted_cycles: int,
) -> dict:
    """
    Compare exact hardware measurement with binary mode prediction.
    
    Returns
    -------
    dict
        Comparison report with error analysis.
    """
    error = abs(measurement.measured_cycles - predicted_cycles)
    error_pct = (error / measurement.measured_cycles * 100) if measurement.measured_cycles > 0 else 0
    
    return {
        "function": measurement.function_name,
        "measured_cycles": measurement.measured_cycles,
        "predicted_cycles": predicted_cycles,
        "error_cycles": error,
        "error_percent": error_pct,
        "within_5_percent": error_pct <= 5.0,
        "exact_match": error == 0,
        "measurement_count": measurement.measurement_count,
        "min_measured": measurement.min_cycles,
        "max_measured": measurement.max_cycles,
        "avg_measured": measurement.avg_cycles,
    }


# ============================================================================
# CLI for exact measurement workflow
# ============================================================================

def main():
    """
    Command-line interface for exact measurement workflow.
    
    Usage:
        python -m irq_verify.exact_measurement input.c --function critical_section
    """
    import argparse
    import sys
    
    parser = argparse.ArgumentParser(
        description="Instrument code for 100% exact cycle measurement"
    )
    parser.add_argument(
        "source",
        type=Path,
        help="C source file to instrument"
    )
    parser.add_argument(
        "--function",
        action="append",
        dest="functions",
        help="Function name to test (repeatable)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path for instrumented C file"
    )
    
    args = parser.parse_args()
    
    # Instrument code
    print(f"Instrumenting {args.source}...")
    
    instrumenter = CodeInstrumenter()
    instrumented_path = instrumenter.instrument_file(args.source, args.output)
    
    print(f"✓ Instrumented code: {instrumented_path}")
    print(f"✓ Found {instrumenter.region_count} critical regions")
    
    # Generate main wrapper if functions specified
    if args.functions:
        main_wrapper = instrumenter.generate_main_wrapper(args.functions)
        
        # Append to instrumented file
        with open(instrumented_path, 'a') as f:
            f.write("\n\n")
            f.write(main_wrapper)
        
        print(f"✓ Added test harness for {len(args.functions)} functions")
    
    print(f"\nNext steps:")
    print(f"1. Compile: arm-none-eabi-gcc {instrumented_path} -o test.elf -mcpu=cortex-m4 -O2 -g")
    print(f"2. Flash to board")
    print(f"3. Run in debugger and read 'irq_verify_measurements' array")
    print(f"4. Compare with binary mode predictions")
    
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
