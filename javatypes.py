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
    """'[Ljava/lang/String;' -> 'java.lang.String[]' ('String[]' with short=True), 'I' -> 'int'"""
    dims = 0
    while desc[dims] == '[':
        dims += 1
    base = desc[dims:]
    if base[0] == 'L':
        name = base[1:-1]
        name = simple_name(name) if short else dotted(name)
    else:
        name = PRIMITIVE_NAMES.get(base[0], base)
    return name + "[]" * dims

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
