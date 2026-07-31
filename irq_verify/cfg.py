"""
cfg.py — Control-flow graph construction for interrupt-disabled regions.

Each node in the CFG is a :class:`BasicBlock` — a maximal sequence of
statements with no branches.  Edges connect blocks as:

  * sequential blocks (unconditional flow)
  * if/else true-branch and false-branch edges
  * loop back-edges (for/while/do-while)
  * function-call "inline" edges (if the callee is defined in the file)

LIMITATIONS
-----------
* ``switch``/``case`` statements are not yet supported and will raise
  ``UnsupportedConstruct``.
* ``goto`` / ``setjmp`` are not supported.
* Only single-level ``break`` / ``continue`` are handled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from pycparser import c_ast
except ImportError as exc:  # pragma: no cover
    raise ImportError("pycparser is required: pip install pycparser") from exc


class UnsupportedConstruct(Exception):
    """Raised when the CFG builder encounters a construct it cannot model."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BasicBlock:
    """A maximal sequence of straight-line statements."""

    id: int
    stmts: list[Any] = field(default_factory=list)
    successors: list["BasicBlock"] = field(default_factory=list)

    # Metadata used during cycle-cost analysis
    is_loop_header: bool = False
    loop_bound: Optional[int] = None       # None → unbounded
    loop_bound_reason: Optional[str] = None  # human-readable explanation
    is_loop_back_edge_target: bool = False


class CFG:
    """A control-flow graph for a sequence of statements."""

    def __init__(self) -> None:
        self._counter = 0
        self.entry: Optional[BasicBlock] = None
        self.exit: BasicBlock = self._new_block()   # synthetic exit block

    def _new_block(self) -> BasicBlock:
        blk = BasicBlock(id=self._counter)
        self._counter += 1
        return blk

    def all_blocks(self) -> list[BasicBlock]:
        """Return all reachable blocks in BFS order."""
        visited: set[int] = set()
        queue = [self.entry] if self.entry else []
        result: list[BasicBlock] = []
        while queue:
            blk = queue.pop(0)
            if blk is None or blk.id in visited:
                continue
            visited.add(blk.id)
            result.append(blk)
            queue.extend(blk.successors)
        # Always include exit block
        if self.exit.id not in visited:
            result.append(self.exit)
        return result


# ---------------------------------------------------------------------------
# CFG builder
# ---------------------------------------------------------------------------


