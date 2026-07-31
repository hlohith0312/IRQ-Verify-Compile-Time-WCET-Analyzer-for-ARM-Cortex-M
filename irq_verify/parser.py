"""
parser.py — Parse a C source file into a pycparser AST and detect
interrupt-disabled regions.

A "region" is the sequence of AST nodes found between a call to
*disable_fn* (e.g. ``__disable_irq()``) and the next matching call
to *enable_fn* (e.g. ``__enable_irq()``).

Preprocessing pipeline
----------------------
When *use_preprocessor=True* (the default) and ``pcpp`` is installed,
the file is first run through the pure-Python C preprocessor pcpp so
that ``#include``, ``#define``, and ``#ifdef`` directives are fully
expanded before pycparser sees the source.  This lets the tool analyse
any real firmware file without requiring gcc or clang.

Fallback (use_preprocessor=False or pcpp not installed):
A fast regex-based pass strips directives and injects a minimal fake
preamble.  Suitable for simple test fixtures or files with no headers.

LIMITATIONS
-----------
* Only top-level function bodies are walked.
* Inline assembly is detected and flagged UNANALYZABLE.
* Vendor SDK macros that alter control-flow (e.g. NVIC macros that
  expand to loops) may produce unexpected results — use
  ``// @irq_loop_bound(N)`` annotations to assert known bounds.
"""

from __future__ import annotations

import io
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import pycparser
    from pycparser import c_ast, parse_file as _pycparser_parse_file
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "pycparser is required.  Install it with: pip install pycparser"
    ) from exc

# ---------------------------------------------------------------------------
# Preprocessor constants
# ---------------------------------------------------------------------------

# Directory containing our bundled minimal fake C headers.
# These provide uint32_t, size_t, bool, etc. so that common embedded headers
# resolve without needing the full system include path.
_FAKE_LIBC_DIR: Path = Path(__file__).parent / "fake_libc"

# GCC/Clang extension keywords that pycparser cannot parse.
# We define them away so pcpp strips them from the expanded source.
_GCC_FAKE_DEFINES: list[str] = [
    "__attribute__(x)=",        # GCC attributes → stripped
    "__extension__=",            # GCC extension keyword
    "__inline=inline",
    "__inline__=inline",
    "__volatile__=volatile",
    "__restrict=",
    "__restrict__=",
    "__builtin_va_list=int",
    "__GNUC__=4",
    "__GNUC_MINOR__=9",
    "__GNUC_PATCHLEVEL__=0",
    "__STDC__=1",
    "__STDC_VERSION__=201112L",
    "__STDC_HOSTED__=1",
    # ARM CMSIS attributes
    "__STATIC_INLINE=static inline",
    "__STATIC_FORCEINLINE=static inline",
    "__WEAK=",
    "__PACKED=",
    "__ALIGNED(x)=",
    "__USED=",
    "__UNUSED=",
    # IAR / ARMCC / Keil compat shims
    "__ramfunc=",
    "__irq=",
    "__packed=",
]



# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class IrqRegion:
    """A single interrupt-disabled critical section found in the source."""

    disable_line: int
    """Line number of the __disable_irq() call."""

    enable_line: int
    """Line number of the __enable_irq() call."""

    stmts: list[Any]
    """AST statement nodes between the disable and enable calls (exclusive)."""

    budget: int | None = None
    """Per-region budget from a ``// @irq_budget(N)`` annotation, or None."""

    source_lines: dict[int, str] = field(default_factory=dict)
    """Mapping of line numbers to source text lines (for reporting)."""

    containing_function: str | None = None
    """Name of the function that contains this region (for reporting)."""

    func_defs: dict[str, Any] = field(default_factory=dict)
    """All FuncDef nodes visible in the file (for call inlining)."""
    
    line_offset: int = 0
    """Offset between AST coord line numbers and original source line numbers."""
    
    line_map: dict[int, tuple[str, int]] | None = None
    """Line map from pcpp preprocessing (preprocessed_line → (file, original_line))."""


# ---------------------------------------------------------------------------
# Annotation parser
# ---------------------------------------------------------------------------

_BUDGET_RE = re.compile(r"//\s*@irq_budget\s*\(\s*(\d+)\s*\)", re.ASCII)
_LOOP_BOUND_RE = re.compile(r"//\s*@irq_loop_bound\s*\(\s*(\d+)\s*\)", re.ASCII)


