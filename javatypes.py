"""Java descriptors, names and modifiers as Java source text -- pure Python, no Binary Ninja import."""

PRIMITIVE_NAMES = {'B': "byte", 'C': "char", 'D': "double", 'F': "float", 'I': "int", 'J': "long",
                   'S': "short", 'Z': "boolean", 'V': "void"}

def dotted(binary_name):
    """'java/lang/String' -> 'java.lang.String' (inner classes keep their '$')"""
    return binary_name.replace("/", ".")

def simple_name(binary_name):
    """'java/lang/String' -> 'String'"""
    return dotted(binary_name).rsplit(".", 1)[-1]

def package_of(binary_name):
    """'com/foo/Bar' -> 'com.foo'; '' for the default package"""
    d = dotted(binary_name)
    return d.rsplit(".", 1)[0] if "." in d else ""

def descriptor_end(desc, i):
    """index just past the field descriptor starting at desc[i]"""
    while desc[i] == '[':
        i += 1
    if desc[i] == 'L':
        return desc.index(';', i) + 1
    return i + 1

def split_method_descriptor(desc):
    """'(I[JLjava/lang/String;)V' -> (['I', '[J', 'Ljava/lang/String;'], 'V')"""
    args = []
    i = desc.index('(') + 1
    while desc[i] != ')':
        end = descriptor_end(desc, i)
        args.append(desc[i:end])
        i = end
    return args, desc[i+1:]

def slot_count(desc):
    """local-variable slots taken by a value of this field descriptor"""
    return 2 if desc[0] in "JD" else 1

def java_type_name(desc, short=False):
    """'[Ljava/lang/String;' -> 'java.lang.String[]' ('String[]' with short=True, 'Map.Entry' for a member
    class), 'I' -> 'int'"""
    dims = 0
    while desc[dims] == '[':
        dims += 1
    base = desc[dims:]
    if base[0] == 'L':
        name = base[1:-1]
        name = source_class_name(name) if short else dotted(name)
    else:
        name = PRIMITIVE_NAMES.get(base[0], base)
    return name + "[]" * dims

def source_class_name(binary_name):
    """'java/util/Map$Entry' -> 'Map.Entry' (simple name as Java source writes a member class)"""
    return simple_name(binary_name).replace("$", ".")

# generic signatures (JVMS 4.7.9.1): each function also collects the binary names of the classes it mentions
def _sig_type(sig, i, names):
    """(Java text, index after) of the type signature at sig[i]"""
    c = sig[i]
    if c == '[':
        t, j = _sig_type(sig, i + 1, names)
        return t + "[]", j
    if c == 'T':
        j = sig.index(';', i)
        return sig[i + 1:j], j + 1
    if c != 'L':
        return PRIMITIVE_NAMES[c], i + 1
    parts, cls, j = [], "", i + 1
    while True:
        k = j
        while sig[k] not in "<;.":
            k += 1
        seg = sig[j:k]
        cls = cls + "$" + seg if cls else seg
        text = source_class_name(seg) if not parts else seg
        if sig[k] == '<':
            args, k = [], k + 1
            while sig[k] != '>':
                if sig[k] == '*':
                    args.append("?")
                    k += 1
                elif sig[k] in "+-":
                    t, k2 = _sig_type(sig, k + 1, names)
                    args.append(("? extends " if sig[k] == '+' else "? super ") + t)
                    k = k2
                else:
                    t, k = _sig_type(sig, k, names)
                    args.append(t)
            text += "<" + ", ".join(args) + ">"
            k += 1
        parts.append(text)
        if sig[k] == '.':
            j = k + 1
            continue
        names.append(cls)
        return ".".join(parts), k + 1

def _type_params(sig, i, names):
    """('<T, U extends Comparable<U>>', index after) when sig[i] starts formal type parameters, else ('', i)"""
    if i >= len(sig) or sig[i] != '<':
        return "", i
    out, i = [], i + 1
    while sig[i] != '>':
        j = sig.index(':', i)
        name, i, bounds = sig[i:j], j, []
        while sig[i] == ':':
            i += 1
            if sig[i] == ':':
                continue  # no class bound, an interface bound follows
            t, i = _sig_type(sig, i, names)
            bounds.append(t)
        bounds = [b for b in bounds if b != "Object"]
        out.append(name + (" extends " + " & ".join(bounds) if bounds else ""))
    return "<" + ", ".join(out) + ">", i + 1