class CFGBuilder:
    """Build a CFG from a list of AST statement nodes."""

    def __init__(self, func_defs: dict[str, Any]) -> None:
        self.func_defs = func_defs
        self._cfg = CFG()
        self._counter = [0]

    def _new_block(self) -> BasicBlock:
        blk = BasicBlock(id=self._counter[0])
        self._counter[0] += 1
        return blk

    def build(self, stmts: list[Any]) -> CFG:
        """Build and return a CFG for *stmts*."""
        entry = self._new_block()
        self._cfg.entry = entry
        exit_blk = self._cfg.exit

        last = self._process_stmts(stmts, entry, exit_blk)
        if last is not exit_blk:
            last.successors.append(exit_blk)

        return self._cfg

    def _process_stmts(
        self,
        stmts: list[Any],
        current: BasicBlock,
        exit_blk: BasicBlock,
    ) -> BasicBlock:
        """
        Append *stmts* into the CFG starting at *current*.
        Returns the last block used (which the caller should link to the next
        block or exit).
        """
        for stmt in stmts:
            current = self._process_stmt(stmt, current, exit_blk)
        return current

    def _process_stmt(  # noqa: C901
        self,
        stmt: Any,
        current: BasicBlock,
        exit_blk: BasicBlock,
    ) -> BasicBlock:
        """Process a single AST statement, updating the CFG.  Returns the
        'current' block after processing (i.e. where the next statement goes)."""

        if stmt is None:
            return current

        # ------------------------------------------------------------------ #
        # Compound block  { ... }                                              #
        # ------------------------------------------------------------------ #
        if isinstance(stmt, c_ast.Compound):
            items = stmt.block_items or []
            return self._process_stmts(items, current, exit_blk)

        # ------------------------------------------------------------------ #
        # If / Else                                                             #
        # ------------------------------------------------------------------ #
        if isinstance(stmt, c_ast.If):
            # Add the condition evaluation to current block
            current.stmts.append(stmt.cond)
            # True branch
            true_entry = self._new_block()
            current.successors.append(true_entry)
            true_exit = self._process_stmt(stmt.iftrue, true_entry, exit_blk)

            # False branch (may be absent)
            join = self._new_block()

            if stmt.iffalse is not None:
                false_entry = self._new_block()
                current.successors.append(false_entry)
                false_exit = self._process_stmt(stmt.iffalse, false_entry, exit_blk)
                false_exit.successors.append(join)
            else:
                # No else → false path goes directly to join
                current.successors.append(join)

            true_exit.successors.append(join)
            return join

        # ------------------------------------------------------------------ #
        # For loop                                                              #
        # ------------------------------------------------------------------ #
        if isinstance(stmt, c_ast.For):
            return self._process_for(stmt, current, exit_blk)

        # ------------------------------------------------------------------ #
        # While loop                                                            #
        # ------------------------------------------------------------------ #
        if isinstance(stmt, c_ast.While):
            return self._process_while(stmt, current, exit_blk)

        # ------------------------------------------------------------------ #
        # DoWhile loop                                                          #
        # ------------------------------------------------------------------ #
        if isinstance(stmt, c_ast.DoWhile):
            return self._process_dowhile(stmt, current, exit_blk)

        # ------------------------------------------------------------------ #
        # Switch — not supported                                                #
        # ------------------------------------------------------------------ #
        if isinstance(stmt, c_ast.Switch):
            raise UnsupportedConstruct(
                f"switch statements are not supported (line "
                f"{stmt.coord.line if stmt.coord else '?'})"
            )

        # ------------------------------------------------------------------ #
        # Return — flow goes to exit                                            #
        # ------------------------------------------------------------------ #
        if isinstance(stmt, c_ast.Return):
            current.stmts.append(stmt)
            current.successors.append(exit_blk)
            # After a return, subsequent code is unreachable; return a dead block
            dead = self._new_block()
            return dead

        # ------------------------------------------------------------------ #
        # Default: treat as a simple statement in the current block            #
        # ------------------------------------------------------------------ #
        current.stmts.append(stmt)
        return current

    # ---------------------------------------------------------------------- #
    # Loop helpers                                                             #
    # ---------------------------------------------------------------------- #

    def _extract_for_bound(self, stmt: c_ast.For) -> tuple[int | None, str]:
        """
        Try to extract a constant upper bound from a for loop.

        Returns (bound, reason) where bound is None if not statically determinable.
        """
        # Heuristic: handle the canonical  for (i = 0; i < N; i++)  pattern
        # where N is an integer constant.
        cond = stmt.cond
        if cond is None:
            return None, "no loop condition (infinite loop)"

        # Condition must be a BinaryOp with a constant RHS
        if not isinstance(cond, c_ast.BinaryOp):
            return None, f"loop condition is not a simple comparison: {type(cond).__name__}"

        op = cond.op
        if op not in ("<", "<=", "!="):
            return None, f"unsupported loop condition operator '{op}'"

        rhs = cond.right
        if isinstance(rhs, c_ast.Constant):
            try:
                n = int(rhs.value, 0)
                if op == "<=":
                    n += 1  # for (i=0; i<=N; i++) runs N+1 times
                return n, f"static bound: {n} iterations"
            except ValueError:
                return None, f"constant parse failed: {rhs.value!r}"

        # RHS is not a literal constant → runtime-dependent
        rhs_str = _node_to_str(rhs)
        return None, f"loop bound depends on runtime variable: {rhs_str}"

    def _process_for(
        self,
        stmt: c_ast.For,
        current: BasicBlock,
        exit_blk: BasicBlock,
    ) -> BasicBlock:
        # Init goes into current block
        if stmt.init is not None:
            current.stmts.append(stmt.init)

        # Loop header block (condition)
        header = self._new_block()
        header.is_loop_header = True
        bound, reason = self._extract_for_bound(stmt)
        header.loop_bound = bound
        header.loop_bound_reason = reason
        current.successors.append(header)

        if stmt.cond is not None:
            header.stmts.append(stmt.cond)

        # Body block
        body_entry = self._new_block()
        header.successors.append(body_entry)  # taken (loop body)

        after_loop = self._new_block()
        header.successors.append(after_loop)  # not taken (loop exits)

        body_exit = self._process_stmt(stmt.stmt or c_ast.Compound(None, None), body_entry, exit_blk)

        # Next (increment)
        if stmt.next is not None:
            body_exit.stmts.append(stmt.next)

        # Back edge to header
        body_exit.successors.append(header)

        return after_loop

    def _process_while(
        self,
        stmt: c_ast.While,
        current: BasicBlock,
        exit_blk: BasicBlock,
    ) -> BasicBlock:
        header = self._new_block()
        header.is_loop_header = True
        header.loop_bound = None
        header.loop_bound_reason = "while loop — bound not statically determinable"
        current.successors.append(header)

        if stmt.cond is not None:
            header.stmts.append(stmt.cond)

        body_entry = self._new_block()
        header.successors.append(body_entry)

        after_loop = self._new_block()
        header.successors.append(after_loop)

        body_exit = self._process_stmt(stmt.stmt or c_ast.Compound(None, None), body_entry, exit_blk)
        body_exit.successors.append(header)

        return after_loop

    def _process_dowhile(
        self,
        stmt: c_ast.DoWhile,
        current: BasicBlock,
        exit_blk: BasicBlock,
    ) -> BasicBlock:
        body_entry = self._new_block()
        body_entry.is_loop_header = True
        body_entry.loop_bound = None
        body_entry.loop_bound_reason = "do-while loop — bound not statically determinable"
        current.successors.append(body_entry)

        body_exit = self._process_stmt(stmt.stmt or c_ast.Compound(None, None), body_entry, exit_blk)

        # Condition at the bottom
        if stmt.cond is not None:
            body_exit.stmts.append(stmt.cond)

        after_loop = self._new_block()
        body_exit.successors.append(body_entry)   # back edge
        body_exit.successors.append(after_loop)   # exit edge

        return after_loop


def build_cfg(stmts: list[Any], func_defs: dict[str, Any]) -> CFG:
    """Build a CFG for *stmts*, using *func_defs* for call inlining."""
    builder = CFGBuilder(func_defs)
    return builder.build(stmts)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _node_to_str(node: Any) -> str:
    """Best-effort conversion of a pycparser node to a short string."""
    if isinstance(node, c_ast.ID):
        return str(node.name)
    if isinstance(node, c_ast.Constant):
        return str(node.value)
    if isinstance(node, c_ast.BinaryOp):
        return f"({_node_to_str(node.left)} {node.op} {_node_to_str(node.right)})"
    if isinstance(node, c_ast.UnaryOp):
        return f"({node.op}{_node_to_str(node.expr)})"
    if isinstance(node, c_ast.ArrayRef):
        return f"{_node_to_str(node.name)}[{_node_to_str(node.subscript)}]"
    if isinstance(node, c_ast.StructRef):
        return f"{_node_to_str(node.name)}{node.type}{_node_to_str(node.field)}"
    if hasattr(node, "__class__"):
        return f"<{node.__class__.__name__}>"
    return "<?>"