def _extract_budget_annotation(source: str, line: int, line_offset: int = 0, search_range: int = 15) -> int | None:
    """Return the budget from a ``// @irq_budget(N)`` comment near *line*.

    *line* is the line number reported by pycparser (1-indexed, relative to the
    preprocessed source which has *line_offset* extra lines prepended).
    We subtract *line_offset* to get back to the original source line number.
    
    *search_range*: how many lines to search both before and after (default: 15).
    This accounts for preprocessor comment stripping shifting line numbers.
    """
    lines = source.splitlines()
    # line from AST is 1-indexed in preprocessed source; subtract preamble offset
    original_line = line - line_offset
    
    # Search both backwards AND forwards to handle comment-stripping offsets
    for offset in range(-search_range, search_range + 1):
        check_line = original_line + offset - 1  # -1 for 1-indexed to 0-indexed
        if check_line < 0 or check_line >= len(lines):
            continue
        m = _BUDGET_RE.search(lines[check_line])
        if m:
            return int(m.group(1))
    
    return None


def _extract_loop_bound_annotation(source: str, line: int, line_offset: int = 0, search_range: int = 5) -> int | None:
    """Return the bound from a ``// @irq_loop_bound(N)`` comment near *line*.

    *line* is the line number reported by pycparser (1-indexed, relative to the
    preprocessed source which has *line_offset* extra lines prepended).
    We subtract *line_offset* to get back to the original source line number.
    
    *search_range*: how many lines before *line* to search (default: 5).
    This accounts for pycparser sometimes reporting the end of a block rather
    than the loop keyword line.
    """
    lines = source.splitlines()
    # line from AST is 1-indexed in preprocessed source; subtract preamble offset
    original_line = line - line_offset
    
    # Search backwards from the reported line to find the annotation
    for offset in range(search_range + 1):
        check_line = original_line - offset - 1  # -1 for 1-indexed to 0-indexed, then -offset more
        if check_line < 0 or check_line >= len(lines):
            continue
        m = _LOOP_BOUND_RE.search(lines[check_line])
        if m:
            return int(m.group(1))
    
    return None


# ---------------------------------------------------------------------------
# pycparser helpers
# ---------------------------------------------------------------------------

# Fake typedefs injected so pycparser can handle common embedded types without
# a full preprocessor pass.  This is a well-known workaround for pycparser.
# _FAKE_LIBC_HEADER: injected before the user's source so pycparser can parse
# common embedded typedefs without a real preprocessor pass.
# __irq_verify_asm_block__ is a sentinel: the preprocessor substitutes it for
# inline-asm blocks so the analyser can detect them and report UNANALYZABLE.
_FAKE_LIBC_HEADER = textwrap.dedent("""\
    typedef unsigned char uint8_t;
    typedef unsigned short uint16_t;
    typedef unsigned int uint32_t;
    typedef unsigned long long uint64_t;
    typedef signed char int8_t;
    typedef signed short int16_t;
    typedef signed int int32_t;
    typedef signed long long int64_t;
    typedef unsigned int size_t;
    typedef int ptrdiff_t;
    typedef unsigned int uintptr_t;
    typedef int bool;
    void __disable_irq(void);
    void __enable_irq(void);
    void __irq_verify_asm_block__(void);
""")


def _strip_comments(source: str) -> str:
    """
    Remove C-style (/* ... */) and C++-style (// ...) comments from *source*.

    This is required because pycparser v3.0+ with use_cpp=False does not accept
    comments in the input.

    We preserve newlines so that line numbers in the AST still correspond to the
    original source file.  (We extract @irq_budget annotations from the original
    source text *before* stripping, so comment content is not lost for that purpose.)
    """
    result: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        # Block comment
        if source[i:i+2] == '/*':
            j = source.find('*/', i + 2)
            if j == -1:
                # Unterminated comment — skip to end
                newlines = source[i:].count('\n')
                result.append('\n' * newlines)
                break
            # Preserve newlines
            span = source[i:j+2]
            result.append('\n' * span.count('\n'))
            i = j + 2
        # Line comment
        elif source[i:i+2] == '//':
            j = source.find('\n', i)
            if j == -1:
                break
            result.append('\n')  # preserve the newline
            i = j + 1
        # String literal — skip so we don't accidentally strip "//" inside strings
        elif source[i] == '"':
            j = i + 1
            while j < n:
                if source[j] == '\\':
                    j += 2
                elif source[j] == '"':
                    j += 1
                    break
                else:
                    j += 1
            result.append(source[i:j])
            i = j
        # Character literal
        elif source[i] == "'":
            j = i + 1
            while j < n:
                if source[j] == '\\':
                    j += 2
                elif source[j] == "'":
                    j += 1
                    break
                else:
                    j += 1
            result.append(source[i:j])
            i = j
        else:
            result.append(source[i])
            i += 1
    return ''.join(result)


