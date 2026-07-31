"""
analysis.py — Worst-case cycle analysis for interrupt-disabled regions.

For each region:
1. Build a CFG from the region's statement list.
2. Walk the CFG computing the worst-case cycle cost via a recursive DFS,
   always taking the maximum across branches.
3. For loops: if the bound is statically known, multiply (body_cost × bound)
   + loop overhead; if unknown, mark the region as UNBOUNDED.
4. For function calls: if the callee is defined in the file, recursively
   analyse its body; if it's external/undefined, mark as UNBOUNDED.
5. Return a list of :class:`RegionResult` objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from pycparser import c_ast
except ImportError as exc:  # pragma: no cover
    raise ImportError("pycparser is required: pip install pycparser") from exc

from irq_verify.cfg import CFG, BasicBlock, CFGBuilder, _node_to_str, build_cfg
from irq_verify.cycle_table import (
    ASSIGN, ARITH, COMPARE, CALL_OVERHEAD, MEM_READ, MEM_WRITE,
    BRANCH, LOOP_ITER, UNARY, get_cost,
)
from irq_verify.parser import IrqRegion, _extract_loop_bound_annotation


# ---------------------------------------------------------------------------
# Result data structure
# ---------------------------------------------------------------------------


@dataclass
class PathStep:
    """One human-readable step in the worst-case path."""
    description: str
    cycles: int
    line: Optional[int] = None


@dataclass
class RegionResult:
    """Analysis result for one interrupt-disabled region."""

    region: IrqRegion
    worst_case_cycles: Optional[int]   # None → UNBOUNDED
    is_unbounded: bool
    unbounded_reason: Optional[str]
    worst_case_path: list[PathStep] = field(default_factory=list)
    budget_used: Optional[int] = None   # The budget that was applied
    passed: bool = False

    @property
    def line(self) -> int:
        return self.region.disable_line


# ---------------------------------------------------------------------------
# Cycle estimator: maps a single AST node to a cycle cost
# ---------------------------------------------------------------------------


def _estimate_node_cycles(
    node: Any,
    cycle_table: dict[str, int],
    func_defs: dict[str, Any],
    call_stack: frozenset[str],
    verbose: bool,
    region: Optional[IrqRegion] = None,
) -> tuple[int, bool, Optional[str], list[PathStep]]:
    """
    Estimate the worst-case cycle cost of a single AST node.

    Returns (cycles, is_unbounded, reason, path_steps).
    """
    if node is None:
        return 0, False, None, []

    # ------------------------------------------------------------------ #
    # Assignment                                                           #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.Assignment):
        lhs_cost, lhs_unb, lhs_r, _lhs_path = _estimate_node_cycles(
            node.lvalue, cycle_table, func_defs, call_stack, verbose, region
        )
        rhs_cost, rhs_unb, rhs_r, _rhs_path = _estimate_node_cycles(
            node.rvalue, cycle_table, func_defs, call_stack, verbose, region
        )
        if lhs_unb:
            return 0, True, lhs_r, []
        if rhs_unb:
            return 0, True, rhs_r, []
        base = get_cost(cycle_table, ASSIGN)            # cost: ASSIGN(2)
        total = base + lhs_cost + rhs_cost              # cost: ASSIGN + lhs + rhs
        # The step's cycles already include lhs_cost and rhs_cost, so we do NOT
        # append _lhs_path/_rhs_path — that would double-count those sub-costs
        # in the displayed path total.
        step = PathStep(
            description=f"assignment ({_node_to_str(node.lvalue)} {node.op} {_node_to_str(node.rvalue)})",
            cycles=total,
            line=node.coord.line if node.coord else None,
        )
        return total, False, None, [step]

    # ------------------------------------------------------------------ #
    # Decl (variable declaration with initialiser)                         #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.Decl):
        if node.init is not None:
            init_cost, init_unb, init_r, _init_path = _estimate_node_cycles(
                node.init, cycle_table, func_defs, call_stack, verbose, region
            )
            if init_unb:
                return 0, True, init_r, []
            base = get_cost(cycle_table, ASSIGN)        # cost: ASSIGN(2)
            total = base + init_cost                    # cost: ASSIGN + init_expr
            # step.cycles already includes init_cost, so _init_path is NOT appended.
            step = PathStep(
                description=f"declaration with init ({node.name})",
                cycles=total,
                line=node.coord.line if node.coord else None,
            )
            return total, False, None, [step]
        return 0, False, None, []

    # ------------------------------------------------------------------ #
    # Function call                                                         #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.FuncCall):
        return _estimate_call(node, cycle_table, func_defs, call_stack, verbose, region)

    # ------------------------------------------------------------------ #
    # Binary operation                                                     #
    # Each BinaryOp operator costs 1 ARITH or COMPARE cycle; operand      #
    # sub-expressions (e.g. pointer dereferences in the rhs of an         #
    # assignment) are recursively costed so they are not silently dropped. #
    # Without recursion, `sum = *R0 + *R1` would charge ARITH(1) and miss #
    # the two LDR cycles for *R0 and *R1 — a soundness violation.         #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.BinaryOp):
        op = node.op
        if op in ("<", ">", "<=", ">=", "==", "!="):
            key = COMPARE
            desc = f"comparison ({_node_to_str(node)})"
        else:
            key = ARITH
            desc = f"arithmetic ({_node_to_str(node)})"
        op_cost = get_cost(cycle_table, key)
        left_c, left_u, left_r, _ = _estimate_node_cycles(
            node.left, cycle_table, func_defs, call_stack, verbose, region
        )
        if left_u:
            return 0, True, left_r, []
        right_c, right_u, right_r, _ = _estimate_node_cycles(
            node.right, cycle_table, func_defs, call_stack, verbose, region
        )
        if right_u:
            return 0, True, right_r, []
        # op_cost: COMPARE(1) or ARITH(1) for the operator instruction.
        # left_c + right_c: cycles for reading operand values from memory.
        total_cost = op_cost + left_c + right_c  # cost: ARITH/COMPARE(1) + left + right
        step = PathStep(description=desc, cycles=total_cost,
                        line=node.coord.line if node.coord else None)
        return total_cost, False, None, [step]

    # ------------------------------------------------------------------ #
    # Unary operation                                                       #
    # A pointer dereference '*' costs MEM_READ (LDR on ARM = 2 cycles).   #
    # Other unary operators cost UNARY (1 cycle for a single ALU op).     #
    # The dead 'elif UnaryOp and op=="*"' block that previously appeared   #
    # after this return has been removed — it was unreachable.             #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.UnaryOp):
        if node.op == "*":
            # Pointer dereference: LDR on Cortex-M0 = MEM_READ cycles.
            # The operand is an ID or address-value expression; we do NOT
            # recurse further — the address is already in a register.
            cost = get_cost(cycle_table, MEM_READ)  # cost: MEM_READ(2) — 1 LDR
            desc = f"memory read (*{_node_to_str(node.expr)})"
            step = PathStep(
                description=desc,
                cycles=cost,
                line=node.coord.line if node.coord else None,
            )
            return cost, False, None, [step]
        else:
            # Non-dereference unary (!, ~, -, ++, --):
            # 1 ALU instruction for the operator + cost of evaluating the operand.
            # Without recursion, `!(*REG)` would miss the MEM_READ for *REG
            # — the same soundness gap that was fixed for BinaryOp operands.
            child_cost, child_unb, child_r, _ = _estimate_node_cycles(
                node.expr, cycle_table, func_defs, call_stack, verbose, region
            )
            if child_unb:
                return 0, True, child_r, []
            op_cost = get_cost(cycle_table, UNARY)   # cost: UNARY(1) — 1 ALU op
            total = op_cost + child_cost              # cost: UNARY + child_expr
            desc = f"unary op ({node.op}{_node_to_str(node.expr)})"
            step = PathStep(
                description=desc,
                cycles=total,
                line=node.coord.line if node.coord else None,
            )
            return total, False, None, [step]


    # ------------------------------------------------------------------ #
    # Memory access: array subscript and struct field                      #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.ArrayRef):
        cost = get_cost(cycle_table, MEM_READ)      # cost: MEM_READ(2) — 1 LDR
        step = PathStep(description=f"array read ({_node_to_str(node)})",
                        cycles=cost,
                        line=node.coord.line if node.coord else None)
        return cost, False, None, [step]

    if isinstance(node, c_ast.StructRef):
        cost = get_cost(cycle_table, MEM_READ)      # cost: MEM_READ(2) — 1 LDR
        step = PathStep(description=f"struct/member access ({_node_to_str(node)})",
                        cycles=cost,
                        line=node.coord.line if node.coord else None)
        return cost, False, None, [step]

    # ------------------------------------------------------------------ #
    # Constants and identifiers — cost 0 (compiler folds to immediate)   #
    # ------------------------------------------------------------------ #
    if isinstance(node, (c_ast.Constant, c_ast.ID)):
        return 0, False, None, []

    # ------------------------------------------------------------------ #
    # Return statement                                                      #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.Return):
        if node.expr is not None:
            return _estimate_node_cycles(
                node.expr, cycle_table, func_defs, call_stack, verbose, region
            )
        return 0, False, None, []

    # ------------------------------------------------------------------ #
    # DeclList (for-loop init)                                             #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.DeclList):
        total_cost = 0
        all_steps: list[PathStep] = []
        for decl in (node.decls or []):
            c, u, r, steps = _estimate_node_cycles(
                decl, cycle_table, func_defs, call_stack, verbose, region
            )
            if u:
                return 0, True, r, []
            total_cost += c
            all_steps.extend(steps)
        return total_cost, False, None, all_steps

    # ------------------------------------------------------------------ #
    # ExprList — comma-separated list                                       #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.ExprList):
        total_cost = 0
        expr_steps: list[PathStep] = []
        for expr in (node.exprs or []):
            c, u, r, steps = _estimate_node_cycles(
                expr, cycle_table, func_defs, call_stack, verbose, region
            )
            if u:
                return 0, True, r, []
            total_cost += c
            expr_steps.extend(steps)
        return total_cost, False, None, expr_steps

    # ------------------------------------------------------------------ #
    # Cast expression                                                       #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.Cast):
        return _estimate_node_cycles(node.expr, cycle_table, func_defs, call_stack, verbose, region)

    # ------------------------------------------------------------------ #
    # Ternary (a ? b : c) — take worst branch + compare cost              #
    # ------------------------------------------------------------------ #
    if isinstance(node, c_ast.TernaryOp):
        cond_c, cond_u, cond_r, _ = _estimate_node_cycles(
            node.cond, cycle_table, func_defs, call_stack, verbose, region
        )
        if cond_u:
            return 0, True, cond_r, []
        true_c, true_u, true_r, true_path = _estimate_node_cycles(
            node.iftrue, cycle_table, func_defs, call_stack, verbose, region
        )
        false_c, false_u, false_r, false_path = _estimate_node_cycles(
            node.iffalse, cycle_table, func_defs, call_stack, verbose, region
        )
        if true_u and false_u:
            return 0, True, true_r or false_r, []
        if true_u:
            return 0, True, true_r, []
        if false_u:
            return 0, True, false_r, []
        branch_cost = get_cost(cycle_table, BRANCH)
        # Emit an explicit step for the condition evaluation + branch cost so
        # path step cycles sum correctly to total (previously cond_c+branch_cost
        # were computed into total but had no corresponding path entry).
        if true_c >= false_c:
            total = cond_c + branch_cost + true_c
            cond_step = PathStep(
                description=f"ternary condition + branch (worse arm: {true_c} cy)",
                cycles=cond_c + branch_cost,
                line=node.coord.line if node.coord else None,
            )
            return total, False, None, [cond_step] + true_path
        else:
            total = cond_c + branch_cost + false_c
            cond_step = PathStep(
                description=f"ternary condition + branch (worse arm: {false_c} cy)",
                cycles=cond_c + branch_cost,
                line=node.coord.line if node.coord else None,
            )
            return total, False, None, [cond_step] + false_path

    # ------------------------------------------------------------------ #
    # Unknown / unhandled node type                                         #
    # ------------------------------------------------------------------ #
    # SAFETY RULE: unknown nodes must NEVER silently receive a non-zero cost
    # estimate.  Doing so could produce a finite-looking total that under-counts
    # real cycles, which is the primary failure mode this tool exists to prevent.
    # Instead, flag the region as UNANALYZABLE so the developer must inspect it.
    node_type = type(node).__name__
    reason = (
        f"unrecognised AST node type '{node_type}' at "
        f"{node.coord if hasattr(node, 'coord') and node.coord else 'unknown location'} "
        f"— cannot determine cycle cost; treat as UNANALYZABLE"
    )
    return 0, True, reason, []


def _estimate_call(
    node: c_ast.FuncCall,
    cycle_table: dict[str, int],
    func_defs: dict[str, Any],
    call_stack: frozenset[str],
    verbose: bool,
    region: Optional[IrqRegion] = None,
) -> tuple[int, bool, Optional[str], list[PathStep]]:
    """Estimate cycles for a function call, inlining if possible."""
    name_node = node.name
    if not isinstance(name_node, c_ast.ID):
        # Indirect call through a function pointer — cannot determine callee.
        # Per the soundness contract this is UNANALYZABLE.
        reason = (
            "indirect function call via function pointer "
            "— cannot determine callee statically; UNANALYZABLE"
        )
        return 0, True, reason, []

    fn_name = name_node.name

    # setjmp/longjmp break normal control-flow assumptions entirely: longjmp
    # can transfer control out of the critical section to an arbitrary saved
    # state, making worst-case path analysis impossible.
    if fn_name in ("setjmp", "longjmp", "_setjmp", "sigsetjmp", "siglongjmp"):
        reason = (
            f"call to '{fn_name}' inside critical section — setjmp/longjmp "
            f"break normal control-flow and make WCET analysis impossible; UNANALYZABLE"
        )
        return 0, True, reason, []

    # Detect recursion (treat as unbounded)
    if fn_name in call_stack:
        reason = f"recursive call to '{fn_name}' — unbounded"
        return 0, True, reason, []

    # If not defined in the file → external/undefined → unbounded
    if fn_name not in func_defs:
        reason = f"call to external/undefined function '{fn_name}' — cannot determine cycle cost"
        return 0, True, reason, []

    # Inline the callee
    callee_def = func_defs[fn_name]
    callee_body = callee_def.body
    if callee_body is None or not hasattr(callee_body, "block_items"):
        overhead = get_cost(cycle_table, CALL_OVERHEAD)
        step = PathStep(description=f"call {fn_name}() — empty body", cycles=overhead)
        return overhead, False, None, [step]

    body_stmts = callee_body.block_items or []
    new_call_stack = call_stack | {fn_name}

    body_cost, body_unb, body_reason, body_path = _estimate_stmts_cycles(
        body_stmts, cycle_table, func_defs, new_call_stack, verbose, region
    )

    overhead = get_cost(cycle_table, CALL_OVERHEAD)
    total = overhead + body_cost

    if body_unb:
        reason = f"call to '{fn_name}' is unbounded: {body_reason}"
        return 0, True, reason, []

    # Split into two path entries so their cycles sum correctly to total:
    #   overhead_step.cycles = overhead          (BL + worst-case prologue/epilogue)
    #   body_path steps sum  = body_cost         (inlined body statements)
    #   -------------------------------------------------
    #   total                = overhead + body_cost
    #
    # Previously a single step carried cycles=total AND body_path was also
    # appended, making the displayed sum = body_cost + total (double-counted).
    overhead_step = PathStep(
        description=(
            f"call {fn_name}() [BL + worst-case prologue/epilogue overhead]"
        ),
        cycles=overhead,
        line=node.coord.line if node.coord else None,
    )
    return total, False, None, [overhead_step] + body_path


def _estimate_stmts_cycles(
    stmts: list[Any],
    cycle_table: dict[str, int],
    func_defs: dict[str, Any],
    call_stack: frozenset[str],
    verbose: bool,
    region: Optional[IrqRegion] = None,
) -> tuple[int, bool, Optional[str], list[PathStep]]:
    """
    Compute worst-case cycle cost for a flat list of AST statements.
    Handles If, For, While, DoWhile specially; everything else is delegated to
    _estimate_node_cycles.
    """
    total = 0
    all_steps: list[PathStep] = []

    for stmt in stmts:
        c, u, r, steps = _estimate_single_stmt(
            stmt, cycle_table, func_defs, call_stack, verbose, region
        )
        if u:
            return 0, True, r, []
        total += c
        all_steps.extend(steps)

    return total, False, None, all_steps


def _estimate_single_stmt(  # noqa: C901
    stmt: Any,
    cycle_table: dict[str, int],
    func_defs: dict[str, Any],
    call_stack: frozenset[str],
    verbose: bool,
    region: Optional[IrqRegion] = None,
) -> tuple[int, bool, Optional[str], list[PathStep]]:
    """Estimate cost of one statement (which may be compound)."""

    if stmt is None:
        return 0, False, None, []

    # Compound block
    if isinstance(stmt, c_ast.Compound):
        return _estimate_stmts_cycles(
            stmt.block_items or [], cycle_table, func_defs, call_stack, verbose, region
        )

    # If / Else
    if isinstance(stmt, c_ast.If):
        cond_cost = get_cost(cycle_table, COMPARE) + get_cost(cycle_table, BRANCH)
        true_cost, true_unb, true_r, true_path = _estimate_single_stmt(
            stmt.iftrue, cycle_table, func_defs, call_stack, verbose, region
        )
        false_cost: int = 0
        false_unb: bool = False
        false_r: Optional[str] = None
        false_path: list[PathStep] = []
        if stmt.iffalse is not None:
            false_cost, false_unb, false_r, false_path = _estimate_single_stmt(
                stmt.iffalse, cycle_table, func_defs, call_stack, verbose, region
            )

        # If either branch is unbounded, the whole if is unbounded
        if true_unb:
            return 0, True, true_r, []
        if false_unb:
            return 0, True, false_r, []

        # Take the maximum (worst case)
        line_info = stmt.coord.line if stmt.coord else None
        if true_cost >= false_cost:
            total = cond_cost + true_cost
            branch_step = PathStep(
                description=f"if-branch taken (worse path, {true_cost} cycles)",
                cycles=cond_cost,
                line=line_info,
            )
            return total, False, None, [branch_step] + true_path
        else:
            total = cond_cost + false_cost
            branch_step = PathStep(
                description=f"else-branch taken (worse path, {false_cost} cycles)",
                cycles=cond_cost,
                line=line_info,
            )
            return total, False, None, [branch_step] + false_path

    # For loop
    if isinstance(stmt, c_ast.For):
        return _estimate_for(stmt, cycle_table, func_defs, call_stack, verbose, region)

    # While loop
    if isinstance(stmt, c_ast.While):
        return _estimate_while(stmt, cycle_table, func_defs, call_stack, verbose, region)

    # Do-While loop
    if isinstance(stmt, c_ast.DoWhile):
        return _estimate_dowhile(stmt, cycle_table, func_defs, call_stack, verbose, region)

    # Switch statement — analyse each case as an independent branch, return max.
    if isinstance(stmt, c_ast.Switch):
        return _estimate_switch(stmt, cycle_table, func_defs, call_stack, verbose, region)

    # goto — non-structured control flow breaks the CFG model entirely.
    # A goto can jump forward, backward, or into/out of a loop, making
    # worst-case path analysis unsound without a full control-flow graph
    # that the current statement-list model does not build.
    if isinstance(stmt, c_ast.Goto):
        line_info = stmt.coord.line if stmt.coord else "?"
        reason = (
            f"goto statement at line {line_info} (target: '{stmt.name}') — "
            f"non-structured control flow is not supported; UNANALYZABLE"
        )
        return 0, True, reason, []

    # Label — present when a goto target exists in the critical section.
    # The label itself costs nothing to execute, but its presence implies
    # a goto is jumping into this region, which we cannot model soundly.
    if isinstance(stmt, c_ast.Label):
        line_info = stmt.coord.line if stmt.coord else "?"
        reason = (
            f"label '{stmt.name}' at line {line_info} — label inside critical "
            f"section implies a goto target, which cannot be modelled soundly; UNANALYZABLE"
        )
        return 0, True, reason, []

    # Break / Continue / Return — unconditional jumps / returns.
    # On Cortex-M, these compile to B (branch) or POP {PC} (return),
    # both of which cost BRANCH cycles (pipeline flush).
    if isinstance(stmt, (c_ast.Break, c_ast.Continue)):
        cost = get_cost(cycle_table, BRANCH)
        desc = "break" if isinstance(stmt, c_ast.Break) else "continue"
        line_info = stmt.coord.line if stmt.coord else None
        return cost, False, None, [PathStep(description=desc, cycles=cost, line=line_info)]

    if isinstance(stmt, c_ast.Return):
        line_info = stmt.coord.line if stmt.coord else None
        # A Return with a value expression may have sub-costs.
        val_cost = 0
        val_steps: list[PathStep] = []
        if stmt.expr is not None:
            val_cost, val_unb, val_r, val_steps = _estimate_node_cycles(
                stmt.expr, cycle_table, func_defs, call_stack, verbose, region
            )
            if val_unb:
                return 0, True, val_r, []
        branch_cost = get_cost(cycle_table, BRANCH)
        total = val_cost + branch_cost
        ret_step = PathStep(description="return", cycles=branch_cost, line=line_info)
        return total, False, None, val_steps + [ret_step]

    # Switch statement
    if isinstance(stmt, c_ast.Switch):
        return _estimate_switch(stmt, cycle_table, func_defs, call_stack, verbose, region)

    # Default: delegate to node estimator
    return _estimate_node_cycles(stmt, cycle_table, func_defs, call_stack, verbose, region)


def _estimate_switch(  # noqa: C901
    stmt: c_ast.Switch,
    cycle_table: dict[str, int],
    func_defs: dict[str, Any],
    call_stack: frozenset[str],
    verbose: bool,
    region: Optional[IrqRegion] = None,
) -> tuple[int, bool, Optional[str], list[PathStep]]:
    """Estimate cycles for a ``switch`` statement.

    Strategy
    --------
    1. Walk the switch body and group statements into per-case buckets.
    2. For each bucket, check that the last control-flow statement is a
       ``Break`` (or the bucket is the last one and falls off the end).
       Any bucket that falls through to the next case → UNANALYZABLE.
    3. Estimate the cost of each case's statement list.
    4. Return ``max(case_costs) + BRANCH`` (dispatch overhead).
    """
    line_info = stmt.coord.line if stmt.coord else "?"

    # The body of a switch is usually a Compound containing a flat list of
    # Case / Default / regular-statement nodes.
    body = stmt.stmt
    if body is None:
        # Empty switch — 0 cost.
        return 0, False, None, []

    raw_items: list[Any] = []
    if isinstance(body, c_ast.Compound) and body.block_items:
        raw_items = body.block_items
    elif not isinstance(body, c_ast.Compound):
        raw_items = [body]

    if not raw_items:
        return 0, False, None, []

    # ------------------------------------------------------------------ #
    # Split into (label_node, [stmts]) buckets.                           #
    # Each bucket starts at a Case or Default node.                       #
    # ------------------------------------------------------------------ #
    # bucket = (case_node, [stmts_before_next_case])
    buckets: list[tuple[Any, list[Any]]] = []
    current_label: Any = None
    current_stmts: list[Any] = []

    for item in raw_items:
        if isinstance(item, (c_ast.Case, c_ast.Default)):
            # Start a new bucket.
            if current_label is not None:
                buckets.append((current_label, current_stmts))
            current_label = item
            current_stmts = []
            # A Case node itself can hold a statement (e.g., `case 0: stmt;`)
            if isinstance(item, c_ast.Case) and item.stmts:
                current_stmts.extend(item.stmts)
            elif isinstance(item, c_ast.Default) and item.stmts:
                current_stmts.extend(item.stmts)
        else:
            current_stmts.append(item)

    if current_label is not None:
        buckets.append((current_label, current_stmts))

    if not buckets:
        # No Case/Default labels at all — treat the whole body as one block.
        return _estimate_stmts_cycles(
            raw_items, cycle_table, func_defs, call_stack, verbose, region
        )

    # ------------------------------------------------------------------ #
    # Fallthrough detection and cost estimation                           #
    # ------------------------------------------------------------------ #
    def _stmts_end_with_break(stmts: list[Any]) -> bool:
        """Return True if the last control-flow statement is a Break."""
        for s in reversed(stmts):
            if isinstance(s, c_ast.Break):
                return True
            if isinstance(s, c_ast.If):
                # Both branches end with break — conservatively accept.
                true_ok = _stmts_end_with_break(
                    [s.iftrue] if s.iftrue else []
                )
                false_ok = _stmts_end_with_break(
                    [s.iffalse] if s.iffalse else []
                ) if s.iffalse else True
                if true_ok and false_ok:
                    return True
                return False
            if isinstance(s, (c_ast.Assignment, c_ast.Decl,
                               c_ast.FuncCall, c_ast.Return,
                               c_ast.Compound)):
                break   # non-control-flow: keep looking backwards
        return False

    worst_cost = 0
    worst_path: list[PathStep] = []
    dispatch_cost = get_cost(cycle_table, BRANCH)   # switch dispatch overhead

    for idx, (label_node, case_stmts) in enumerate(buckets):
        is_last = (idx == len(buckets) - 1)

        # Detect fallthrough: not the last bucket and no Break found.
        if not is_last and not _stmts_end_with_break(case_stmts):
            label_line = label_node.coord.line if label_node.coord else line_info
            reason = (
                f"switch at line {line_info}: case at line {label_line} "
                f"falls through to the next case without a 'break' — "
                f"fallthrough makes worst-case cost unsound; UNANALYZABLE"
            )
            return 0, True, reason, []

        # Cost of this case's body.
        case_cost, case_unb, case_r, case_path = _estimate_stmts_cycles(
            case_stmts, cycle_table, func_defs, call_stack, verbose, region
        )
        if case_unb:
            return 0, True, case_r, []

        if case_cost > worst_cost:
            worst_cost = case_cost
            worst_path = case_path

    total = dispatch_cost + worst_cost
    label_line = (
        stmt.coord.line if stmt.coord else "?"
    )
    switch_step = PathStep(
        description=(
            f"switch (line {label_line}): worst-case case costs {worst_cost} cycles"
        ),
        cycles=dispatch_cost,
        line=stmt.coord.line if stmt.coord else None,
    )
    return total, False, None, [switch_step] + worst_path


def _estimate_for(
    stmt: c_ast.For,
    cycle_table: dict[str, int],
    func_defs: dict[str, Any],
    call_stack: frozenset[str],
    verbose: bool,
    region: Optional[IrqRegion] = None,
) -> tuple[int, bool, Optional[str], list[PathStep]]:
    """Estimate cycles for a for loop."""
    line_info = stmt.coord.line if stmt.coord else "?"

    # Try to extract a constant bound
    cond = stmt.cond
    bound: Optional[int] = None
    bound_reason = "unknown"

    if cond is None:
        reason = f"for loop at line {line_info} — no condition (infinite loop)"
        return 0, True, reason, []

    if isinstance(cond, c_ast.BinaryOp) and cond.op in ("<", "<=", "!="):
        rhs = cond.right
        if isinstance(rhs, c_ast.Constant):
            try:
                n = int(rhs.value, 0)
                if cond.op == "<=":
                    n += 1
                bound = n
                bound_reason = f"static bound: {bound} iterations"
            except ValueError:
                pass
        if bound is None:
            rhs_str = _node_to_str(rhs)
            reason = (
                f"for loop at line {line_info} — bound depends on runtime variable: {rhs_str}"
            )
            return 0, True, reason, []
    else:
        reason = (
            f"for loop at line {line_info} — cannot statically determine bound "
            f"(condition: {_node_to_str(cond)})"
        )
        return 0, True, reason, []

    # Body cost
    body_stmts: list[Any] = []
    if stmt.stmt is not None:
        if isinstance(stmt.stmt, c_ast.Compound):
            body_stmts = stmt.stmt.block_items or []
        else:
            body_stmts = [stmt.stmt]

    body_cost, body_unb, body_r, body_path = _estimate_stmts_cycles(
        body_stmts, cycle_table, func_defs, call_stack, verbose, region
    )

    if body_unb:
        reason = f"for loop at line {line_info} body is unbounded: {body_r}"
        return 0, True, reason, []

    loop_overhead = get_cost(cycle_table, LOOP_ITER)
    total = bound * (body_cost + loop_overhead)

    # The loop summary step carries cycles=total which already encodes
    # bound × (body_cost + loop_overhead).  body_path must NOT be appended:
    # its cycle values are already baked into total, appending them would
    # double-count them in the displayed path sum.
    step = PathStep(
        description=(
            f"for loop (line {line_info}): {bound} iterations x "
            f"({body_cost} body + {loop_overhead} iter-overhead) = {total} cycles"
        ),
        cycles=total,
        line=line_info if isinstance(line_info, int) else None,
    )
    return total, False, None, [step]


def _estimate_while(
    stmt: c_ast.While,
    cycle_table: dict[str, int],
    func_defs: dict[str, Any],
    call_stack: frozenset[str],
    verbose: bool,
    region: Optional[IrqRegion] = None,
) -> tuple[int, bool, Optional[str], list[PathStep]]:
    """Estimate cycles for a while loop.
    
    Checks for // @irq_loop_bound(N) annotation immediately before the while statement.
    If found, treats the loop as bounded; otherwise marks as UNBOUNDED.
    """
    line_info = stmt.coord.line if stmt.coord else 0
    
    # Try to extract annotation if we have region context
    bound: Optional[int] = None
    if region is not None and line_info:
        source = "\n".join(region.source_lines.values())
        
        # Translate preprocessed line number to original source line number
        if region.line_map is not None:
            # pcpp path: use line_map
            _, orig_line = region.line_map.get(
                line_info,
                ("", line_info - region.line_offset)
            )
            original_line = orig_line - region.line_offset
        else:
            # Legacy path: simple arithmetic
            original_line = line_info - region.line_offset
        
        bound = _extract_loop_bound_annotation(source, original_line, line_offset=0)
    
    if bound is not None:
        # User provided an asserted bound
        body_stmts: list[Any] = []
        if stmt.stmt is not None:
            if isinstance(stmt.stmt, c_ast.Compound):
                body_stmts = stmt.stmt.block_items or []
            else:
                body_stmts = [stmt.stmt]

        body_cost, body_unb, body_r, body_path = _estimate_stmts_cycles(
            body_stmts, cycle_table, func_defs, call_stack, verbose, region
        )

        if body_unb:
            reason = f"while loop at line {line_info} body is unbounded: {body_r}"
            return 0, True, reason, []

        loop_overhead = get_cost(cycle_table, LOOP_ITER)
        total = bound * (body_cost + loop_overhead)

        step = PathStep(
            description=(
                f"while loop (line {line_info}) [ASSERTED BOUND]: {bound} iterations x "
                f"({body_cost} body + {loop_overhead} iter-overhead) = {total} cycles"
            ),
            cycles=total,
            line=line_info if isinstance(line_info, int) else None,
        )
        return total, False, None, [step]
    
    # No annotation found - mark as unbounded
    reason = (
        f"while loop at line {line_info} — bound depends on runtime condition; "
        f"add // @irq_loop_bound(N) annotation before the while statement to assert a known bound"
    )
    return 0, True, reason, []


def _estimate_dowhile(
    stmt: c_ast.DoWhile,
    cycle_table: dict[str, int],
    func_defs: dict[str, Any],
    call_stack: frozenset[str],
    verbose: bool,
    region: Optional[IrqRegion] = None,
) -> tuple[int, bool, Optional[str], list[PathStep]]:
    """Estimate cycles for a do-while loop.
    
    Checks for // @irq_loop_bound(N) annotation immediately before the do-while statement.
    If found, treats the loop as bounded; otherwise marks as UNBOUNDED.
    """
    line_info = stmt.coord.line if stmt.coord else 0
    
    # Try to extract annotation if we have region context
    bound: Optional[int] = None
    if region is not None and line_info:
        source = "\n".join(region.source_lines.values())
        
        # Translate preprocessed line number to original source line number
        if region.line_map is not None:
            # pcpp path: use line_map
            _, orig_line = region.line_map.get(
                line_info,
                ("", line_info - region.line_offset)
            )
            original_line = orig_line - region.line_offset
        else:
            # Legacy path: simple arithmetic
            original_line = line_info - region.line_offset
        
        bound = _extract_loop_bound_annotation(source, original_line, line_offset=0)
    
    if bound is not None:
        # User provided an asserted bound
        body_stmts: list[Any] = []
        if stmt.stmt is not None:
            if isinstance(stmt.stmt, c_ast.Compound):
                body_stmts = stmt.stmt.block_items or []
            else:
                body_stmts = [stmt.stmt]

        body_cost, body_unb, body_r, body_path = _estimate_stmts_cycles(
            body_stmts, cycle_table, func_defs, call_stack, verbose, region
        )

        if body_unb:
            reason = f"do-while loop at line {line_info} body is unbounded: {body_r}"
            return 0, True, reason, []

        loop_overhead = get_cost(cycle_table, LOOP_ITER)
        total = bound * (body_cost + loop_overhead)

        step = PathStep(
            description=(
                f"do-while loop (line {line_info}) [ASSERTED BOUND]: {bound} iterations x "
                f"({body_cost} body + {loop_overhead} iter-overhead) = {total} cycles"
            ),
            cycles=total,
            line=line_info if isinstance(line_info, int) else None,
        )
        return total, False, None, [step]
    
    # No annotation found - mark as unbounded
    reason = (
        f"do-while loop at line {line_info} — bound depends on runtime condition; "
        f"add // @irq_loop_bound(N) annotation before the do statement to assert a known bound"
    )
    return 0, True, reason, []


# ---------------------------------------------------------------------------
# Region analyser
# ---------------------------------------------------------------------------


def analyse_region(
    region: IrqRegion,
    cycle_table: dict[str, int],
    global_budget: Optional[int],
    verbose: bool = False,
) -> RegionResult:
    """Analyse a single :class:`IrqRegion` and return a :class:`RegionResult`."""
    budget = region.budget if region.budget is not None else global_budget

    total_cost, is_unb, unb_reason, path = _estimate_stmts_cycles(
        region.stmts,
        cycle_table,
        region.func_defs,
        frozenset(),
        verbose,
        region,  # Pass region for loop bound annotation extraction
    )

    if is_unb:
        return RegionResult(
            region=region,
            worst_case_cycles=None,
            is_unbounded=True,
            unbounded_reason=unb_reason,
            worst_case_path=path,
            budget_used=budget,
            passed=False,
        )

    passed = (budget is None) or (total_cost <= budget)

    return RegionResult(
        region=region,
        worst_case_cycles=total_cost,
        is_unbounded=False,
        unbounded_reason=None,
        worst_case_path=path,
        budget_used=budget,
        passed=passed,
    )


def analyse_regions(
    regions: list[IrqRegion],
    ast: Any,
    cycle_table: dict[str, int],
    global_budget: Optional[int],
    verbose: bool = False,
) -> list[RegionResult]:
    """Analyse all regions and return a list of results."""
    return [
        analyse_region(r, cycle_table, global_budget, verbose)
        for r in regions
    ]
