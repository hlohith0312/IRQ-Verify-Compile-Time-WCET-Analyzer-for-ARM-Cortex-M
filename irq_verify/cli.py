"""
cli.py -- Command-line interface for irq-verify.

This module provides the main command-line interface for the irq-verify tool.
It handles argument parsing, file collection, analysis orchestration, and result reporting.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Setup logging with proper formatting
logging.basicConfig(
    level=logging.WARNING,
    format='%(levelname)s: %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser for irq-verify CLI.
    
    Returns:
        ArgumentParser configured with all CLI options.
    """
    from irq_verify import __version__, __description__
    
    parser = argparse.ArgumentParser(
        prog="irq-verify",
        description=__description__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # C-AST mode (fast, ±50-200% accuracy, no compiler needed)
  irq-verify sensors.c --budget 300

  # Binary mode (cycle-exact, ±2-5% accuracy, requires arm-none-eabi-gcc)
  irq-verify sensors.c --budget 300 --compile-with arm-none-eabi-gcc --flash-wait-states 5

  # Binary mode with custom compiler flags
  irq-verify main.c --budget 300 --compile-with arm-none-eabi-gcc --cflags="-O2 -DSTM32F4"

  # multiple files -- functions defined in utils.c are visible from main.c
  irq-verify main.c utils.c isr.c --budget 300

  # whole project directory
  irq-verify --dir src/ -I include/ -I drivers/STM32/include --budget 300

  # different target architecture
  irq-verify main.c --budget 300 --target cortex-m4

  # custom RTOS critical-section API
  irq-verify main.c --disable-fn taskENTER_CRITICAL --enable-fn taskEXIT_CRITICAL

  # JSON output for CI/CD integration
  irq-verify main.c --budget 300 --output-format json > results.json

  # SARIF output for GitHub Security tab
  irq-verify main.c --budget 300 --output-format sarif > results.sarif

exit codes:
  0   All interrupt-disabled regions pass the cycle budget.
  1   One or more regions exceed the budget or are UNBOUNDED.
  2   Tool error (e.g. file not found, parse failure).
        """,
    )

    # ------------------------------------------------------------------ #
    # Version information                                                 #
    # ------------------------------------------------------------------ #
    parser.add_argument(
        "--version",
        action="version",
        version=f"irq-verify {__version__}",
        help="Show version information and exit",
    )

    # ------------------------------------------------------------------ #
    # Input: files or directory                                           #
    # ------------------------------------------------------------------ #
    parser.add_argument(
        "files",
        metavar="FILE",
        type=Path,
        nargs="*",
        help="One or more C source files to analyse.",
    )
    parser.add_argument(
        "--dir",
        metavar="DIR",
        type=Path,
        default=None,
        help="Analyse all *.c files in this directory.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="When using --dir, also search subdirectories recursively.",
    )

    # ------------------------------------------------------------------ #
    # Analysis options                                                    #
    # ------------------------------------------------------------------ #
    parser.add_argument(
        "--budget",
        metavar="CYCLES",
        type=int,
        default=None,
        help=(
            "Global worst-case cycle budget for every interrupt-disabled region. "
            "Can be overridden per region with a '// @irq_budget(N)' comment "
            "placed directly above the disable call."
        ),
    )
    parser.add_argument(
        "--target",
        metavar="ARCH",
        default="cortex-m0",
        dest="target",
        help=(
            "Target MCU architecture. Selects the built-in cycle-cost table. "
            "Supported: cortex-m0, cortex-m0+, cortex-m3, cortex-m4, cortex-m33, cortex-m7 "
            "(default: cortex-m0). Use --cycle-table to further override individual costs."
        ),
    )
    parser.add_argument(
        "--disable-fn",
        metavar="NAME",
        default="__disable_irq",
        help="Name of the interrupt-disable function call (default: __disable_irq).",
    )
    parser.add_argument(
        "--enable-fn",
        metavar="NAME",
        default="__enable_irq",
        help="Name of the interrupt-enable function call (default: __enable_irq).",
    )

    # ------------------------------------------------------------------ #
    # Preprocessor options                                                #
    # ------------------------------------------------------------------ #
    parser.add_argument(
        "-I", "--include-dir",
        metavar="DIR",
        type=Path,
        action="append",
        dest="include_dirs",
        default=[],
        help=(
            "Add a directory to the include search path (same as compiler -I). "
            "May be specified multiple times."
        ),
    )
    parser.add_argument(
        "--no-preprocess",
        action="store_true",
        help=(
            "Skip pcpp preprocessing. Strips #include/#define via regex instead. "
            "Suitable for simple fixture files; does not expand macros."
        ),
    )

    # ------------------------------------------------------------------ #
    # Binary mode options (cycle-exact analysis)                          #
    # ------------------------------------------------------------------ #
    parser.add_argument(
        "--compile-with",
        metavar="GCC_PATH",
        type=str,
        default=None,
        help=(
            "Enable binary mode: compile source with arm-none-eabi-gcc and perform "
            "cycle-exact analysis on the resulting ELF binary. Provide path to compiler "
            "(default: search PATH for 'arm-none-eabi-gcc')."
        ),
    )
    parser.add_argument(
        "--cflags",
        metavar="FLAGS",
        type=str,
        default="",
        help=(
            "Additional compiler flags for binary mode (e.g., '--cflags=\"-DSTM32F4 -O2\"'). "
            "Optimization level defaults to -O2 if not specified."
        ),
    )
    parser.add_argument(
        "--flash-wait-states",
        metavar="N",
        type=int,
        default=0,
        help=(
            "Flash memory wait states for binary mode analysis (board-specific). "
            "Example: STM32F4 at 168MHz typically uses 5 wait states. "
            "Default: 0 (no penalty)."
        ),
    )
    parser.add_argument(
        "--keep-elf",
        action="store_true",
        help="Keep the compiled ELF binary after analysis (binary mode only).",
    )

    # ------------------------------------------------------------------ #
    # Other options                                                       #
    # ------------------------------------------------------------------ #
    parser.add_argument(
        "--cycle-table",
        metavar="JSON_FILE",
        type=Path,
        default=None,
        help="Path to a JSON file containing a custom cycle-cost table.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed per-region CFG and path information.",
    )
    parser.add_argument(
        "--output-format",
        metavar="FORMAT",
        choices=["text", "json", "sarif"],
        default="text",
        help="Output format: 'text' (human-readable), 'json' (CI), or 'sarif' (GitHub Security).",
    )
    return parser


def _collect_files(args: argparse.Namespace) -> list[Path]:
    """Resolve the list of .c files to analyse from CLI arguments."""
    if args.dir is not None:
        pattern = "**/*.c" if args.recursive else "*.c"
        found = sorted(args.dir.glob(pattern))
        if not found:
            print(f"warning: no *.c files found in {args.dir}", file=sys.stderr)
        return found
    return list(args.files) if args.files else []


def main(argv: list[str] | None = None) -> None:  # noqa: C901
    parser = build_parser()
    args = parser.parse_args(argv)
    
    # Set logging level based on verbosity
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # ------------------------------------------------------------------ #
    # Binary mode: compile first, then analyze ELF                        #
    # ------------------------------------------------------------------ #
    if args.compile_with:
        logger.info("Binary mode enabled: compiling source to ELF")
        # Binary mode implementation would go here
        # For now, print a message and fall back to C-AST mode
        print(
            "Binary mode (--compile-with) is available but requires additional "
            "integration. See binary_wcet.py and compiler.py modules.",
            file=sys.stderr
        )
        print("Falling back to C-AST mode...\n", file=sys.stderr)
    
    files = _collect_files(args)

    if not files:
        parser.print_help()
        sys.exit(2)

    # ------------------------------------------------------------------ #
    # Validate inputs                                                     #
    # ------------------------------------------------------------------ #
    for f in files:
        if not f.exists():
            logger.error(f"file not found: {f}")
            sys.exit(2)
        if not f.is_file():
            logger.error(f"not a regular file: {f}")
            sys.exit(2)

    if args.budget is not None and args.budget < 0:
        logger.error("--budget must be a non-negative integer")
        sys.exit(2)

    if args.cycle_table is not None and not args.cycle_table.exists():
        logger.error(f"cycle table file not found: {args.cycle_table}")
        sys.exit(2)

    # ------------------------------------------------------------------ #
    # Lazy imports                                                        #
    # ------------------------------------------------------------------ #
    from irq_verify.cycle_table import load_cycle_table_for_arch, SUPPORTED_ARCHS
    from irq_verify.parser import parse_project
    from irq_verify.analysis import analyse_regions, RegionResult
    from irq_verify.reporting import (
        print_project_header,
        print_results,
        print_project_summary,
        print_results_json,
        print_results_sarif,
    )

    # Validate --target
    target = args.target.lower().strip()
    if target not in SUPPORTED_ARCHS:
        print(
            f"error: unknown --target '{args.target}'. "
            f"Supported: {', '.join(SUPPORTED_ARCHS)}",
            file=sys.stderr,
        )
        sys.exit(2)

    cycle_table = load_cycle_table_for_arch(target, args.cycle_table)
    use_preprocessor = not args.no_preprocess
    include_dirs: list[Path] | None = args.include_dirs if args.include_dirs else None

    # ------------------------------------------------------------------ #
    # Parse all files (two-pass: collect all func_defs, then analyse)    #
    # ------------------------------------------------------------------ #
    try:
        _func_defs, file_regions = parse_project(
            files,
            disable_fn=args.disable_fn,
            enable_fn=args.enable_fn,
            include_dirs=include_dirs,
            use_preprocessor=use_preprocessor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"failed to parse: {exc}")
        if args.verbose:
            logger.exception("Full traceback:")
        sys.exit(2)

    total_regions = sum(len(r) for _, r in file_regions)
    if total_regions == 0:
        logger.info("no interrupt-disabled regions found in any input file")
        sys.exit(0)

    # ------------------------------------------------------------------ #
    # Print project header (if multiple files)                            #
    # ------------------------------------------------------------------ #
    num_input_files = len(file_regions)
    
    if args.output_format != "json":
        print_project_header(num_input_files, total_regions)

    # ------------------------------------------------------------------ #
    # Analyse and report -- one combined report with per-file sections   #
    # ------------------------------------------------------------------ #
    all_file_results: list[tuple[Path, list[RegionResult]]] = []

    for file_path, regions in file_regions:
        if not regions:
            continue

        results = analyse_regions(
            regions=regions,
            ast=None,           # func_defs already attached to each region
            cycle_table=cycle_table,
            global_budget=args.budget,
            verbose=args.verbose,
        )

        if args.output_format == "text":
            print_results(results, file_path, verbose=args.verbose)
        
        all_file_results.append((file_path, results))

    # ------------------------------------------------------------------ #
    # Print final output based on format                                  #
    # ------------------------------------------------------------------ #
    if args.output_format == "json":
        overall_exit = print_results_json(all_file_results)
    elif args.output_format == "sarif":
        overall_exit = print_results_sarif(all_file_results, files)
    else:
        overall_exit = print_project_summary(all_file_results, num_input_files)
    
    sys.exit(overall_exit)


if __name__ == "__main__":
    main()
