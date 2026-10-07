"""`JVM\\Show class` (jvm-41): the viewed class as one unit -- a Java-like skeleton with header, fields
and methods in source order, built from the "jvm.class" metadata the view stores. Method bodies come
from `pseudo_java.render_body(func)` when that module exists, else from the function's HLIL text.
jvm-62: imports and simple names, generic signatures, `throws`, `@Override` (supertypes from the class files
of the same unpacked jar, else JDK_TYPES), member and anonymous classes from their own class files."""
import os
import re
import traceback

from binaryninja import (PluginCommand, LinearViewObject, LinearViewCursor, LinearDisassemblyLineType,
                         DisassemblySettings, DisassemblyOption, BinaryView, BinaryViewType)

from .classfile import CLASS_METADATA_KEY
from .javatypes import (ACC_STATIC, ACC_ENUM, ACC_SYNTHETIC, ACC_BRIDGE, ACC_PRIVATE, ACC_INTERFACE, class_keyword,
                        java_type_name, modifiers, split_method_descriptor, simple_name, source_class_name,
                        package_of, dotted, class_signature, field_signature, method_signature)
from .constants import ARCH_NAME
try:
    from .pseudo_java import class_supertypes, class_method_flags
except ImportError:
    class_supertypes = class_method_flags = None

# methods of common JDK types a class may override (there is no class file at hand for them):
# binary name -> (superclass, interfaces, {name + descriptor})
JDK_TYPES = {
    "java/lang/Object": (None, [], {"toString()Ljava/lang/String;", "equals(Ljava/lang/Object;)Z", "hashCode()I",
                                    "clone()Ljava/lang/Object;", "finalize()V"}),
    "java/lang/Runnable": (None, [], {"run()V"}),
    "java/lang/Thread": ("java/lang/Object", ["java/lang/Runnable"], {"run()V", "interrupt()V", "start()V"}),
    "java/lang/Comparable": (None, [], {"compareTo(Ljava/lang/Object;)I"}),
    "java/lang/Cloneable": (None, [], set()),
    "java/io/Serializable": (None, [], set()),
    "java/lang/Throwable": ("java/lang/Object", ["java/io/Serializable"],
                            {"getMessage()Ljava/lang/String;", "getLocalizedMessage()Ljava/lang/String;",
                             "getCause()Ljava/lang/Throwable;", "fillInStackTrace()Ljava/lang/Throwable;",
                             "printStackTrace()V", "printStackTrace(Ljava/io/PrintStream;)V",
                             "printStackTrace(Ljava/io/PrintWriter;)V"}),
    "java/lang/Exception": ("java/lang/Throwable", [], set()),
    "java/lang/RuntimeException": ("java/lang/Exception", [], set()),
    "java/lang/Error": ("java/lang/Throwable", [], set()),
    "java/io/IOException": ("java/lang/Exception", [], set()),
    "java/util/Enumeration": (None, [], {"hasMoreElements()Z", "nextElement()Ljava/lang/Object;"}),
    "java/util/Iterator": (None, [], {"hasNext()Z", "next()Ljava/lang/Object;", "remove()V"}),
    "java/util/Comparator": (None, [], {"compare(Ljava/lang/Object;Ljava/lang/Object;)I"}),
    "java/util/EventListener": (None, [], set()),
    "java/io/InputStream": ("java/lang/Object", [], {"read()I", "read([B)I", "read([BII)I", "close()V",
                                                     "available()I", "skip(J)J"}),
    "java/io/OutputStream": ("java/lang/Object", [], {"write(I)V", "write([B)V", "write([BII)V", "flush()V",
                                                      "close()V"}),
}

try:
    from .pseudo_java import render_body  # body only: the skeleton prints the header and braces itself
except ImportError:
    render_body = None
try:
    from .pseudo_java import hoist_field_initializers
except ImportError:
    hoist_field_initializers = None

INDENT = "    "

def class_info(bv):
    try:
        return bv.query_metadata(CLASS_METADATA_KEY)
    except (KeyError, AttributeError):
        return None

BODY_LINE_TYPES = (LinearDisassemblyLineType.CodeDisassemblyLineType, LinearDisassemblyLineType.BlankLineType,
                   LinearDisassemblyLineType.AnalysisWarningLineType)

