# shared with binary-cpp (moved here from binary-jvm/pseudo_java.py, jvm-80 / jvm-68; binary-cpp: not yet, bncpp-108);
# keep language-neutral and pure Python
"""Condition graphs (jvm-80): an if/goto tree over shared outcomes, reduced to one condition. A graph is
{id: ('dec', cond, t, f) | ('leaf', key, items)}; a condition is an HLIL condition or ('not' | 'and' |
'or' | 'tern', ...). Conditions are told apart by str() (the same text is the same condition: every
decision runs before any statement, so it sees the same state).
"""


def cg_reduce(nodes, root):
    """collapse decisions into &&, || and ?: (in place); the reduced root"""
    def preds():
        count = {}
        seen, todo = set(), [root]
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            if nodes[n][0] == 'dec':
                for c in nodes[n][2:4]:
                    count[c] = count.get(c, 0) + 1
                    todo.append(c)
        return count, seen
    changed = True
    while changed:
        changed = False
        count, live = preds()
        for n in sorted(live):
            e = nodes[n]
            if e[0] != 'dec':
                continue
            _, c, t, f = e
            te, fe = nodes[t], nodes[f]
            if te[0] == 'dec' and count.get(t) == 1:
                if te[3] == f:
                    nodes[n] = ('dec', ('and', c, te[1]), te[2], f)
                elif te[2] == f:
                    nodes[n] = ('dec', ('and', c, ('not', te[1])), te[3], f)
                if nodes[n] is not e:
                    changed = True
                    break
            if fe[0] == 'dec' and count.get(f) == 1:
                if fe[2] == t:
                    nodes[n] = ('dec', ('or', c, fe[1]), t, fe[3])
                elif fe[3] == t:
                    nodes[n] = ('dec', ('or', c, ('not', fe[1])), t, fe[2])
                if nodes[n] is not e:
                    changed = True
                    break
            if te[0] == 'dec' and fe[0] == 'dec' and count.get(t) == 1 and count.get(f) == 1:
                if (te[2], te[3]) == (fe[2], fe[3]):
                    nodes[n] = ('dec', ('tern', c, te[1], fe[1]), te[2], te[3])
                elif (te[2], te[3]) == (fe[3], fe[2]):
                    nodes[n] = ('dec', ('tern', c, te[1], ('not', fe[1])), te[2], te[3])
                if nodes[n] is not e:
                    changed = True
                    break
    return nodes[root]


def cg_verify(nodes, root, top):
    """the reduced condition picks the same outcome as the decision graph for every truth assignment
    of its basic conditions (all of them up to 10 conditions, else 512 random ones)"""
    import itertools, random
    conds = {}
    for e in nodes.values():
        if e[0] == 'dec':
            conds.setdefault(str(e[1]), len(conds))

    def walk(assign):
        n = root
        while nodes[n][0] == 'dec':
            n = nodes[n][2] if assign[conds[str(nodes[n][1])]] else nodes[n][3]
        return n

    def value(e, assign):
        if isinstance(e, tuple):
            if e[0] == 'not':
                return not value(e[1], assign)
            if e[0] == 'and':
                return value(e[1], assign) and value(e[2], assign)
            if e[0] == 'or':
                return value(e[1], assign) or value(e[2], assign)
            return value(e[2], assign) if value(e[1], assign) else value(e[3], assign)
        return assign[conds[str(e)]]

    n = len(conds)
    rng = random.Random(80)
    cases = itertools.product((False, True), repeat=n) if n <= 10 else \
        ([rng.random() < 0.5 for _ in range(n)] for _ in range(512))
    for assign in cases:
        if walk(assign) != (top[2] if value(top[1], assign) else top[3]):
            return False
    return True
