"""
elf_parser.py — ELF binary parser for ARM Cortex-M firmware.

This module extracts executable code, DWARF debug information, and symbol
tables from compiled ARM ELF binaries to enable cycle-exact WCET analysis
at the instruction level.

FEATURES
--------
- Parse .text section containing executable machine code
- Extract DWARF debug info to map instructions back to C source lines
- Locate functions by symbol name (for critical section identification)
- Handle both ARM and Thumb instruction sets
- Support relocated and non-relocated ELF files

TYPICAL WORKFLOW
----------------
1. Compile C source: arm-none-eabi-gcc -g -O2 -mcpu=cortex-m4 main.c -o main.elf
2. Parse ELF: elf = ELFBinary.from_file("main.elf")
3. Find function: func = elf.find_function("__disable_irq")
4. Extract code: code_bytes = elf.get_function_code(func)
5. Disassemble (see disasm.py) and analyze cycles

LIMITATIONS
-----------
- Requires DWARF debug info (compile with -g)
- Does not handle dynamically-linked shared libraries
- Flash wait states must be specified separately (board-specific)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection
    from elftools.dwarf.compileunit import CompileUnit
    from elftools.dwarf.dwarfinfo import DWARFInfo
    from elftools.dwarf.die import DIE
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "pyelftools is required for binary mode analysis.\n"
        "Install it with: pip install pyelftools"
    ) from exc


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ELFFunction:
    """Metadata for a single function extracted from ELF symbol table."""
    name: str               # Function name (e.g., "read_sensor")
    address: int            # Virtual address (VMA) where function starts
    size: int               # Size in bytes
    is_thumb: bool          # True if Thumb mode (LSB of address = 1)
    section_name: str       # Section containing this function (usually ".text")


@dataclass
class SourceLocation:
    """Mapping from instruction address to source file/line."""
    address: int            # Instruction address
    file: str               # Source file path
    line: int               # Line number in source file
    column: int             # Column number (if available)


@dataclass
class ELFBinary:
    """
    Parsed ARM ELF binary with code extraction and debug info.
    
    Attributes
    ----------
    path:
        Path to the ELF file.
    elffile:
        Parsed ELFFile object (from pyelftools).
    arch:
        ARM architecture string (e.g., "ARM", "v7", "v8-M").
    is_little_endian:
        True if little-endian (standard for Cortex-M).
    text_section:
        The .text section containing executable code.
    functions:
        Dictionary mapping function name → ELFFunction.
    dwarf_info:
        DWARF debug information (if present, else None).
    """
    path: Path
    elffile: ELFFile
    arch: str
    is_little_endian: bool
    text_section: Optional[any]  # type: Section from pyelftools
    functions: dict[str, ELFFunction]
    dwarf_info: Optional[DWARFInfo]
    
    @classmethod
    def from_file(cls, path: Path) -> ELFBinary:
        """
        Parse an ARM ELF binary from disk.
        
        Parameters
        ----------
        path:
            Path to the .elf file.
        
        Returns
        -------
        ELFBinary
            Parsed binary metadata.
        
        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        ValueError
            If the file is not a valid ARM ELF binary.
        """
        if not path.exists():
            raise FileNotFoundError(f"ELF file not found: {path}")
        
        with open(path, "rb") as f:
            elffile = ELFFile(f)
            
            # Validate architecture
            machine = elffile.header["e_machine"]
            if machine not in ("EM_ARM", "ARM"):
                raise ValueError(
                    f"Not an ARM binary: e_machine={machine}. "
                    f"Expected EM_ARM (40)."
                )
            
            # Check endianness (Cortex-M is little-endian)
            is_little_endian = elffile.little_endian
            if not is_little_endian:
                logger.warning(
                    f"{path}: Big-endian ARM binary detected. "
                    f"Cortex-M is little-endian; proceed with caution."
                )
            
            # Extract architecture details from ELF flags
            flags = elffile.header["e_flags"]
            arch = _decode_arm_arch_flags(flags)
            logger.debug(f"ELF architecture: {arch} (flags=0x{flags:08x})")
            
            # Find .text section
            text_section = elffile.get_section_by_name(".text")
            if text_section is None:
                raise ValueError(f"No .text section found in {path}")
            
            # Extract symbol table
            functions = _extract_functions(elffile)
            logger.info(
                f"Loaded {len(functions)} functions from {path}"
            )
            
            # Extract DWARF debug info (if present)
            dwarf_info = None
            if elffile.has_dwarf_info():
                dwarf_info = elffile.get_dwarf_info()
                logger.debug(f"DWARF debug info present in {path}")
            else:
                logger.warning(
                    f"{path}: No DWARF debug info found. "
                    f"Source line mapping will not be available. "
                    f"Compile with -g to enable debug info."
                )
            
            # Reopen file for later reads (ELFFile closes it)
            f.seek(0)
            elffile_persistent = ELFFile(f)
            
            return cls(
                path=path,
                elffile=elffile_persistent,
                arch=arch,
                is_little_endian=is_little_endian,
                text_section=text_section,
                functions=functions,
                dwarf_info=dwarf_info,
            )
    
    def find_function(self, name: str) -> Optional[ELFFunction]:
        """
        Find a function by name in the symbol table.
        
        Parameters
        ----------
        name:
            Function name (e.g., "__disable_irq").
        
        Returns
        -------
        ELFFunction or None
            Function metadata if found, else None.
        """
        return self.functions.get(name)
    
    def get_function_code(self, func: ELFFunction) -> bytes:
        """
        Extract raw machine code bytes for a function.
        
        Parameters
        ----------
        func:
            Function metadata (from find_function).
        
        Returns
        -------
        bytes
            Raw instruction bytes for the function.
        
        Notes
        -----
        For Thumb functions, the LSB of the address is cleared before lookup
        (Thumb addresses have bit 0 set as a mode indicator, not part of the
        actual address).
        """
        if self.text_section is None:
            raise ValueError("No .text section available")
        
        # Thumb mode indicator: LSB of address = 1
        # Clear it to get the actual address
        actual_addr = func.address & ~1
        
        # Compute offset within .text section
        section_vma = self.text_section["sh_addr"]
        offset = actual_addr - section_vma
        
        if offset < 0 or offset + func.size > self.text_section.data_size:
            raise ValueError(
                f"Function {func.name} address 0x{func.address:08x} "
                f"is outside .text section bounds"
            )
        
        # Extract bytes
        code = self.text_section.data()[offset : offset + func.size]
        return code
    
    def get_source_location(self, address: int) -> Optional[SourceLocation]:
        """
        Map an instruction address to source file and line number.
        
        Parameters
        ----------
        address:
            Instruction address (VMA).
        
        Returns
        -------
        SourceLocation or None
            Source location if DWARF info is available, else None.
        """
        if self.dwarf_info is None:
            return None
        
        # Clear Thumb bit if present
        actual_addr = address & ~1
        
        # Search all compile units for the address
        for cu in self.dwarf_info.iter_CUs():
            lineprog = self.dwarf_info.line_program_for_CU(cu)
            if lineprog is None:
                continue
            
            # Iterate through line program entries
            prev_entry = None
            for entry in lineprog.get_entries():
                if entry.state is None:
                    continue
                
                # Check if address falls in this entry's range
                if prev_entry and prev_entry.state:
                    if prev_entry.state.address <= actual_addr < entry.state.address:
                        file_entry = lineprog["file_entry"][prev_entry.state.file - 1]
                        return SourceLocation(
                            address=actual_addr,
                            file=file_entry.name.decode("utf-8", errors="replace"),
                            line=prev_entry.state.line,
                            column=prev_entry.state.column,
                        )
                prev_entry = entry
        
        return None
    
    def list_functions(self) -> list[str]:
        """Return a list of all function names found in the binary."""
        return sorted(self.functions.keys())


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _decode_arm_arch_flags(flags: int) -> str:
    """
    Decode ARM ELF e_flags to determine architecture version.
    
    Reference: ARM IHI0044F "ELF for the ARM Architecture" §4.3.
    """
    # EF_ARM_EABIMASK = 0xFF000000
    # EF_ARM_EABI_VERSION(flags) = (flags & EF_ARM_EABIMASK)
    eabi_version = (flags >> 24) & 0xFF
    
    # Architecture version in bits 0-23 (implementation-defined)
    # For GCC-compiled binaries, this often encodes ARMv6-M, ARMv7-M, etc.
    # We use heuristics based on known flag patterns.
    
    if flags & 0x00000400:  # EF_ARM_BE8 (big-endian)
        endian = "BE8"
    else:
        endian = "LE"
    
    # Common patterns (not exhaustive):
    # ARMv6-M (Cortex-M0/M0+): typically eabi_version=5, flags=0x05000000
    # ARMv7-M (Cortex-M3/M4):  typically eabi_version=5, flags=0x05000000
    # ARMv8-M (Cortex-M33):    typically eabi_version=5, flags=0x05000000
    
    # The e_flags field doesn't reliably distinguish M0 from M4; we rely on
    # the user specifying --target at the CLI level instead.
    
    return f"ARM-EABI{eabi_version}-{endian}"


def _extract_functions(elffile: ELFFile) -> dict[str, ELFFunction]:
    """
    Extract function symbols from the ELF symbol table.
    
    Returns a dictionary mapping function name → ELFFunction.
    """
    functions: dict[str, ELFFunction] = {}
    
    # Search both .symtab and .dynsym (if present)
    for section in elffile.iter_sections():
        if not isinstance(section, SymbolTableSection):
            continue
        
        for symbol in section.iter_symbols():
            # Only include STT_FUNC (function) symbols
            if symbol["st_info"]["type"] != "STT_FUNC":
                continue
            
            # Skip symbols with zero size (usually external/undefined)
            size = symbol["st_size"]
            if size == 0:
                continue
            
            name = symbol.name
            address = symbol["st_value"]
            
            # Determine if Thumb mode (LSB = 1)
            is_thumb = (address & 1) == 1
            
            # Determine section name
            section_index = symbol["st_shndx"]
            if section_index == "SHN_UNDEF":
                continue  # Undefined symbol (external)
            
            try:
                func_section = elffile.get_section(section_index)
                section_name = func_section.name
            except Exception:  # noqa: BLE001
                section_name = "<unknown>"
            
            # Store function metadata
            functions[name] = ELFFunction(
                name=name,
                address=address,
                size=size,
                is_thumb=is_thumb,
                section_name=section_name,
            )
    
    return functions


def extract_critical_sections(
    elf: ELFBinary,
    disable_fn: str = "__disable_irq",
    enable_fn: str = "__enable_irq",
) -> list[tuple[int, int]]:
    """
    Scan the binary for interrupt-disabled regions by locating calls to
    *disable_fn* and *enable_fn*.
    
    This is a simplified heuristic that looks for BL (branch-with-link)
    instructions targeting the disable/enable function addresses.
    
    Parameters
    ----------
    elf:
        Parsed ELF binary.
    disable_fn:
        Name of the interrupt-disable function.
    enable_fn:
        Name of the interrupt-enable function.
    
    Returns
    -------
    list of (start_addr, end_addr)
        List of address ranges for critical sections.
    
    Notes
    -----
    This is a ROUGH heuristic. Production analysis should use:
    - Control-flow graph construction
    - Interprocedural analysis
    - Symbolic execution for path constraints
    
    For now, we rely on the C-level parser (parser.py) to identify regions,
    and use binary analysis only for cycle-exact costing of those regions.
    """
    # Locate disable and enable functions
    disable_func = elf.find_function(disable_fn)
    enable_func = elf.find_function(enable_fn)
    
    if not disable_func or not enable_func:
        logger.warning(
            f"Could not find {disable_fn} or {enable_fn} in binary. "
            f"Critical section detection skipped."
        )
        return []
    
    # For production: disassemble .text, scan for BL to disable_func.address,
    # then find matching BL to enable_func.address.
    # This requires full CFG construction (see binary_wcet.py).
    
    # Placeholder: return empty list (actual implementation in binary_wcet.py)
    logger.debug(
        f"Found {disable_fn} at 0x{disable_func.address:08x}, "
        f"{enable_fn} at 0x{enable_func.address:08x}"
    )
    return []


# ---------------------------------------------------------------------------
# Command-line interface (for testing)
# ---------------------------------------------------------------------------

def main() -> None:
    """Simple CLI to inspect an ELF file (for development/testing)."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python -m irq_verify.elf_parser <file.elf>")
        sys.exit(1)
    
    logging.basicConfig(level=logging.DEBUG)
    
    path = Path(sys.argv[1])
    elf = ELFBinary.from_file(path)
    
    print(f"ELF Binary: {path}")
    print(f"Architecture: {elf.arch}")
    print(f"Endianness: {'little' if elf.is_little_endian else 'big'}")
    print(f"Functions found: {len(elf.functions)}")
    print()
    
    # List all functions
    print("Functions:")
    for name in elf.list_functions():
        func = elf.functions[name]
        mode = "Thumb" if func.is_thumb else "ARM"
        print(f"  {name:30s} @ 0x{func.address:08x} ({func.size:4d} bytes, {mode})")
    
    print()
    
    # Try to map a few addresses to source lines
    if elf.dwarf_info:
        print("Source line mapping (first 5 functions):")
        for name in list(elf.list_functions())[:5]:
            func = elf.functions[name]
            loc = elf.get_source_location(func.address)
            if loc:
                print(f"  {name}: {loc.file}:{loc.line}")
            else:
                print(f"  {name}: <no debug info>")
    else:
        print("No DWARF debug info available.")


if __name__ == "__main__":
    main()
