"""
reporting.py — Format and print analysis results for irq-verify.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from irq_verify.analysis import RegionResult

# Force UTF-8 output on Windows so we can use nice characters without crashing.
# Falls back gracefully if stdout doesn't support reconfiguration.
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# ANSI colour codes (disabled on non-TTY outputs)
_USE_COLOUR = sys.stdout.isatty()

_GREEN  = "\033[92m" if _USE_COLOUR else ""
_RED    = "\033[91m" if _USE_COLOUR else ""
_YELLOW = "\033[93m" if _USE_COLOUR else ""
_CYAN   = "\033[96m" if _USE_COLOUR else ""
_BOLD   = "\033[1m"  if _USE_COLOUR else ""
_RESET  = "\033[0m"  if _USE_COLOUR else ""

_SEP = "-" * 60


def _fmt_pass(msg: str) -> str:
    return f"{_GREEN}{_BOLD}PASS{_RESET} {msg}"


def _fmt_fail(msg: str) -> str:
    return f"{_RED}{_BOLD}FAIL{_RESET} {msg}"


def _fmt_header(msg: str) -> str:
    return f"{_BOLD}{_CYAN}{msg}{_RESET}"


def print_project_header(
    total_files: int,
    total_regions: int,
) -> None:
    """Print a project-level header when analyzing multiple files."""
    if total_files <= 1:
        return  # Single file — no need for project header
    
    banner = "═" * 70
    print()
    print(_fmt_header(banner))
    print(_fmt_header("PROJECT ANALYSIS SUMMARY"))
    print(_fmt_header(banner))
    print(f"Files to analyze: {total_files}")
    print(f"Total regions found: {total_regions}")
    print(_fmt_header(banner))
    print()


def print_results(
    results: list["RegionResult"],
    source_file: Path,
    verbose: bool = False,
) -> int:
    """
    Print a human-readable summary of all region results.

    Returns
    -------
    int
        0 if all regions passed, 1 if any region failed.
    """
    total = len(results)
    failures = [r for r in results if not r.passed]

    print(_fmt_header(f"\nirq-verify -- {source_file}"))
    print(_fmt_header(_SEP))
    print(f"Regions analysed: {total}")
    print()

    for i, result in enumerate(results, start=1):
        fn = result.region.containing_function or "<unknown>"
        line = result.region.disable_line
        budget = result.budget_used

        header_line = f"Region {i} -- {fn}() line {line}"
        if budget is not None:
            header_line += f" (budget: {budget} cycles)"
        else:
            header_line += " (no budget declared)"

        print(f"  {_BOLD}{header_line}{_RESET}")

        if result.is_unbounded:
            print(f"  {_fmt_fail('UNBOUNDED')}")
            print(f"    Reason: {result.unbounded_reason}")
        else:
            cycles = result.worst_case_cycles
            if result.passed:
                if budget is not None:
                    print(_fmt_pass(f"{cycles} / {budget} cycles"))
                else:
                    print(_fmt_pass(f"{cycles} cycles (no budget to compare)"))
            else:
                print(_fmt_fail(f"{cycles} cycles > budget {budget}"))

        if verbose or not result.passed:
            _print_path(result)

        print()

    # Summary
    print(_fmt_header(_SEP))
    if not failures:
        print(_fmt_pass(f"All {total} region(s) passed."))
        return 0
    else:
        print(_fmt_fail(f"{len(failures)} of {total} region(s) FAILED."))
        return 1


def print_project_summary(
    file_regions: list[tuple[Path, list["RegionResult"]]],
    num_input_files: int,
) -> int:
    """
    Print a project-level summary when analyzing multiple files.
    
    Parameters
    ----------
    file_regions:
        List of (file_path, results) for files that HAD regions.
    num_input_files:
        Total number of input files analyzed (including those with no regions).
    
    Returns
    -------
    int
        0 if all regions passed, 1 if any region failed.
    """
    if num_input_files <= 1:
        return 0  # Single file — handled by print_results
    
    total_regions = sum(len(results) for _, results in file_regions)
    total_passed = sum(
        sum(1 for r in results if r.passed)
        for _, results in file_regions
    )
    total_failed = total_regions - total_passed
    
    banner = "═" * 70
    print()
    print(_fmt_header(banner))
    print(_fmt_header("FINAL PROJECT SUMMARY"))
    print(_fmt_header(banner))
    
    if total_failed == 0:
        print(_fmt_pass(f"All {total_regions} region(s) across {num_input_files} file(s) PASSED."))
    else:
        print(_fmt_fail(f"{total_failed} of {total_regions} region(s) FAILED."))
        print(f"  Passed: {total_passed}")
        print(f"  Failed: {total_failed}")
    
    print(_fmt_header(banner))
    return 1 if total_failed > 0 else 0


def print_results_json(
    all_file_results: list[tuple[Path, list["RegionResult"]]],
) -> int:
    """
    Print JSON-formatted analysis results for CI/CD integration.
    
    Returns
    -------
    int
        0 if all regions passed, 1 if any region failed.
    """
    output = {
        "files": [],
        "summary": {
            "total_files": len(all_file_results),
            "total_regions": 0,
            "passed": 0,
            "failed": 0,
        }
    }
    
    for file_path, results in all_file_results:
        file_data = {
            "file": str(file_path),
            "regions": []
        }
        
        for result in results:
            region_data = {
                "function": result.region.containing_function,
                "line": result.region.disable_line,
                "budget": result.budget_used,
                "cycles": result.worst_case_cycles,
                "passed": result.passed,
                "unbounded": result.is_unbounded,
                "unbounded_reason": result.unbounded_reason,
                "path": [
                    {
                        "description": step.description,
                        "cycles": step.cycles,
                        "line": step.line
                    }
                    for step in result.worst_case_path
                ]
            }
            file_data["regions"].append(region_data)
            
            output["summary"]["total_regions"] += 1
            if result.passed:
                output["summary"]["passed"] += 1
            else:
                output["summary"]["failed"] += 1
        
        output["files"].append(file_data)
    
    print(json.dumps(output, indent=2))
    
    return 1 if output["summary"]["failed"] > 0 else 0


def _print_path(result: "RegionResult") -> None:
    """Print the worst-case path for a result."""
    if not result.worst_case_path:
        return
    print("    Worst-case path:")
    for step in result.worst_case_path:
        line_tag = f" (line {step.line})" if step.line else ""
        print(f"      [{step.cycles:4d} cy] {step.description}{line_tag}")


def print_results_sarif(
    all_file_results: list[tuple[Path, list["RegionResult"]]],
    files: list[Path],
) -> int:
    """
    Print SARIF-formatted analysis results for GitHub Security integration.
    
    SARIF is used by GitHub Security tab, Azure DevOps, and other security platforms.
    
    Specification: https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html
    
    Parameters
    ----------
    all_file_results:
        List of (file_path, results) for all analyzed files.
    files:
        List of source files analyzed.
    
    Returns
    -------
    int
        0 if all regions passed, 1 if any region failed.
    """
    from datetime import datetime, timezone
    
    sarif_results = []
    has_failures = False
    
    for file_path, results in all_file_results:
        for result in results:
            # Determine rule ID and level
            if result.is_unbounded:
                rule_id = "irq-verify/unbounded-region"
                level = "error"
                message = f"UNBOUNDED: {result.unbounded_reason}"
                has_failures = True
            elif not result.passed:
                rule_id = "irq-verify/budget-exceeded"
                level = "error"
                cycles = result.worst_case_cycles
                budget = result.budget_used
                message = f"Budget exceeded: {cycles} cycles > {budget} cycles"
                has_failures = True
            else:
                rule_id = "irq-verify/budget-passed"
                level = "note"  # Success is reported as note
                cycles = result.worst_case_cycles
                budget = result.budget_used or "none"
                message = f"Passed: {cycles} / {budget} cycles"
            
            # Build SARIF result entry
            sarif_result = {
                "ruleId": rule_id,
                "level": level,
                "message": {
                    "text": message
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": str(file_path.as_posix()),
                                "uriBaseId": "%SRCROOT%"
                            },
                            "region": {
                                "startLine": result.region.disable_line,
                                "startColumn": 1
                            }
                        }
                    }
                ],
                "properties": {
                    "function": result.region.containing_function,
                    "cycles": result.worst_case_cycles,
                    "budget": result.budget_used,
                    "passed": result.passed,
                    "unbounded": result.is_unbounded
                }
            }
            
            # Add worst-case path as code flow (for failures)
            if not result.passed and result.worst_case_path:
                code_flow = {
                    "threadFlows": [
                        {
                            "locations": [
                                {
                                    "location": {
                                        "message": {
                                            "text": f"{step.description} ({step.cycles} cycles)"
                                        },
                                        "physicalLocation": {
                                            "artifactLocation": {
                                                "uri": str(file_path.as_posix()),
                                                "uriBaseId": "%SRCROOT%"
                                            },
                                            "region": {
                                                "startLine": step.line if step.line else result.region.disable_line,
                                                "startColumn": 1
                                            }
                                        }
                                    }
                                }
                                for step in result.worst_case_path
                            ]
                        }
                    ]
                }
                sarif_result["codeFlows"] = [code_flow]
            
            sarif_results.append(sarif_result)
    
    # Build SARIF document
    sarif = {
        "version": "2.1.0",
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "irq-verify",
                        "version": "1.0.0",
                        "informationUri": "https://github.com/your-org/irq-verify",
                        "semanticVersion": "1.0.0",
                        "rules": [
                            {
                                "id": "irq-verify/budget-exceeded",
                                "name": "InterruptBudgetExceeded",
                                "shortDescription": {
                                    "text": "Critical section exceeds cycle budget"
                                },
                                "fullDescription": {
                                    "text": "An interrupt-disabled critical section exceeds its declared worst-case cycle budget, which may cause real-time deadline misses, communication failures, or system instability."
                                },
                                "defaultConfiguration": {
                                    "level": "error"
                                },
                                "help": {
                                    "text": "Reduce the cycle count by optimizing the critical section code, or increase the budget if the deadline allows. See worst-case path for details."
                                }
                            },
                            {
                                "id": "irq-verify/unbounded-region",
                                "name": "UnboundedCriticalSection",
                                "shortDescription": {
                                    "text": "Critical section contains unbounded loops or external calls"
                                },
                                "fullDescription": {
                                    "text": "The critical section contains constructs whose worst-case timing cannot be statically determined (unbounded loops, external function calls, recursion). This prevents worst-case execution time analysis."
                                },
                                "defaultConfiguration": {
                                    "level": "error"
                                },
                                "help": {
                                    "text": "Add @irq_loop_bound(N) annotations to while/do-while loops, inline external functions, or remove unbounded constructs from the critical section."
                                }
                            },
                            {
                                "id": "irq-verify/budget-passed",
                                "name": "InterruptBudgetPassed",
                                "shortDescription": {
                                    "text": "Critical section passes cycle budget"
                                },
                                "fullDescription": {
                                    "text": "The interrupt-disabled critical section completes within its declared cycle budget."
                                },
                                "defaultConfiguration": {
                                    "level": "note"
                                }
                            }
                        ]
                    }
                },
                "results": sarif_results,
                "artifacts": [
                    {
                        "location": {
                            "uri": str(f.as_posix()),
                            "uriBaseId": "%SRCROOT%"
                        }
                    }
                    for f in files
                ],
                "invocations": [
                    {
                        "executionSuccessful": not has_failures,
                        "endTimeUtc": datetime.now(timezone.utc).isoformat()
                    }
                ]
            }
        ]
    }
    
    print(json.dumps(sarif, indent=2))
    
    return 1 if has_failures else 0