def hlil_lines(func):
    """the function body as the linear view shows it in Pseudo C (braces/indentation included), without
    the function header line"""
    if func.analysis_skipped:
        return ["// analysis skipped: %s" % func.analysis_skip_reason.name]
    settings = DisassemblySettings()
    settings.set_option(DisassemblyOption.ShowAddress, False)
    cursor = LinearViewCursor(LinearViewObject.single_function_language_representation(func, settings, "Pseudo C"))
    lines = []
    while True:
        lines += ["".join(t.text for t in l.contents.tokens) for l in cursor.lines if l.type in BODY_LINE_TYPES]
        if not cursor.next():
            break
    # the outermost braces belong to the function header, which the skeleton prints itself
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip() == "{":
        lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == "}":
            lines.pop()
    indent = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
    return [l[indent:] for l in lines]

def method_body(func):
    if render_body is not None:
        try:
            return list(render_body(func))
        except Exception:
            return ["// pseudo_java.render_body failed:"] + ["// " + l for l in traceback.format_exc().splitlines()]
    return hlil_lines(func)

def parameter_names(func, count, static):
    """declared parameter names from the function type (named from MethodParameters/LocalVariableTable
    by the view), arg<n> where there is none"""
    names = []
    if func is not None:
        try:
            names = [p.name for p in func.type.parameters]
        except Exception:
            names = []
        if not static:
            names = names[1:]  # this
    return [names[i] if i < len(names) and names[i] else "arg%d" % i for i in range(count)]

def method_header(info, m, func, outer_param=False):
    """outer_param: the first parameter is the outer instance of an inner class (not printed)"""
    static = bool(m["access_flags"] & ACC_STATIC)
    args, ret = split_method_descriptor(m["descriptor"])
    names = parameter_names(func, len(args), static)
    types = [java_type_name(a, short=True) for a in args]
    ret_text, type_params = java_type_name(ret, short=True), ""
    throws = [source_class_name(e.replace(".", "/")) for e in m.get("exceptions", [])]
    if m["signature"]:
        try:
            type_params, g_args, g_ret, g_throws, _ = method_signature(m["signature"])
            if len(g_args) == len(args):  # (inner-class constructors leave out synthetic parameters)
                types = g_args
            ret_text = g_ret
            throws = g_throws or throws
        except (ValueError, IndexError, KeyError):
            pass
    if outer_param and len(types) == len(args):
        types, names = types[1:], names[1:]
    params = ", ".join("%s %s" % (t, n) for t, n in zip(types, names))
    if m["name"] == "<clinit>":
        return "static"
    words = modifiers(m["access_flags"], "method")
    if type_params:
        words.append(type_params)
    if m["name"] == "<init>":
        words.append(simple_name(info["name"].replace(".", "/")).rsplit("$", 1)[-1])
    else:
        words += [ret_text, m["name"]]
    return " ".join(words) + "(" + params + ")" + (" throws " + ", ".join(throws) if throws else "")

def class_dir(bv, info):
    """the directory the class's package tree starts at (an unpacked jar), or None"""
    own = info["name"].replace(".", "/") + ".class"
    path = bv.file.original_filename if bv.file else None
    return path[:-len(own)] if path and path.endswith(own) else None

def supertype_methods(bv, info):
    """name + descriptor of every instance method the class's supertypes declare: from the class files of the
    same unpacked jar, else JDK_TYPES (Object's methods for any other type)"""
    base = class_dir(bv, info)
    queue = [n.replace(".", "/") for n in [info["super"]] + list(info["interfaces"]) if n]
    seen, out = set(), set(JDK_TYPES["java/lang/Object"][2])
    while queue:
        t = queue.pop()
        if t in seen:
            continue
        seen.add(t)
        path = os.path.join(base, t + ".class") if base else None
        if path and os.path.isfile(path) and class_method_flags is not None:
            with open(path, "rb") as fh:
                data = fh.read()
            out.update(n + d for (n, d), f in (class_method_flags(data) or {}).items()
                       if n not in ("<init>", "<clinit>") and not f & (ACC_STATIC | ACC_PRIVATE))
            sup = class_supertypes(data)
            if sup:
                queue += ([sup[0]] if sup[0] else []) + sup[1]
        elif t in JDK_TYPES:
            sup, ifaces, methods = JDK_TYPES[t]
            out.update(methods)
            queue += ([sup] if sup else []) + ifaces
    return out