# ---------------------------------------------------------------------------
# Line-number map helpers (used with pcpp preprocessing)
# ---------------------------------------------------------------------------

# Maps preprocessed-output line number → (source file path, original line number)
_LineMap = dict[int, tuple[str, int]]


def _build_line_map(preprocessed: str, default_file: str) -> _LineMap:
    """
    Parse ``#line N "file"`` directives emitted by pcpp and build a mapping
    from preprocessed-output line index to (original_file, original_line_number).

    Lines that are themselves #line directives are NOT counted in the output
    index — they are consumed to update the current tracking state.
    """
    line_map: _LineMap = {}
    current_file = default_file
    current_orig = 1
    out_idx = 0
    for raw in preprocessed.splitlines():
        m = re.match(r'^#\s*(?:line\s+)?(\d+)(?:\s+"([^"]*)")?' , raw)
        if m:
            current_orig = int(m.group(1))
            if m.group(2):
                current_file = m.group(2)
            # Do not increment out_idx — this line does not appear in output
            continue
        out_idx += 1
        line_map[out_idx] = (current_file, current_orig)
        current_orig += 1
    return line_map


def _strip_line_directives(text: str) -> str:
    """Remove ``#line`` / ``# N`` directives from preprocessor output."""
    kept = [
        ln for ln in text.splitlines()
        if not re.match(r'^#\s*(?:line\s+)?\d+', ln)
    ]
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# pcpp-based preprocessor
# ---------------------------------------------------------------------------


def _preprocess_with_pcpp(
    source: str,
    path: Path,
    include_dirs: list[Path] | None,
    disable_fn: str,
    enable_fn: str,
) -> tuple[str, _LineMap]:
    """
    Expand macros and ``#include`` directives using pcpp (pure Python).

    Returns
    -------
    clean_source:
        Preprocessed C source with ``#line`` directives stripped,
        ready for pycparser.
    line_map:
        Mapping from clean_source line index → (original_file, original_line).
    """
    try:
        import pcpp  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "pcpp is required for preprocessing real firmware files.\n"
            "Install it with: pip install pcpp\n"
            "Or use --no-preprocess for simple files without #include."
        ) from exc

    pp = pcpp.Preprocessor()

    # Emit #line directives so we can map back to original line numbers
    pp.line_directive = "#line"

    # Silence pcpp's own error output — let pycparser produce the real error
    pp.on_error = lambda *_: None  # type: ignore[attr-defined]

    # Define away GCC / ARM CMSIS extensions that pycparser can't handle
    for defn in _GCC_FAKE_DEFINES:
        pp.define(defn)

    # Our bundled fake libc headers (uint32_t, size_t, bool, …)
    if _FAKE_LIBC_DIR.is_dir():
        pp.add_path(str(_FAKE_LIBC_DIR))

    # Directory of the source file itself (for `#include "myheader.h"`)
    pp.add_path(str(path.parent))

    # User-supplied include directories
    for d in (include_dirs or []):
        pp.add_path(str(d))


    # For the pcpp path we use a MINIMAL preamble containing ONLY the
    # sentinel function declarations.  Typedefs (uint32_t etc.) are NOT
    # included here because they will be provided by pcpp expanding the
    # user's own `#include <stdint.h>` via our bundled fake_libc headers.
    # Including them here too would create duplicate-typedef parse errors.
    #
    # Files without `#include <stdint.h>` that use uint32_t are invalid C
    # and will correctly get a parse error.
    preamble_lines_list = [
        "void __disable_irq(void);",
        "void __enable_irq(void);",
        "void __irq_verify_asm_block__(void);",
    ]
    if disable_fn not in ("__disable_irq",):
        preamble_lines_list.append(f"void {disable_fn}(void);")
    if enable_fn not in ("__enable_irq",):
        preamble_lines_list.append(f"void {enable_fn}(void);")
    preamble = "\n".join(preamble_lines_list)

    # Strip C comments from the user source BEFORE handing it to pcpp.
    # pcpp is a standard-conforming preprocessor: it replaces an entire
    # /* ... */ block comment with a single space character, which collapses
    # multi-line comments and shifts all subsequent line numbers.
    # Our _strip_comments() replaces each comment line with a blank line,
    # preserving the original line count so AST coordinates remain correct.
    # Budget annotations (@irq_budget) are extracted from self.source
    # (the unmodified original), so stripping here is safe.
    #
    # UPDATE: Actually, DON'T strip comments here. pcpp needs to see them
    # to properly track line numbers. pcpp will handle comment removal itself
    # and emit correct #line directives.
    # source_for_pp = _strip_comments(source)
    source_for_pp = source

    # Prepend the preamble so it is always parsed even if the user file
    # doesn't include stdint.h etc.
    full_source = preamble + "\n" + source_for_pp

    pp.parse(full_source, str(path))


    buf = io.StringIO()
    pp.write(buf)
    expanded = buf.getvalue()

    # Build the line map BEFORE stripping directives
    line_map = _build_line_map(expanded, str(path))

    # Strip #line directives (pycparser with use_cpp=False can't handle them)
    clean = _strip_line_directives(expanded)

    # Post-process: handle __asm__ volatile patterns that pcpp's object-like
    # #define can't match (because `volatile` appears between the name and `(`).
    clean = re.sub(
        r'(?:__asm__|asm)\s*(?:volatile\s*)?\s*\([^;]*\)\s*;',
        '__irq_verify_asm_block__();',
        clean,
        flags=re.DOTALL,
    )

    return clean, line_map


