"""Whole JAR loading (jvm-13): unpack a .jar next to itself. The decompiler reads another class from
that directory when it needs one (inner classes, supertypes, varargs). A tab opens only for a class
the user picks from the list. One class is still one view."""
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


def classes_in(dest):
    """(binary name, absolute path) of every .class under an unpacked jar directory"""
    dest = os.path.abspath(dest)
    found = []
    for dirpath, _dirs, files in os.walk(dest):
        for name in files:
            if not name.endswith(".class"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, dest)[:-len(".class")].replace(os.sep, "/")
            found.append((rel, full))
    return sorted(found)


def choose_class(found, title):
    """filterable list of binary names. Returns the chosen path, or None if cancelled."""
    from binaryninja import get_large_choice_input
    if not found:
        return None
    index = get_large_choice_input("Open", title, [name for name, _path in found])
    if index is None:
        return None
    return found[index][1]


def open_class_tab(path):
    """open one .class as a JVM Class tab. Returns True if a window accepted it."""
    from binaryninja import log_error
    from binaryninjaui import UIContext, FileContext
    ctxs = UIContext.allContexts()
    ctx = UIContext.activeContext() or (ctxs[0] if ctxs else None)
    if ctx is None:
        log_error("Whole JAR: no window to open %s in" % path)
        return False
    frame = FileContext.openFilename(path)
    if frame is None:
        log_error("Whole JAR: could not open %s" % path)
        return False
    ctx.openFileContext(frame)
    return True


def load_whole_jar(path=None):
    """ask for a .jar if path is omitted, unpack it, then open the one class the user picks"""
    from binaryninja import get_open_filename_input, log_error, log_info, show_message_box
    try:
        if not path:
            path = get_open_filename_input("Open whole JAR", "*.jar")
            if not path:
                return
        path = os.path.abspath(path)
        try:
            extract_classes(path)
        except (zipfile.BadZipFile, OSError) as e:
            log_error("Whole JAR: %s" % e)
            show_message_box("Whole JAR", "Could not read %s\n%s" % (path, e))
            return
        dest = jar_dest(path)
        found = classes_in(dest)
        if not found:
            log_error("Whole JAR: no .class entries in %s" % path)
            show_message_box("Whole JAR", "No .class entries in %s" % path)
            return
        chosen = choose_class(found, "Whole JAR (%d classes)" % len(found))
        if chosen is None:
            log_info("Whole JAR: unpacked %d classes to %s" % (len(found), dest))
            return
        if open_class_tab(chosen):
            log_info("Whole JAR: opened %s (%d classes unpacked)" % (os.path.basename(chosen), len(found)))
        else:
            show_message_box("Whole JAR", "Unpacked to %s but could not open a tab." % dest)
    except Exception as e:
        log_error("Whole JAR: %s" % e)
        show_message_box("Whole JAR", "Failed:\n%s" % e)


def open_class_in_same_tree(bv):
    """chooser of the other classes in this class's unpacked directory (the decompiler reads those
    files itself; this only opens a tab)"""
    from binaryninja import log_error
    from .classui import class_dir, class_info
    info = class_info(bv)
    root = class_dir(bv, info) if info else None
    if not root or not os.path.isdir(root):
        log_error("Open class: this file is not inside an unpacked JAR directory")
        return
    found = classes_in(root)
    chosen = choose_class(found, "Classes in %s" % os.path.basename(root.rstrip(os.sep)))
    if chosen:
        open_class_tab(chosen)


def _has_tree(bv):
    try:
        from .classui import class_dir, class_info
        info = class_info(bv)
        root = class_dir(bv, info) if info else None
        return bool(root and os.path.isdir(root))
    except Exception:
        return False


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
    # A backslash makes a plugin-menu folder, and PluginCommand.register only runs when a
    # view is open. The open-dialog button has no view, so the action must be global and flat.
    action = "Load Whole JAR..."
    UIAction.registerAction(action)
    UIActionHandler.globalActions().bindAction(action, UIAction(lambda ctx: load_whole_jar()))
    Menu.mainMenu("File").addAction(action, "Open")
    UIContext.registerFileOpenMode(
        "Whole JAR...",
        "Unpack a JAR next to itself. Pick a class to open; the decompiler reads the others from that folder when it needs them.",
        action)
    PluginCommand.register_global(action, "Unpack a JAR, then choose which class to open", load_whole_jar)
    pick = "JVM\\Open class from this JAR..."
    PluginCommand.register(pick, "Choose another class in the same unpacked JAR and open it",
                           open_class_in_same_tree,
                           lambda bv: _has_tree(bv))
