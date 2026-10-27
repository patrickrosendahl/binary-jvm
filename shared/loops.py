# shared with binary-cpp (moved here from binary-jvm/pseudo_java.py, jvm-50/54/82 / jvm-68; binary-cpp: not yet,
# bncpp-87); keep language-neutral
"""Loop conditions and split short-circuit conditions (jvm-50, jvm-54, jvm-82): analysis only, the language prints.

  loop_cond_shape(w, after, block)   `while (true) { defs; if (c) { tail; break; } rest }` -> `while (!c) { rest }
                                     tail`; defs read once by c fold into it, defs read again become `(x = e)` in
                                     it, `t = x; x = x + 1; if (t ..)` becomes `x++`; a do-while whose last
                                     statement feeds its test; the code after the loop moves to the single other
                                     break when it is short and ends the method (Vineflower's MergeHelper)
  merge_split_cond(items, k, hi)     `T x; if (a) { x = e; } if (!a || p(x))` -> `if (!a || p(e))` (same with &&)

Needs FoldMixin (shared/folding.py): eval_nodes, pure, side_effect, reads_memory, refs, var_count, def_count.

State the language class keeps (the passes record their decisions there):
  self.inline          {var: value}   as in FoldMixin; adds ("assignx", x, value, def statement) -- `(x = value)`
                                      printed where the condition reads the def's variable -- and incdec values
                                      without a store (`("incdec", f, step, pre, None, None)`)
  self.consumed        {expr_index}   statements printed elsewhere (not again in their own place)
  self.hidden_vars     {var}          variables whose declaration does not print
  self.count_discount  {var: n}       reads of var that no longer print (var_count subtracts them)
  self.hoisted         {var}          variables already declared further out
  self.loop_decls      {loop expr_index: [(var, def)]}   declared before the loop: assigned in its condition
  self.break_subst     {expr_index: (items, block)}      a break (or a do-while) that prints the moved code
Hooks (defaults for a language without exception plumbing in its HLIL):
  items_of(instr)      -> [stmt]      the statements of a block, nested blocks spliced in
  opaque(x)            -> bool        an item of items_of that is not an HLIL statement (never matched)
  opaque_if(s)         -> bool        an if that is part of the language's plumbing, not a source if
  item_instr(x)        -> instr       the HLIL to walk for an opaque item
  hidden_item(x)       -> bool        prints nothing (NOP, consumed)
  ends_flow(items)     -> bool        the last printed statement never falls through
  movable(e)           -> bool        e may print elsewhere than its own statement
  fixed_var(var)       -> bool        var must keep its own assignments
  plain_condition(c)   -> bool        a do-while condition that is a source test (not plumbing)
  cond_movable(w, exprs) -> bool      exprs may move into the loop test (False: they throw inside a try)
  loop_exit_special(items, k, defs, do_cond) -> shape | False | None   a language's own exit test shape at items[k]
                                      (a shape is returned as is, False rejects the loop, None goes on)
  loop_breaks(items), infinite_loop(w)   the loop's own break / continue statements; a loop left only by
                                      break / return / throw
  split_conditions, assign_in_conditions   class attributes switching the passes (default True)
"""
from binaryninja import HighLevelILOperation

from .hlil import children, walk

Op = HighLevelILOperation

NEGATED_CMP = {Op.HLIL_CMP_E: Op.HLIL_CMP_NE, Op.HLIL_CMP_NE: Op.HLIL_CMP_E,
               Op.HLIL_CMP_SLT: Op.HLIL_CMP_SGE, Op.HLIL_CMP_SGE: Op.HLIL_CMP_SLT,
               Op.HLIL_CMP_SLE: Op.HLIL_CMP_SGT, Op.HLIL_CMP_SGT: Op.HLIL_CMP_SLE,
               Op.HLIL_CMP_ULT: Op.HLIL_CMP_UGE, Op.HLIL_CMP_UGE: Op.HLIL_CMP_ULT,
               Op.HLIL_CMP_ULE: Op.HLIL_CMP_UGT, Op.HLIL_CMP_UGT: Op.HLIL_CMP_ULE}

_LOOPS = (Op.HLIL_WHILE, Op.HLIL_DO_WHILE, Op.HLIL_FOR)