# ---------------------------------------------------------------------------
# Legacy (no-preprocessor) path — fast, no external deps
# ---------------------------------------------------------------------------


def _preprocess_source(source: str, disable_fn: str, enable_fn: str) -> str:
    """
    Minimal source preparation so pycparser can handle common embedded idioms
    WITHOUT running a real preprocessor.

    Steps:
    1. Strip ALL preprocessor directives (#include, #define, …).
    2. Strip C and C++ comments.
    3. Strip ``__attribute__((...))`` qualifiers.
    4. Replace ``__asm__`` / ``asm`` blocks with an UNANALYZABLE sentinel.
    5. Inject fake typedefs and disable/enable declarations as a preamble.

    KNOWN LIMITATION: macro-defined constants (e.g. #define N 100) are stripped
    so loop bounds that use them appear as UNANALYZABLE identifiers.
    Use --include-dir or literal constants inside critical sections instead.
    """
    # Remove ALL preprocessor directive lines
    source = re.sub(r'^\s*#[^\n]*$', '', source, flags=re.MULTILINE)

    # Strip all C/C++ comments (pycparser v3.0+ rejects them)
    source = _strip_comments(source)

    # Remove __attribute__((...)) — handles nested parens up to 3 levels
    source = re.sub(
        r'__attribute__\s*\(\s*\([^)]*(?:\([^)]*\)[^)]*)*\)\s*\)', '', source
    )

    # Replace inline asm blocks with a sentinel call
    source = re.sub(
        r'(?:__asm__|asm)\s*(?:volatile\s*)?\s*\([^;]*\)\s*;',
        '__irq_verify_asm_block__();',
        source,
        flags=re.DOTALL,
    )

    # Build fake header preamble
    preamble = _FAKE_LIBC_HEADER
    if disable_fn not in ("__disable_irq",):
        preamble += f"void {disable_fn}(void);\n"
    if enable_fn not in ("__enable_irq",):
        preamble += f"void {enable_fn}(void);\n"

    return preamble + "\n" + source



# ---------------------------------------------------------------------------
# AST walking helpers
# ---------------------------------------------------------------------------


def _is_call(node: Any, fn_name: str) -> bool:
    """Return True if *node* is a function call to *fn_name*."""
    if not isinstance(node, c_ast.FuncCall):
        return False
    name_node = node.name
    if isinstance(name_node, c_ast.ID):
        return bool(name_node.name == fn_name)  # pycparser ID.name is Any; cast to bool
    return False