def class_signature(sig):
    """'<T:Ljava/lang/Object;>Ljava/util/Vector<TT;>;Lx/I;' -> ('<T>', 'Vector<T>', ['I'], binary names)"""
    names = []
    params, i = _type_params(sig, 0, names)
    sup, i = _sig_type(sig, i, names)
    ifaces = []
    while i < len(sig):
        t, i = _sig_type(sig, i, names)
        ifaces.append(t)
    return params, sup, ifaces, names

def field_signature(sig):
    """'Ljava/util/Hashtable<Ljava/lang/String;Lx/Y;>;' -> ('Hashtable<String, Y>', binary names)"""
    names = []
    return _sig_type(sig, 0, names)[0], names

def method_signature(sig):
    """'<T:..>(TT;I)Ljava/util/List<TT;>;^Lx/E;' -> ('<T>', ['T', 'int'], 'List<T>', ['E'], binary names)"""
    names = []
    params, i = _type_params(sig, 0, names)
    i += 1  # (
    args = []
    while sig[i] != ')':
        t, i = _sig_type(sig, i, names)
        args.append(t)
    ret, i = _sig_type(sig, i + 1, names)
    throws = []
    while i < len(sig) and sig[i] == '^':
        t, i = _sig_type(sig, i + 1, names)
        throws.append(t)
    return params, args, ret, throws, names

def method_parameter_slots(desc, static):
    """[(slot, field descriptor)] of the declared parameters (without `this`)"""
    args, _ = split_method_descriptor(desc)
    slot = 0 if static else 1
    result = []
    for a in args:
        result.append((slot, a))
        slot += slot_count(a)
    return result

# access flags (JVMS 4.1, 4.5, 4.6, 4.7.6)
ACC_PUBLIC, ACC_PRIVATE, ACC_PROTECTED, ACC_STATIC, ACC_FINAL = 0x0001, 0x0002, 0x0004, 0x0008, 0x0010
ACC_SYNCHRONIZED, ACC_VOLATILE, ACC_BRIDGE, ACC_TRANSIENT, ACC_VARARGS = 0x0020, 0x0040, 0x0040, 0x0080, 0x0080
ACC_NATIVE, ACC_INTERFACE, ACC_ABSTRACT, ACC_STRICT, ACC_SYNTHETIC = 0x0100, 0x0200, 0x0400, 0x0800, 0x1000
ACC_ANNOTATION, ACC_ENUM = 0x2000, 0x4000

def modifiers(flags, kind):
    """Java modifier keywords for a 'class', 'field' or 'method' access-flag word"""
    words = []
    for bit, word in ((ACC_PUBLIC, "public"), (ACC_PRIVATE, "private"), (ACC_PROTECTED, "protected"),
                      (ACC_STATIC, "static")):
        if flags & bit:
            words.append(word)
    if kind == "class":
        if flags & ACC_ABSTRACT and not flags & ACC_INTERFACE: words.append("abstract")
        if flags & ACC_FINAL: words.append("final")
    elif kind == "field":
        if flags & ACC_FINAL: words.append("final")
        if flags & ACC_TRANSIENT: words.append("transient")
        if flags & ACC_VOLATILE: words.append("volatile")
    else:
        if flags & ACC_ABSTRACT: words.append("abstract")
        if flags & ACC_FINAL: words.append("final")
        if flags & ACC_SYNCHRONIZED: words.append("synchronized")
        if flags & ACC_NATIVE: words.append("native")
        if flags & ACC_STRICT: words.append("strictfp")
    return words

def class_keyword(flags):
    if flags & ACC_ANNOTATION: return "@interface"
    if flags & ACC_INTERFACE: return "interface"
    if flags & ACC_ENUM: return "enum"
    return "class"