def is_override(m, inherited, methods):
    if m["name"] in ("<init>", "<clinit>") or m["access_flags"] & (ACC_STATIC | ACC_PRIVATE | ACC_SYNTHETIC):
        return False
    if m["name"] + m["descriptor"] in inherited:
        return True
    # a generic override compiles to a bridge method with the erased descriptor next to it
    n_args = len(split_method_descriptor(m["descriptor"])[0])
    return any(x is not m and x["name"] == m["name"] and x["access_flags"] & ACC_BRIDGE and
               x["access_flags"] & ACC_SYNTHETIC and len(split_method_descriptor(x["descriptor"])[0]) == n_args
               for x in methods)

def imports(info, text, refs=()):
    """`import a.b.C;` lines for the classes the printed text names by their simple name: outside java.lang and
    the class's own package, the outer class for a member class, none for a simple name two classes share"""
    own_pkg = package_of(info["name"].replace(".", "/"))
    code = re.sub(r"//[^\n]*", "", re.sub(r'"(\\.|[^"\\])*"', '""', text))
    words = set(re.findall(r"[A-Za-z_$][\w$]*", code))
    names = set(refs) | set(info.get("referenced", [])) | {info["super"]} | set(info["interfaces"])
    by_simple = {}
    for n in names:
        top = (n or "").replace(".", "/").split("$")[0]
        if "/" not in top or package_of(top) in ("java.lang", own_pkg):
            continue
        by_simple.setdefault(simple_name(top), set()).add(dotted(top))
    return sorted("import %s;" % next(iter(v)) for s, v in by_simple.items() if len(v) == 1 and s in words)