def _is_stmt_call(node: Any, fn_name: str) -> bool:
    """Return True if *node* is an ExprList/Decl wrapping a call to *fn_name*."""
    if isinstance(node, c_ast.Decl):
        return False
    if isinstance(node, c_ast.FuncCall):
        return _is_call(node, fn_name)
    return False


def _stmt_is_call(stmt: Any, fn_name: str) -> bool:
    """Return True if the statement is (or wraps) a bare call to *fn_name*."""
    if isinstance(stmt, c_ast.FuncCall):
        return _is_call(stmt, fn_name)
    return False


def _collect_func_defs(ast: c_ast.FileAST) -> dict[str, c_ast.FuncDef]:
    """Return a mapping of function name → FuncDef for all functions in the AST."""
    defs: dict[str, c_ast.FuncDef] = {}
    for node in ast.ext:
        if isinstance(node, c_ast.FuncDef):
            name = node.decl.name
            defs[name] = node
    return defs


# ---------------------------------------------------------------------------
# Region extractor
# ---------------------------------------------------------------------------


class _RegionExtractor(c_ast.NodeVisitor):  # type: ignore[misc]  # pycparser NodeVisitor is Any
    """Walk function bodies and extract interrupt-disabled regions."""

    def __init__(
        self,
        disable_fn: str,
        enable_fn: str,
        source: str,
        func_defs: dict[str, Any],
        line_offset: int = 0,
    ) -> None:
        self.disable_fn = disable_fn
        self.enable_fn = enable_fn
        self.source = source
        self.source_lines = {i + 1: line for i, line in enumerate(source.splitlines())}
        self.func_defs = func_defs
        self.line_offset = line_offset
        # line_map from pcpp: preprocessed-line → (file, original-line)
        # When set, takes priority over line_offset for coordinate translation.
        self.line_map: _LineMap | None = None
        self.source_path: str = ""
        self.regions: list[IrqRegion] = []

    def visit_FuncDef(self, node: c_ast.FuncDef) -> None:  # noqa: N802
        func_name = node.decl.name
        body = node.body
        if body is None or not isinstance(body, c_ast.Compound):
            return
        self._scan_compound(body, func_name)

    def _scan_compound(self, compound: c_ast.Compound, func_name: str) -> None:
        """Scan a Compound node for disable/enable pairs."""
        if compound.block_items is None:
            return
        stmts = compound.block_items
        i = 0
        while i < len(stmts):
            stmt = stmts[i]
            if _stmt_is_call(stmt, self.disable_fn):
                disable_line = stmt.coord.line if stmt.coord else 0
                # Collect everything up to (but not including) the next enable call
                region_stmts: list[Any] = []
                j = i + 1
                while j < len(stmts):
                    if _stmt_is_call(stmts[j], self.enable_fn):
                        break
                    region_stmts.append(stmts[j])
                    j += 1

                enable_line = 0
                if j < len(stmts):
                    enable_line = stmts[j].coord.line if stmts[j].coord else 0

                # Translate AST line numbers back to original source line numbers.
                #
                # The AST coords are 1-indexed line numbers in the preprocessed source.
                # The preprocessed source has a preamble prepended, so we subtract
                # preamble_lines to get the original source line number.
                #
                # For pcpp path: Even though pcpp emits #line directives, they don't
                # correctly track comments, so we use simple arithmetic instead.
                orig_disable_line = max(1, disable_line - self.line_offset)
                orig_enable_line = (
                    max(1, enable_line - self.line_offset) if enable_line else 0
                )

                # _extract_budget_annotation scans the ORIGINAL source.
                # Pass the already-translated orig_disable_line with offset=0.
                budget = _extract_budget_annotation(
                    self.source, orig_disable_line, 0
                )

                region = IrqRegion(
                    disable_line=orig_disable_line,
                    enable_line=orig_enable_line,
                    stmts=region_stmts,
                    budget=budget,
                    source_lines=self.source_lines,
                    containing_function=func_name,
                    func_defs=self.func_defs,
                    line_offset=self.line_offset,
                    line_map=self.line_map,
                )
                self.regions.append(region)
                i = j + 1  # skip past the enable call
            else:
                i += 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_file(
    path: Path,
    disable_fn: str = "__disable_irq",
    enable_fn: str = "__enable_irq",
    include_dirs: list[Path] | None = None,
    use_preprocessor: bool = True,
) -> tuple[c_ast.FileAST, list[IrqRegion]]:
    """
    Parse *path* and return ``(ast, regions)``.

    Parameters
    ----------
    path:
        Path to the C source file.
    disable_fn:
        Name of the function call that begins a critical section.
    enable_fn:
        Name of the function call that ends a critical section.
    include_dirs:
        Additional directories to search for ``#include`` files.
        Passed to pcpp as ``-I`` flags.  Ignored when
        *use_preprocessor* is False.
    use_preprocessor:
        When True (default), run pcpp to fully expand macros and
        ``#include`` directives before parsing.  When False, use the
        fast regex-based legacy path (suitable for test fixtures).

    Returns
    -------
    ast:
        The full pycparser AST of the file.
    regions:
        A list of :class:`IrqRegion` objects, one per detected critical section.
    """
    import tempfile
    import os

    source = path.read_text(encoding="utf-8", errors="replace")

    line_map: _LineMap | None = None

    # ------------------------------------------------------------------
    # Choose preprocessing path
    # ------------------------------------------------------------------
    _pcpp_available = False
    try:
        import pcpp as _pcpp_mod  # type: ignore[import-untyped]  # noqa: F401
        _pcpp_available = True
    except ImportError:
        pass

    # preamble_lines is computed AFTER choosing the path because the two paths
    # inject different amounts of preamble text.
    preamble_lines: int

    if use_preprocessor and _pcpp_available:
        # Full preprocessing: expands #include and #define via pcpp.
        preprocessed, line_map = _preprocess_with_pcpp(
            source, path, include_dirs, disable_fn, enable_fn
        )
        # For the pcpp path, we still need preamble_lines for the budget annotation
        # extraction, since annotations are in the original source but AST coords
        # are from the preprocessed source.
        # The preamble in _preprocess_with_pcpp is 3 function declarations.
        preamble_lines = 3

    else:
        # Legacy regex-based path: strips directives, injects fake preamble.
        preprocessed = _preprocess_source(source, disable_fn, enable_fn)

    # ------------------------------------------------------------------
    # Parse with pycparser (needs a file, not a string)
    # ------------------------------------------------------------------
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".c", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(preprocessed)
        tmp_path = tmp.name

    try:
        ast = pycparser.parse_file(tmp_path, use_cpp=False)
    finally:
        os.unlink(tmp_path)

    func_defs = _collect_func_defs(ast)

    extractor = _RegionExtractor(
        disable_fn, enable_fn, source, func_defs, line_offset=preamble_lines
    )
    extractor.line_map = line_map
    extractor.source_path = str(path)
    extractor.visit(ast)

    return ast, extractor.regions