class LoopMixin:
    split_conditions = True
    assign_in_conditions = True

    # --- hooks ------------------------------------------------------------------------------------------
    def items_of(self, instr):
        if instr is None:
            return []
        if instr.operation != Op.HLIL_BLOCK:
            return [instr]
        out = []
        for s in instr.body:
            out.extend(self.items_of(s) if s.operation == Op.HLIL_BLOCK else [s])
        return out

    def opaque(self, x):
        return False

    def opaque_if(self, s):
        return False

    def item_instr(self, x):
        return x

    def hidden_item(self, x):
        return x.operation == Op.HLIL_NOP or x.expr_index in self.consumed

    def ends_flow(self, items):
        for s in reversed(items):
            if self.opaque(s) or self.hidden_item(s):
                continue
            return s.operation in (Op.HLIL_RET, Op.HLIL_BREAK, Op.HLIL_CONTINUE, Op.HLIL_GOTO, Op.HLIL_NORET,
                                   Op.HLIL_TAILCALL)
        return False

    def movable(self, e):
        return True

    def fixed_var(self, var):
        return False

    def plain_condition(self, c):
        return True

    def cond_movable(self, w, exprs):
        return True

    def loop_exit_special(self, items, k, defs, do_cond):
        return None

    def loop_breaks(self, items):
        """break / continue statements of the loop these items belong to (not of nested loops / switches)"""
        out = []
        stack = [x for x in items if not self.opaque(x)]
        while stack:
            i = stack.pop()
            if i.operation in (Op.HLIL_BREAK, Op.HLIL_CONTINUE):
                out.append(i)
            elif i.operation not in _LOOPS + (Op.HLIL_SWITCH,):
                stack.extend(children(i))
        return out

    def infinite_loop(self, w):
        """a loop that is only left by break / return / throw: `while (true)`, `do { } while (true)`, `for (;;)`"""
        cond = w.condition
        true = cond.operation == Op.HLIL_CONST and cond.constant != 0
        if w.operation == Op.HLIL_FOR:
            return true and all(x.operation == Op.HLIL_NOP for x in (w.init, w.update))
        return true

    # --- loop conditions (jvm-50) -----------------------------------------------------------------------
    def loop_cond_shape(self, w, after=(), block=None):
        """(defs, if statement, tail items, rest items, moved) for
        `while (true) { defs; if (c) { tail; break; } rest }`; moved: indexes of the code after the loop that
        is printed at the loop's single other break (`if (x) break; ... } return y;` -> `if (x) { return y; }`)"""
        do_cond = None
        if not self.infinite_loop(w):
            if w.operation != Op.HLIL_DO_WHILE or not self.plain_condition(w.condition):
                return None
            do_cond = w.condition  # do { body } while (d) == while (true) { body; if (!d) break; }
        items = self.items_of(w.body)
        if self.split_conditions:
            for k in range(len(items)):
                try:
                    self.merge_split_cond(items, k, len(items) - 1)
                except Exception:
                    pass
        k = 0
        defs = []
        while k < len(items) and (self.hidden_item(items[k]) or
                                  (not self.opaque(items[k]) and
                                   (items[k].operation == Op.HLIL_VAR_INIT or
                                    (self.assign_in_conditions and items[k].operation == Op.HLIL_ASSIGN and
                                     items[k].dest.operation == Op.HLIL_VAR)))):
            if not self.hidden_item(items[k]):
                defs.append(items[k])
            k += 1
        if k >= len(items) or self.opaque(items[k]) or items[k].operation != Op.HLIL_IF:
            return None
        iff = items[k]
        if not self.cond_movable(w, [d.src for d in defs] + [iff.condition]):
            return None
        special = self.loop_exit_special(items, k, defs, do_cond)
        if special is False:
            return None
        if special is not None:
            return special
        if self.opaque_if(iff) or \
                not (iff.false is None or iff.false.operation in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE)):
            return None
        t_items = self.items_of(iff.true)
        real = [x for x in t_items if not self.hidden_item(x)]
        if not real or self.opaque(real[-1]):
            return None
        if real[-1].operation == Op.HLIL_BREAK:
            tail = t_items[:t_items.index(real[-1])]
        elif real[-1].operation not in (Op.HLIL_CONTINUE, Op.HLIL_GOTO) and self.ends_flow([real[-1]]):
            tail = t_items  # `if (c) { ...; return x; }`: the loop is only left there
        else:
            return None
        rest = items[k + 1:]
        moved = []
        if self.loop_breaks(tail):
            return None
        rest_breaks = [b for b in self.loop_breaks(rest) if b.operation == Op.HLIL_BREAK]
        if do_cond is not None:
            if rest_breaks or self.loop_breaks(rest):
                return None  # (a continue would skip the moved `if (!d)` test)
            moved = self.search_loop_tail([None], tail, after)
            last = [x for x in rest if not self.hidden_item(x)]
            if moved is None or not last or self.opaque(last[-1]) or \
                    last[-1].operation != Op.HLIL_ASSIGN or last[-1].dest.operation != Op.HLIL_VAR:
                return None
            x, asg = last[-1].dest.var, last[-1]
            if self.var_count(x) - self.refs(asg, x) != 1 or self.refs(do_cond, x) != 1 or \
                    self.def_count(x) != 2 or not self.movable(asg.src) or \
                    self.eval_nodes(do_cond, x)[1] is not True or \
                    any(self.side_effect(i) or self.reads_memory(i) for i in self.eval_nodes(do_cond, x)[0]):
                return None
        elif rest_breaks:
            moved = self.search_loop_tail(rest_breaks, tail, after)
            if moved is None:
                return None
        if not self.labels_internal(tail):
            return None  # (rest stays in the loop body: its labels keep their place)
        # every def is read once, in the condition, where it can be evaluated; a def whose value is also
        # needed later becomes an assignment in the condition (`while ((line = r.readLine()) != null)`, jvm-82)
        saved = dict(self.inline)
        decls = []
        assigned = self.assign_defs(defs, iff.condition)
        if assigned is None:
            self.inline = saved
            return None
        for d in reversed(defs):
            if d.expr_index in assigned:
                continue
            var = d.dest
            if d.operation != Op.HLIL_VAR_INIT or self.var_count(var) != 1 or self.def_count(var) != 1 or \
                    not self.movable(d.src):
                self.inline = saved
                return None
            before, found = self.eval_nodes(iff.condition, var)
            if found is not True or any(self.side_effect(i) for i in before) or \
                    (not self.pure(d.src) and any(self.reads_memory(i) for i in before)):
                self.inline = saved
                return None
            self.inline[var] = d.src
        for d in defs:
            if d.expr_index in assigned:
                target, value, declare = assigned[d.expr_index]
                key = d.dest if d.operation == Op.HLIL_VAR_INIT else d.dest.var
                if isinstance(value, tuple) and value[0] == 'incdec':
                    self.inline[key] = value
                elif value is not None:
                    self.inline[key] = ('assignx', target, value, d)
                if declare:
                    decls.append((target, d))
        self.loop_decls[w.expr_index] = decls
        if do_cond is not None:
            # `x = v; } while (x)`: the test moves to the end of the body as `if (!v) { <code after> }`
            self.inline[x] = asg.src
            self.hidden_vars.add(x)
            self.consumed.add(asg.expr_index)
            self.break_subst[w.expr_index] = ([y for _, y in after], block)
            return defs, iff, tail, rest, ([j for j, _ in after], w.expr_index, do_cond)
        if moved:
            self.break_subst[rest_breaks[0].expr_index] = ([x for _, x in after], block)
        return defs, iff, tail, rest, ([j for j, _ in after], rest_breaks[0].expr_index) if moved else None


    def assign_defs(self, defs, cond):
        """{expr_index of a def: (variable assigned, value or None if folded into another def, declare it
        before the loop)} for the defs before a loop's exit test that become `(x = e)` in its condition:
        `T x = e;` / `x = e;` whose x the condition reads once and the body reads again, and javac's
        `T t = e; x = t;` with t read only by that store and the condition (-> `(x = e)` where t was).
        Plain single-use defs are left to the caller. None when a def can't move into the condition."""
        out = {}
        if not self.assign_in_conditions:
            return out
        for n, d in enumerate(defs):
            var = d.dest if d.operation == Op.HLIL_VAR_INIT else d.dest.var
            nxt = defs[n + 1] if n + 1 < len(defs) else None
            step = self.step_of(nxt, d.src) if nxt is not None and d.operation == Op.HLIL_VAR_INIT else None
            if step is not None and self.var_count(var) == 1 and self.refs(cond, var) == 1 and \
                    self.def_count(var) == 1:
                # `t = x; x = x + 1; if (t ..)` -> `x++` where t was (javac's postfix in a loop test)
                out[d.expr_index] = (var, ('incdec', d.src, step, False, None, None), False)
                out[nxt.expr_index] = (var, None, False)
                continue
            if d.expr_index in out:
                continue
            step = self.step_of(d, d.dest) if d.operation == Op.HLIL_ASSIGN else None
            if step is not None and self.refs(cond, var) == 1:
                out[d.expr_index] = (var, ('incdec', d.dest, step, True, None, None), False)  # ++x
                continue
            if d.operation == Op.HLIL_VAR_INIT and self.var_count(var) == 1:
                continue  # read only by the condition: inlined as it is
            nxt = defs[n + 1] if n + 1 < len(defs) else None
            if d.operation == Op.HLIL_VAR_INIT and nxt is not None and nxt.operation == Op.HLIL_ASSIGN and \
                    nxt.src.operation == Op.HLIL_VAR and nxt.src.var == var and self.var_count(var) == 2 and \
                    self.refs(cond, var) == 1 and self.def_count(var) == 1:
                out[d.expr_index] = (nxt.dest.var, d.src, False)  # t = e; x = t; if (t ..)
                out[nxt.expr_index] = (nxt.dest.var, None, False)
                continue
            if nxt is not None and nxt.expr_index in out:
                continue
            if d.expr_index in out:
                continue
            if self.refs(cond, var) != 1 or not self.movable(d.src):
                return None
            if d.operation == Op.HLIL_VAR_INIT and self.def_count(var) != 1:
                return None
            out[d.expr_index] = (var, d.src, d.operation == Op.HLIL_VAR_INIT and var not in self.hoisted)
        for d in defs:  # the moved value is evaluated where the condition reads it: nothing with effects first
            if d.expr_index not in out or out[d.expr_index][1] is None:
                continue
            if isinstance(out[d.expr_index][1], tuple):
                key = d.dest if d.operation == Op.HLIL_VAR_INIT else d.dest.var
                before, found = self.eval_nodes(cond, key)
                if found is not True or any(self.side_effect(i) for i in before):
                    return None
                continue
            key = d.dest if d.operation == Op.HLIL_VAR_INIT else d.dest.var
            before, found = self.eval_nodes(cond, key)
            if found is not True or any(self.side_effect(i) for i in before) or \
                    (not self.pure(d.src) and any(self.reads_memory(i) for i in before)):
                return None
        return out


    def step_of(self, s, x):
        """'+' / '-' when statement s is `v = v + 1` / `v = v - 1` of the variable expression x, else None"""
        if s is None or self.opaque(s) or s.operation != Op.HLIL_ASSIGN or \
                s.dest.operation != Op.HLIL_VAR or x.operation != Op.HLIL_VAR or s.dest.var != x.var:
            return None
        v = s.src
        if v.operation not in (Op.HLIL_ADD, Op.HLIL_SUB) or v.left.operation != Op.HLIL_VAR or \
                v.left.var != x.var or v.right.operation != Op.HLIL_CONST:
            return None
        c, bits = v.right.constant, 8 * (v.right.size or 4)
        if c >= 1 << (bits - 1):
            c -= 1 << bits  # BN writes a decrement as `x + -1` (0xffffffff)
        if c not in (1, -1):
            return None
        return '+' if (c == 1) == (v.operation == Op.HLIL_ADD) else '-' 


    def labels_internal(self, items):
        """every label in items is only jumped to from items (they can move as a whole, e.g. out of a loop)"""
        labels, gotos = set(), {}
        for x in items:
            for i in walk(self.item_instr(x)):
                if i.operation == Op.HLIL_LABEL:
                    labels.add(i.target.label_id)
                elif i.operation == Op.HLIL_GOTO:
                    gotos[i.target.label_id] = gotos.get(i.target.label_id, 0) + 1
        return all(gotos.get(lid, 0) == self.label_uses(lid) for lid in labels)


    def search_loop_tail(self, breaks, tail, after):
        """the code after a loop can be printed at its single break instead (Vineflower's MergeHelper):
        the loop is only left there and by `tail`, which must not fall through; the code is short, ends the
        method (return / throw) and has no label"""
        if len(breaks) != 1 or not self.ends_flow(tail):
            return None  # (`if (c) { tail; break; }`: tail falls through to that code too)
        items = [x for _, x in after]
        real = [x for x in items if not self.hidden_item(x)]
        if not real or len(real) > 4 or not self.ends_flow(items):
            return None
        for x in items:
            if self.opaque(x):
                continue
            if any(i.operation == Op.HLIL_LABEL for i in walk(x)) or \
                    (x.operation == Op.HLIL_VAR_INIT and x.dest in self.inline):
                return None
        return items


    # --- split short-circuit conditions (jvm-54) ---------------------------------------------------------
    def negation_of(self, a, b):
        """b is the condition !a (integer compare flipped, or a NOT)"""
        if b.operation == Op.HLIL_NOT:
            return str(b.src) == str(a)
        if a.operation == Op.HLIL_NOT:
            return str(a.src) == str(b)
        flip = NEGATED_CMP.get(a.operation)
        return flip is not None and b.operation == flip and str(a.left) == str(b.left) and \
            str(a.right) == str(b.right)


    def merge_split_cond(self, items, k, hi):
        """BN splits `if (!a || p(e))` into `T x; if (a) { x = e; } if (!a || p(x))` (same with `a && p(x)`).
        Print the second test with e in place of x and drop the first: e is still only evaluated when a holds,
        in the same order"""
        s = items[k]
        if self.opaque(s) or s.operation != Op.HLIL_IF or s.expr_index in self.consumed or \
                self.opaque_if(s) or \
                not (s.false is None or s.false.operation in (Op.HLIL_NOP, Op.HLIL_UNREACHABLE)):
            return False
        a = s.condition
        if not self.movable(a) or not self.pure(a):
            return False
        assigns = [x for x in self.items_of(s.true) if not self.hidden_item(x)]
        if not assigns or any(self.opaque(x) or x.operation != Op.HLIL_ASSIGN or
                              x.dest.operation != Op.HLIL_VAR for x in assigns):
            return False
        xs = [x.dest.var for x in assigns]
        if len(set(xs)) != len(xs):
            return False
        j = k + 1
        while j <= hi and self.hidden_item(items[j]):
            j += 1
        if j > hi or self.opaque(items[j]) or items[j].operation != Op.HLIL_IF:
            return False
        c = items[j].condition
        if c.operation not in (Op.HLIL_OR, Op.HLIL_AND) or c.size != 0:
            return False
        if not (self.negation_of(a, c.left) if c.operation == Op.HLIL_OR else str(c.left) == str(a)):
            return False
        r = c.right
        for x, asg in zip(xs, assigns):
            if self.fixed_var(x) or not self.movable(asg.src) or self.refs(c.left, x) or self.refs(r, x) != 1 \
                    or self.var_count(x) - self.refs(s, x) != 1:
                return False
        # Java order: the values are evaluated in assignment order, nothing with an effect before them
        order = [i.var for i in self.eval_nodes(r)[0] if i.operation in (Op.HLIL_VAR, Op.HLIL_VAR_SSA) and i.var in xs]
        if order != xs:
            return False
        for x, asg in zip(xs, assigns):
            before, found = self.eval_nodes(r, x)
            if found is not True:
                return False
            others = [i for i in before if not (i.operation in (Op.HLIL_VAR, Op.HLIL_VAR_SSA) and i.var in xs)]
            if any(self.side_effect(i) for i in others) or \
                    (not self.pure(asg.src) and any(self.reads_memory(i) for i in others)):
                return False
        for x, asg in zip(xs, assigns):
            self.inline[x] = asg.src
            self.hidden_vars.add(x)
        self.consumed.add(s.expr_index)
        for i in walk(a):
            if i.operation == Op.HLIL_VAR:
                self.count_discount[i.var] = self.count_discount.get(i.var, 0) + 1
        return True


    # --- labels -----------------------------------------------------------------------------------------
    def label_uses(self, label_id):
        try:
            return len(self.hlil.get_label_uses(label_id))
        except Exception:
            return -1
