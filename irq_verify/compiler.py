"""
compiler.py — ARM GCC compilation orchestration for binary mode analysis.

This module manages the compilation of C source files to ARM ELF binaries,
handling toolchain invocation, build flags, and temporary file management.

TYPICAL WORKFLOW
----------------
1. User provides C source file + compiler path
2. We invoke arm-none-eabi-gcc with appropriate flags:
   - Debug info: -g (DWARF for source line mapping)
   - Optimization: -O2 (realistic production code)
   - Target CPU: -mcpu=cortex-m4
   - Thumb mode: -mthumb
3. We capture the output ELF binary
4. We pass it to binary_wcet.py for analysis

TOOLCHAIN REQUIREMENTS
----------------------
- arm-none-eabi-gcc (GNU ARM Embedded Toolchain)
- arm-none-eabi-objdump (for verification)
- arm-none-eabi-size (for stats)

Download: https://developer.arm.com/tools-and-software/open-source-software/developer-tools/gnu-toolchain/gnu-rm
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CompilationResult:
    """
    Result of compiling a C source file to ARM ELF binary.
    
    Attributes
    ----------
    success:
        True if compilation succeeded.
    elf_path:
        Path to the compiled ELF file (None if compilation failed).
    stdout:
        Compiler stdout output.
    stderr:
        Compiler stderr output (warnings, errors).
    return_code:
        Compiler process return code (0 = success).
    command:
        Full command line used for compilation.
    """
    success: bool
    elf_path: Optional[Path]
    stdout: str
    stderr: str
    return_code: int
    command: str


# ---------------------------------------------------------------------------
# Compiler wrapper
# ---------------------------------------------------------------------------

class ARMCompiler:
    """
    Wrapper for arm-none-eabi-gcc compilation.
    
    Parameters
    ----------
    compiler_path:
        Path to arm-none-eabi-gcc executable (default: search PATH).
    target_cpu:
        Target CPU (e.g., "cortex-m0", "cortex-m4").
    optimization:
        Optimization level (e.g., "-O0", "-O2", "-Os").
    extra_flags:
        Additional compiler flags as a list of strings.
    """
    
    def __init__(
        self,
        compiler_path: str = "arm-none-eabi-gcc",
        target_cpu: str = "cortex-m0",
        optimization: str = "-O2",
        extra_flags: Optional[list[str]] = None,
    ):
        self.compiler_path = compiler_path
        self.target_cpu = target_cpu
        self.optimization = optimization
        self.extra_flags = extra_flags or []
        
        # Verify compiler is available
        if not self._check_compiler():
            raise FileNotFoundError(
                f"ARM compiler not found: {compiler_path}\n"
                f"Please install the GNU ARM Embedded Toolchain:\n"
                f"https://developer.arm.com/tools-and-software/open-source-software/developer-tools/gnu-toolchain/gnu-rm"
            )
        
        logger.info(f"ARM compiler initialized: {compiler_path} -mcpu={target_cpu} {optimization}")
    
    def _check_compiler(self) -> bool:
        """Verify that the compiler is available and executable."""
        return shutil.which(self.compiler_path) is not None
    
    def compile(
        self,
        source_path: Path,
        output_path: Optional[Path] = None,
        include_dirs: Optional[list[Path]] = None,
    ) -> CompilationResult:
        """
        Compile a C source file to an ARM ELF binary.
        
        Parameters
        ----------
        source_path:
            Path to the .c source file.
        output_path:
            Path for the output .elf file (default: temporary file).
        include_dirs:
            Additional include directories (passed as -I flags).
        
        Returns
        -------
        CompilationResult
            Compilation outcome with ELF path if successful.
        """
        if not source_path.exists():
            return CompilationResult(
                success=False,
                elf_path=None,
                stdout="",
                stderr=f"Source file not found: {source_path}",
                return_code=1,
                command="",
            )
        
        # Determine output path
        if output_path is None:
            # Create temporary file (caller must clean up)
            tmp = tempfile.NamedTemporaryFile(suffix=".elf", delete=False)
            output_path = Path(tmp.name)
            tmp.close()
        
        # Build command line
        cmd = [
            self.compiler_path,
            str(source_path),
            "-o", str(output_path),
            "-mcpu=" + self.target_cpu,
            "-mthumb",              # Thumb mode (standard for Cortex-M)
            self.optimization,
            "-g",                   # Debug info (DWARF)
            "-Wall",                # Enable warnings
            "-ffunction-sections",  # Separate function sections (for size analysis)
            "-fdata-sections",
        ]
        
        # Add include directories
        if include_dirs:
            for inc_dir in include_dirs:
                cmd.append(f"-I{inc_dir}")
        
        # Add extra flags
        cmd.extend(self.extra_flags)
        
        # Execute compilation
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            success = result.returncode == 0
            
            if success:
                logger.info(f"Compilation succeeded: {output_path}")
            else:
                logger.error(f"Compilation failed: {result.stderr}")
            
            return CompilationResult(
                success=success,
                elf_path=output_path if success else None,
                stdout=result.stdout,
                stderr=result.stderr,
                return_code=result.returncode,
                command=" ".join(cmd),
            )
        
        except subprocess.TimeoutExpired:
            return CompilationResult(
                success=False,
                elf_path=None,
                stdout="",
                stderr="Compilation timed out after 30 seconds",
                return_code=-1,
                command=" ".join(cmd),
            )
        
        except Exception as exc:
            return CompilationResult(
                success=False,
                elf_path=None,
                stdout="",
                stderr=f"Compilation error: {exc}",
                return_code=-1,
                command=" ".join(cmd),
            )
    
    def get_binary_size(self, elf_path: Path) -> Optional[dict[str, int]]:
        """
        Get section sizes from an ELF binary using arm-none-eabi-size.
        
        Returns
        -------
        dict or None
            Dictionary with keys: "text", "data", "bss" (sizes in bytes).
        """
        size_tool = self.compiler_path.replace("gcc", "size")
        
        if not shutil.which(size_tool):
            logger.warning(f"arm-none-eabi-size not found: {size_tool}")
            return None
        
        try:
            result = subprocess.run(
                [size_tool, str(elf_path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            
            if result.returncode != 0:
                return None
            
            # Parse output:
            # text    data     bss     dec     hex filename
            # 1234     56      78    1368     558 firmware.elf
            lines = result.stdout.strip().split("\n")
            if len(lines) < 2:
                return None
            
            header = lines[0].split()
            values = lines[1].split()
            
            if len(header) < 3 or len(values) < 3:
                return None
            
            return {
                "text": int(values[0]),
                "data": int(values[1]),
                "bss": int(values[2]),
            }
        
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to get binary size: {exc}")
            return None


# ---------------------------------------------------------------------------
# High-level compile-and-analyze workflow
# ---------------------------------------------------------------------------

def compile_and_analyze(
    source_path: Path,
    target_cpu: str = "cortex-m4",
    optimization: str = "-O2",
    flash_wait_states: int = 0,
    include_dirs: Optional[list[Path]] = None,
    compiler_path: str = "arm-none-eabi-gcc",
    keep_elf: bool = False,
) -> Optional[Path]:
    """
    Compile a C source file and return the path to the ELF binary.
    
    This is a convenience function for the CLI --compile-with mode.
    
    Parameters
    ----------
    source_path:
        Path to the .c source file.
    target_cpu:
        Target CPU (e.g., "cortex-m4").
    optimization:
        Optimization level ("-O0", "-O2", "-Os").
    flash_wait_states:
        Flash wait states (for binary analysis).
    include_dirs:
        Additional include directories.
    compiler_path:
        Path to arm-none-eabi-gcc.
    keep_elf:
        If True, keep the ELF file; if False, use temporary file.
    
    Returns
    -------
    Path or None
        Path to compiled ELF binary if successful, else None.
    """
    compiler = ARMCompiler(
        compiler_path=compiler_path,
        target_cpu=target_cpu,
        optimization=optimization,
    )
    
    output_path = None
    if keep_elf:
        output_path = source_path.with_suffix(".elf")
    
    result = compiler.compile(
        source_path=source_path,
        output_path=output_path,
        include_dirs=include_dirs,
    )
    
    if not result.success:
        logger.error(f"Compilation failed:\n{result.stderr}")
        return None
    
    # Print size info
    sizes = compiler.get_binary_size(result.elf_path)
    if sizes:
        logger.info(
            f"Binary size: text={sizes['text']} data={sizes['data']} bss={sizes['bss']} bytes"
        )
    
    return result.elf_path


# ---------------------------------------------------------------------------
# Command-line interface (for testing)
# ---------------------------------------------------------------------------

def main() -> None:
    """Simple CLI to compile a C file."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python -m irq_verify.compiler <source.c> [output.elf]")
        sys.exit(1)
    
    logging.basicConfig(level=logging.INFO)
    
    source_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    
    compiler = ARMCompiler(target_cpu="cortex-m4", optimization="-O2")
    result = compiler.compile(source_path, output_path)
    
    if result.success:
        print(f"✓ Compilation successful: {result.elf_path}")
        
        sizes = compiler.get_binary_size(result.elf_path)
        if sizes:
            print(f"  text: {sizes['text']:6d} bytes")
            print(f"  data: {sizes['data']:6d} bytes")
            print(f"  bss:  {sizes['bss']:6d} bytes")
    else:
        print(f"✗ Compilation failed (exit code {result.return_code})")
        print(result.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