def parse_project(
    files: list[Path],
    disable_fn: str = "__disable_irq",
    enable_fn: str = "__enable_irq",
    include_dirs: list[Path] | None = None,
    use_preprocessor: bool = True,
) -> tuple[dict[str, Any], list[tuple[Path, list[IrqRegion]]]]:
    """
    Parse multiple C files as a single project.

    All function definitions from all files are merged into one shared
    ``func_defs`` dictionary so that cross-file call resolution works.
    Each file's regions are returned with the file path so the reporter
    can group them.

    Returns
    -------
    func_defs:
        Merged mapping of function name → FuncDef from ALL files.
    file_regions:
        List of ``(file_path, regions)`` pairs, one per input file.
    """
    # Pass 1: parse all files and collect function definitions
    per_file_data: list[tuple[Path, c_ast.FileAST, list[IrqRegion]]] = []
    merged_func_defs: dict[str, Any] = {}

    for file_path in files:
        ast, regions = parse_file(
            file_path,
            disable_fn=disable_fn,
            enable_fn=enable_fn,
            include_dirs=include_dirs,
            use_preprocessor=use_preprocessor,
        )
        file_func_defs = _collect_func_defs(ast)
        merged_func_defs.update(file_func_defs)
        per_file_data.append((file_path, ast, regions))

    # Pass 2: re-attach the merged func_defs to all regions so cross-file
    # calls (defined in a different translation unit) can be inlined.
    file_regions: list[tuple[Path, list[IrqRegion]]] = []
    for file_path, _ast, regions in per_file_data:
        for region in regions:
            region.func_defs = merged_func_defs
        file_regions.append((file_path, regions))

    return merged_func_defs, file_regions