def render_class(bv, nested=None, refs=None):
    """the class skeleton as a list of text lines, member / anonymous classes at its end (from their class files
    next to this one). nested: {flags, anonymous, outer} when rendering such a class; refs collects the
    classes all of them mention (for the imports)"""
    info = class_info(bv)
    if info is None:
        return ["// no JVM class metadata in this view"]
    out = []
    pkg, _, name = info["name"].rpartition(".")
    if nested is None:
        refs = set()
        if info["source_file"]:
            out.append("// source file: %s" % info["source_file"])
        if pkg:
            out += ["package %s;" % pkg, ""]
    imports_at = len(out)
    refs.update(info.get("referenced", []))
    flags = info["access_flags"]
    if nested is not None:
        # the InnerClasses flags hold what the source declared (static, private, protected)
        flags = nested["flags"] | (flags & ACC_INTERFACE)
        if nested["anonymous"]:
            out.append("// anonymous class")
    sup = source_class_name(info["super"].replace(".", "/")) if info["super"] else ""
    ifaces = [source_class_name(i.replace(".", "/")) for i in info["interfaces"]]
    type_params = ""
    if info["signature"]:
        try:
            type_params, sup, ifaces, _ = class_signature(info["signature"])
        except (ValueError, IndexError, KeyError):
            out.append("// generic signature: %s" % info["signature"])
    shown = name if nested is not None and nested["anonymous"] else name.rsplit("$", 1)[-1]
    header = modifiers(flags, "class") + [class_keyword(flags), shown + type_params]
    if sup and sup != "Object" and not flags & ACC_ENUM:
        header += ["extends", sup]
    if ifaces:
        header += ["implements" if class_keyword(flags) == "class" or class_keyword(flags) == "enum" else "extends",
                   ", ".join(ifaces)]
    out.append(" ".join(header) + " {")
    if info["fields"]:
        out.append("")
    funcs = {id(m): (bv.get_function_at(m["address"]) if m["address"] else None) for m in info["methods"]}
    bodies = {id(m): method_body(funcs[id(m)]) for m in info["methods"] if funcs[id(m)] is not None}
    inits = {}
    if hoist_field_initializers is not None and render_body is not None:
        # field initialisers back on the fields (jvm-52); an emptied static {} / default constructor goes away
        ctors = [m for m in info["methods"] if m["name"] == "<init>" and id(m) in bodies]
        clinit = [m for m in info["methods"] if m["name"] == "<clinit>" and id(m) in bodies]
        try:
            inits, new_ctors, new_clinit = hoist_field_initializers(
                simple_name(info["name"].replace(".", "/")).rsplit("$", 1)[-1],
                [(f["name"], bool(f["access_flags"] & ACC_STATIC)) for f in info["fields"]],
                [bodies[id(m)] for m in ctors], bodies[id(clinit[0])] if clinit else None)
            for m, b in zip(ctors, new_ctors):
                bodies[id(m)] = b
            if clinit:
                bodies[id(clinit[0])] = new_clinit
        except Exception:
            inits = {}
    hidden = set()
    if inits:
        access = 0x0001 | 0x0002 | 0x0004  # public / private / protected: Java's default constructor has the class's
        for m in info["methods"]:
            if id(m) in bodies and not any(l.strip() for l in bodies[id(m)]) and \
                    (m["name"] == "<clinit>" or (m["name"] == "<init>" and m["descriptor"] == "()V" and
                     m["access_flags"] & access == flags & access and
                     sum(1 for x in info["methods"] if x["name"] == "<init>") == 1)):
                hidden.add(id(m))
    for f in info["fields"]:
        if nested is not None and f["access_flags"] & ACC_SYNTHETIC:
            continue  # this$0, val$x: the outer instance and captured locals
        ftype = java_type_name(f["descriptor"], short=True)
        if f["signature"]:
            try:
                ftype = field_signature(f["signature"])[0]
            except (ValueError, IndexError, KeyError):
                out.append(INDENT + "// generic signature: %s" % f["signature"])
        words = modifiers(f["access_flags"], "field") + [ftype, f["name"]]
        value = inits.get(f["name"]) or f.get("constant")  # hoisted initialiser / ConstantValue attribute
        init = " = " + value if value else ""
        out.append(INDENT + " ".join(words) + init + ";" +
                   ("  // synthetic" if f["access_flags"] & ACC_SYNTHETIC else ""))
    inherited = supertype_methods(bv, info)
    for m in info["methods"]:
        if id(m) in hidden or (m["access_flags"] & ACC_BRIDGE and m["access_flags"] & ACC_SYNTHETIC) or \
                (nested is not None and m["access_flags"] & ACC_SYNTHETIC):
            continue  # (a bridge method only forwards to the generic method it was made for)
        func = funcs[id(m)]
        out.append("")
        if is_override(m, inherited, info["methods"]):
            out.append(INDENT + "@Override")
        # an inner (not static) member class's constructors take the outer instance first
        outer_param = nested is not None and not nested["anonymous"] and not flags & ACC_STATIC and \
            m["name"] == "<init>" and split_method_descriptor(m["descriptor"])[0][:1] == \
            ["L%s;" % nested["outer"].replace(".", "/")]
        head = method_header(info, m, func, outer_param)
        if func is None:
            out.append(INDENT + head + ";")
            continue
        out.append(INDENT + head + " {  // %s @ 0x%x" % (func.name, func.start))
        out += [INDENT * 2 + line for line in bodies[id(m)]]
        out.append(INDENT + "}")
    for e in info["inner_classes"]:
        inner = e["inner"]
        member = e["outer"] == info["name"] and e["name"]
        local = not e["outer"] and inner and inner.rsplit("$", 1)[0] == info["name"]  # anonymous / local class
        if inner == info["name"] or not (member or local):
            continue
        iv = open_sibling(bv, info, inner)
        if iv is None:
            out += ["", INDENT + "// inner class %s (no class file next to this one)" % inner]
            continue
        try:
            lines = render_class(iv, {"flags": e["access_flags"], "anonymous": not e["name"],
                                      "outer": info["name"]}, refs)
        finally:
            iv.file.close()
        out.append("")
        out += [INDENT + l if l else l for l in lines]
    out.append("}")
    if nested is None:
        lines = imports(info, "\n".join(out), refs)
        if lines:
            out[imports_at:imports_at] = lines + [""]
    return out

def open_sibling(bv, info, binary_name):
    """a view of another class file of the same unpacked jar (an inner class), analysed; None if missing"""
    base = class_dir(bv, info)
    path = os.path.join(base, binary_name.replace(".", "/") + ".class") if base else None
    if not path or not os.path.isfile(path):
        return None
    try:
        v = BinaryViewType[bv.view_type].create(BinaryView.open(path))
        v.update_analysis_and_wait()
        return v
    except Exception:
        return None

def show_class(bv):
    info = class_info(bv)
    title = info["name"] if info else "JVM class"
    bv.show_plain_text_report(title, "\n".join(render_class(bv)))

def register_commands():
    name = "JVM\\Show class" if ARCH_NAME == "JVM" else "JVM\\Show class (%s)" % ARCH_NAME  # dev loads
    PluginCommand.register(name, "Show the class as a Java-like skeleton (header, fields, methods)",
                           show_class, lambda bv: class_info(bv) is not None)
