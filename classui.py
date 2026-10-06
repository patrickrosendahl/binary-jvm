"""`JVM\\Show class` (jvm-41): the viewed class as one unit -- a Java-like skeleton with header, fields
and methods in source order, built from the "jvm.class" metadata the view stores. Method bodies come
from `pseudo_java.render_method(func)` when that module exists, else from the function's HLIL text."""
import traceback

from binaryninja import (PluginCommand, LinearViewObject, LinearViewCursor, LinearDisassemblyLineType,
                         DisassemblySettings, DisassemblyOption)

from .classfile import CLASS_METADATA_KEY
from .javatypes import (ACC_STATIC, ACC_ENUM, ACC_SYNTHETIC, class_keyword, java_type_name, modifiers,
                        split_method_descriptor, simple_name)
from .constants import ARCH_NAME

try:
    from .pseudo_java import render_method
except ImportError:
    render_method = None

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
    if render_method is not None:
        try:
            return list(render_method(func))
        except Exception:
            return ["// pseudo_java.render_method failed:"] + ["// " + l for l in traceback.format_exc().splitlines()]
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

def method_header(info, m, func):
    static = bool(m["access_flags"] & ACC_STATIC)
    args, ret = split_method_descriptor(m["descriptor"])
    names = parameter_names(func, len(args), static)
    params = ", ".join("%s %s" % (java_type_name(a, short=True), n) for a, n in zip(args, names))
    if m["name"] == "<clinit>":
        return "static"
    words = modifiers(m["access_flags"], "method")
    if m["name"] == "<init>":
        words.append(simple_name(info["name"].replace(".", "/")).rsplit("$", 1)[-1])
    else:
        words += [java_type_name(ret, short=True), m["name"]]
    return " ".join(words) + "(" + params + ")"

def render_class(bv):
    """the class skeleton as a list of text lines"""
    info = class_info(bv)
    if info is None:
        return ["// no JVM class metadata in this view"]
    out = []
    if info["source_file"]:
        out.append("// source file: %s" % info["source_file"])
    pkg, _, name = info["name"].rpartition(".")
    if pkg:
        out += ["package %s;" % pkg, ""]
    if info["signature"]:
        out.append("// generic signature: %s" % info["signature"])
    flags = info["access_flags"]
    header = modifiers(flags, "class") + [class_keyword(flags), name]
    if info["super"] and info["super"] != "java.lang.Object" and not flags & ACC_ENUM:
        header += ["extends", info["super"]]
    if info["interfaces"]:
        header += ["implements" if class_keyword(flags) == "class" or class_keyword(flags) == "enum" else "extends",
                   ", ".join(info["interfaces"])]
    out.append(" ".join(header) + " {")
    for e in info["inner_classes"]:
        if e["outer"] == info["name"]:
            out.append(INDENT + "// inner class %s" % e["inner"])
    if info["fields"]:
        out.append("")
    for f in info["fields"]:
        if f["signature"]:
            out.append(INDENT + "// generic signature: %s" % f["signature"])
        words = modifiers(f["access_flags"], "field") + [java_type_name(f["descriptor"], short=True), f["name"]]
        out.append(INDENT + " ".join(words) + ";" + ("  // synthetic" if f["access_flags"] & ACC_SYNTHETIC else ""))
    for m in info["methods"]:
        func = bv.get_function_at(m["address"]) if m["address"] else None
        out.append("")
        if m["signature"]:
            out.append(INDENT + "// generic signature: %s" % m["signature"])
        head = method_header(info, m, func)
        if func is None:
            out.append(INDENT + head + ";")
            continue
        out.append(INDENT + head + " {  // %s @ 0x%x" % (func.name, func.start))
        out += [INDENT * 2 + line for line in method_body(func)]
        out.append(INDENT + "}")
    out.append("}")
    return out

def show_class(bv):
    info = class_info(bv)
    title = info["name"] if info else "JVM class"
    bv.show_plain_text_report(title, "\n".join(render_class(bv)))

def register_commands():
    name = "JVM\\Show class" if ARCH_NAME == "JVM" else "JVM\\Show class (%s)" % ARCH_NAME  # dev loads
    PluginCommand.register(name, "Show the class as a Java-like skeleton (header, fields, methods)",
                           show_class, lambda bv: class_info(bv) is not None)
