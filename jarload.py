"""Whole JAR loading (jvm-13): unpack a .jar next to itself and open every class with the existing
JVM Class view. One class is still one view; this is only another way to get those files open.
Sibling lookup (inner classes, supertypes) keeps reading .class files from that directory."""
import os
import zipfile


def jar_dest(jar_path):
    """foo.jar -> the directory foo next to it (the unpack convention)."""
    base = jar_path[:-4] if jar_path.lower().endswith(".jar") else jar_path + "_classes"
    return os.path.abspath(base)


def _dest_path(root, name):
    """a zip entry's path inside root, or None if it would escape root"""
    rel = os.path.normpath(name.replace("\\", "/"))
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    full = os.path.normpath(os.path.join(root, rel))
    root_prefix = os.path.normpath(root) + os.sep
    if full != os.path.normpath(root) and not full.startswith(root_prefix):
        return None
    return full


def class_entries(jar_path):
    """'.class' members of a zip, directories and nested jars left out"""
    with zipfile.ZipFile(jar_path) as zf:
        return [n for n in zf.namelist() if n.endswith(".class") and not n.endswith("/")]


def extract_classes(jar_path, dest=None):
    """write the jar's .class entries under dest (default: beside the jar). Returns their paths."""
    dest = os.path.abspath(dest or jar_dest(jar_path))
    os.makedirs(dest, exist_ok=True)
    written = []
    with zipfile.ZipFile(jar_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".class") or name.endswith("/"):
                continue
            path = _dest_path(dest, name)
            if path is None:
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with zf.open(name) as src, open(path, "wb") as out:
                out.write(src.read())
            written.append(path)
    return sorted(written)


def load_whole_jar(path=None):
    """ask for a .jar if path is omitted, unpack it, and open every class in the UI"""
    from binaryninja import get_open_filename_input, log_error, log_info
    if not path:
        path = get_open_filename_input("Open whole JAR", "*.jar")
        if not path:
            return
    path = os.path.abspath(path)
    try:
        classes = extract_classes(path)
    except (zipfile.BadZipFile, OSError) as e:
        log_error("Whole JAR: %s" % e)
        return
    if not classes:
        log_error("Whole JAR: no .class entries in %s" % path)
        return
    from binaryninjaui import UIContext, FileContext
    ctx = UIContext.activeContext()
    if ctx is None:
        log_error("Whole JAR: no window to open classes in")
        return
    opened = 0
    for filename in classes:
        frame = FileContext.openFilename(filename)
        if frame is None:
            log_error("Whole JAR: could not open %s" % filename)
            continue
        ctx.openFileContext(frame)
        opened += 1
    log_info("Whole JAR: opened %d classes from %s" % (opened, path))


def register():
    """the installed plugin only: dev loads keep their own class view and must not add the mode again"""
    from .constants import ARCH_NAME
    if ARCH_NAME != "JVM":
        return
    try:
        from binaryninja import PluginCommand
        from binaryninjaui import UIContext, UIAction, UIActionHandler, Menu
    except Exception:
        return
    name = "JVM\\Load whole jar..."
    UIAction.registerAction(name)
    UIActionHandler.globalActions().bindAction(name, UIAction(lambda ctx: load_whole_jar()))
    Menu.mainMenu("File").addAction(name, "Open")
    UIContext.registerFileOpenMode(
        "Whole JAR...",
        "Unpack a JAR next to itself and open every class with the JVM Class view.",
        name)
    PluginCommand.register(name, "Unpack a JAR and open every .class in its own view",
                           lambda bv: load_whole_jar(), lambda bv: True)
