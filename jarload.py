"""Whole JAR loading (jvm-13, jvm-15): unpack a .jar next to itself. The decompiler reads another class
from that directory when it needs one (inner classes, supertypes, varargs). Tabs open only for the classes
the user picks from the list (several at once, jvm-76). Main-Class from META-INF/MANIFEST.MF is preselected.
Nested .jar entries can be unpacked the same way (foo.jar entry lib/bar.jar -> foo/lib/bar/). Other
non-class entries are skipped. One class is still one view; opening a .class never comes through here."""
import io
import os
import zipfile
from collections import namedtuple


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


def _unsafe(name):
    """True if a zip entry would land outside the unpack directory"""
    return _dest_path("/jarroot", name) is None


def classify_names(names):
    """zip entry names -> (class entries, nested .jar entries, other files, unsafe entries).
    Directories are ignored. .jar is matched case-insensitively; .class stays case-sensitive,
    matching what extract_classes has always written."""
    classes, nested, other, unsafe = [], [], [], []
    for name in names:
        if name.endswith("/"):
            continue
        if _unsafe(name):
            unsafe.append(name)
            continue
        if name.endswith(".class"):
            classes.append(name)
        elif name.lower().endswith(".jar"):
            nested.append(name)
        else:
            other.append(name)
    return sorted(classes), sorted(nested), sorted(other), sorted(unsafe)


def manifest_main_attributes(data):
    """main section of a MANIFEST.MF (bytes or str) as {header: value}. Continuation lines (a leading
    space) are joined onto the previous value. The first blank line ends the section, so per-entry
    sections cannot override Main-Class. A repeated header keeps the last value."""
    if not isinstance(data, str):
        data = data.decode("utf-8", "replace")
    text = data.replace("\r\n", "\n").replace("\r", "\n")
    attrs = {}
    name = None
    buf = None

    def commit():
        nonlocal name, buf
        if name is not None:
            for old in list(attrs):
                if old.lower() == name.lower():
                    del attrs[old]
                    break
            attrs[name] = buf
        name = buf = None

    for line in text.split("\n"):
        if line == "":
            commit()
            break
        if line.startswith(" "):
            if name is not None:
                buf += line[1:]
            continue
        if ":" not in line:
            continue
        commit()
        raw_name, raw_value = line.split(":", 1)
        name = raw_name
        buf = raw_value[1:] if raw_value.startswith(" ") else raw_value
    else:
        commit()
    return attrs


def _manifest_entry(names):
    exact = "META-INF/MANIFEST.MF"
    if exact in names:
        return exact
    for name in names:
        if name.upper() == exact:
            return name
    return None


def main_class_binary(value):
    """manifest Main-Class value -> binary name (com.example.Main -> com/example/Main), or None.
    A trailing .class is stripped. A value that already uses slashes is kept."""
    value = (value or "").strip()
    if value.endswith(".class"):
        value = value[:-len(".class")].strip()
    if not value:
        return None
    if "/" not in value and "\\" not in value:
        value = value.replace(".", "/")
    value = value.replace("\\", "/")
    if _unsafe(value) or value.endswith("/"):
        return None
    return value


def main_class_of(attributes):
    """binary name of the Main-Class header, or None"""
    for key, value in attributes.items():
        if key.lower() == "main-class":
            return main_class_binary(value)
    return None


JarScan = namedtuple("JarScan", "classes nested other unsafe attributes")


def scan_jar(jar_path):
    """classify a jar's entries and read its main manifest attributes. Does not write anything."""
    with zipfile.ZipFile(jar_path) as zf:
        names = zf.namelist()
        classes, nested, other, unsafe = classify_names(names)
        manifest = _manifest_entry(names)
        attributes = manifest_main_attributes(zf.read(manifest)) if manifest and not _unsafe(manifest) else {}
        return JarScan(classes, nested, other, unsafe, attributes)


def skipped_summary(other, unsafe):
    """log body for non-class entries that are not unpacked, or None when there is nothing to say.
    Nested JARs are not included: the user is asked about those separately."""
    bits = []
    if other:
        bits.append("%d non-class resource%s" % (len(other), "" if len(other) == 1 else "s"))
    if unsafe:
        bits.append("%d entr%s outside the unpack directory" % (len(unsafe), "y" if len(unsafe) == 1 else "ies"))
    if not bits:
        return None
    return "skipped " + ", ".join(bits)


def nested_left_message(left):
    """log body listing nested JARs the user did not ask to unpack"""
    if len(left) <= 8:
        return "left nested JARs packed: %s" % ", ".join(left)
    return "left %d nested JARs packed: %s, ..." % (len(left), ", ".join(left[:8]))


def _extract_named(zf, dest, names):
    """write the named zip entries under dest. Entries that would escape dest are skipped."""
    os.makedirs(dest, exist_ok=True)
    written = []
    for name in names:
        path = _dest_path(dest, name)
        if path is None:
            continue
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with zf.open(name) as src, open(path, "wb") as out:
            out.write(src.read())
        written.append(path)
    return sorted(written)


def extract_classes(jar_path, dest=None):
    """write the jar's .class entries under dest (default: beside the jar). Returns their paths."""
    dest = os.path.abspath(dest or jar_dest(jar_path))
    with zipfile.ZipFile(jar_path) as zf:
        classes, _nested, _other, _unsafe = classify_names(zf.namelist())
        return _extract_named(zf, dest, classes)


def nested_unpack_dir(outer_jar, entry, dest_root=None):
    """foo.jar + 'lib/bar.jar' -> <foo>/lib/bar. None if entry would escape the unpack directory."""
    root = os.path.abspath(dest_root or jar_dest(outer_jar))
    rel = entry.replace("\\", "/")
    if rel.lower().endswith(".jar"):
        rel = rel[:-4]
    return _dest_path(root, rel)


def unpack_nested_jar(outer_jar, entry, dest_root=None):
    """unpack one nested jar entry's .class files into nested_unpack_dir.
    Returns (written paths, nested jar names inside it, unsafe entry names inside it).
    Deeper nested JARs are reported and left packed. Raises ValueError if entry escapes,
    BadZipFile if the entry is not a zip, KeyError if entry is absent."""
    dest = nested_unpack_dir(outer_jar, entry, dest_root)
    if dest is None:
        raise ValueError("nested JAR escapes the unpack directory: %s" % entry)
    with zipfile.ZipFile(outer_jar) as outer:
        data = outer.read(entry)
    with zipfile.ZipFile(io.BytesIO(data)) as inner:
        classes, nested, _other, unsafe = classify_names(inner.namelist())
        written = _extract_named(inner, dest, classes)
    return written, nested, unsafe


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


def find_main_class(found, binary):
    """binary name in `found` to preselect for Main-Class, or None.
    An exact binary name wins. Otherwise a single class whose name ends with /<binary>
    (a class that arrived inside an unpacked nested JAR) is used. Several such classes: no preselect."""
    if not binary:
        return None
    names = [name for name, _path in found]
    if binary in names:
        return binary
    suffix = "/" + binary
    hits = [name for name in names if name.endswith(suffix)]
    if len(hits) == 1:
        return hits[0]
    return None


def main_class_note(binary, match):
    """chooser label / log body for a detected Main-Class. `match` is find_main_class's result."""
    dotted = binary.replace("/", ".")
    if match is None:
        return "Main-Class: %s (not among these classes)" % dotted
    if match == binary:
        return "Main-Class: %s" % dotted
    return "Main-Class: %s (%s)" % (dotted, match)


def manifest_main_class(root):
    """Main-Class binary name for an unpacked directory whose .jar sits beside it, or None."""
    jar = os.path.abspath(root).rstrip(os.sep) + ".jar"
    if not os.path.isfile(jar):
        return None
    try:
        return main_class_of(scan_jar(jar).attributes)
    except (zipfile.BadZipFile, OSError):
        return None


MANY_TABS = 10  # opening more tabs than this at once asks first (each one analyses its class)


def _choice_fallback(found, title, main_class, main_label):
    """BN's single-choice list, used when Qt isn't available. The Main-Class entry is first."""
    from binaryninja import get_large_choice_input
    ordered = list(found)
    if main_class:
        ordered.sort(key=lambda item: 0 if item[0] == main_class else 1)
    prompt = title if not main_label else "%s\n%s" % (title, main_label)
    index = get_large_choice_input("Open", prompt, [name for name, _path in ordered])
    return [] if index is None else [ordered[index][1]]


def choose_classes(found, title, main_class=None, main_label=None):
    """filterable multi-select list of binary names (Shift / Cmd click). Returns the chosen paths ([] if
    cancelled). Inner classes ($) are hidden unless asked for: the class view prints them inside their
    outer class. main_class (a binary name from find_main_class) is selected. main_label is shown above
    the list. Falls back to BN's single-choice list without Qt"""
    if not found:
        return []
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QLabel, QLineEdit,
                                       QListWidget, QListWidgetItem, QVBoxLayout)
    except ImportError:
        return _choice_fallback(found, title, main_class, main_label)

    dialog = QDialog()
    dialog.setWindowTitle(title)
    dialog.resize(640, 560)
    layout = QVBoxLayout(dialog)
    if main_label:
        banner = QLabel(main_label)
        banner.setWordWrap(True)
        layout.addWidget(banner)
    flt = QLineEdit()
    flt.setPlaceholderText("Filter (part of the name, e.g. log/Logger)")
    inner = QCheckBox("Show inner and anonymous classes")
    lst = QListWidget()
    lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
    for name, path in found:
        item = QListWidgetItem(name)
        item.setData(Qt.UserRole, path)
        lst.addItem(item)
    status = QLabel()
    buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
    for w in (flt, inner, lst, status, buttons):
        layout.addWidget(w)

    def refresh():
        text = flt.text().strip().lower()
        shown = 0
        for i in range(lst.count()):
            item = lst.item(i)
            name = item.text()
            hide = (text and text not in name.lower()) or (not inner.isChecked() and "$" in name.rsplit("/", 1)[-1])
            item.setHidden(bool(hide))
            if hide:
                item.setSelected(False)
            shown += not hide
        picked = len(lst.selectedItems())
        status.setText("%d of %d classes shown, %d selected" % (shown, len(found), picked))
        buttons.button(QDialogButtonBox.Open).setEnabled(picked > 0)

    def select_main():
        if not main_class:
            return
        if "$" in main_class.rsplit("/", 1)[-1]:
            inner.setChecked(True)
        for i in range(lst.count()):
            item = lst.item(i)
            if item.text() == main_class and not item.isHidden():
                item.setSelected(True)
                lst.setCurrentItem(item)
                lst.scrollToItem(item)
                return

    flt.textChanged.connect(refresh)
    inner.toggled.connect(refresh)
    lst.itemSelectionChanged.connect(refresh)
    lst.itemDoubleClicked.connect(lambda _item: dialog.accept())
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    refresh()
    select_main()
    flt.setFocus()
    if dialog.exec() != QDialog.Accepted:
        return []
    return [item.data(Qt.UserRole) for item in lst.selectedItems() if not item.isHidden()]


def choose_nested_jars(entries, title):
    """multi-select of nested jar entry names. Returns the ones to unpack ([] if skipped or Qt is absent).
    Nothing is selected at first: unpacking a fat JAR's dependencies is opt-in."""
    if not entries:
        return []
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QLabel, QLineEdit,
                                       QListWidget, QListWidgetItem, QVBoxLayout)
    except ImportError:
        return []

    dialog = QDialog()
    dialog.setWindowTitle(title)
    dialog.resize(640, 480)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("Unpack selected nested JARs (lib/bar.jar goes to lib/bar/)."))
    flt = QLineEdit()
    flt.setPlaceholderText("Filter")
    select_all = QCheckBox("Select all shown")
    lst = QListWidget()
    lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
    for name in entries:
        item = QListWidgetItem(name)
        item.setData(Qt.UserRole, name)
        lst.addItem(item)
    status = QLabel()
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText("Unpack")
    buttons.button(QDialogButtonBox.Cancel).setText("Skip")
    for w in (flt, select_all, lst, status, buttons):
        layout.addWidget(w)

    def refresh():
        text = flt.text().strip().lower()
        shown = 0
        for i in range(lst.count()):
            item = lst.item(i)
            hide = bool(text and text not in item.text().lower())
            item.setHidden(hide)
            if hide:
                item.setSelected(False)
            shown += not hide
        picked = len(lst.selectedItems())
        status.setText("%d of %d nested JARs shown, %d selected" % (shown, len(entries), picked))
        buttons.button(QDialogButtonBox.Ok).setEnabled(picked > 0)

    def on_all(checked):
        for i in range(lst.count()):
            item = lst.item(i)
            if not item.isHidden():
                item.setSelected(bool(checked))

    def on_double(item):
        if not item.isSelected():
            lst.clearSelection()
            item.setSelected(True)
        dialog.accept()

    flt.textChanged.connect(refresh)
    select_all.toggled.connect(on_all)
    lst.itemSelectionChanged.connect(refresh)
    lst.itemDoubleClicked.connect(on_double)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    refresh()
    flt.setFocus()
    if dialog.exec() != QDialog.Accepted:
        return []
    return [item.data(Qt.UserRole) for item in lst.selectedItems() if not item.isHidden()]


def open_class_tabs(paths):
    """open each path as a tab (asks first when there are many). Returns how many opened"""
    from binaryninja import show_message_box, MessageBoxButtonSet, MessageBoxButtonResult
    if len(paths) > MANY_TABS and show_message_box(
            "Open classes", "Open %d classes as %d tabs?" % (len(paths), len(paths)),
            MessageBoxButtonSet.YesNoButtonSet) != MessageBoxButtonResult.YesButton:
        return 0
    return sum(1 for path in paths if open_class_tab(path))


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
    """ask for a .jar if path is omitted, unpack it (and any nested JARs the user picks), then open
    the classes the user picks. Main-Class is preselected. Non-class entries are skipped and logged."""
    from binaryninja import get_open_filename_input, log_error, log_info, show_message_box
    try:
        if not path:
            path = get_open_filename_input("Open whole JAR", "*.jar")
            if not path:
                return
        path = os.path.abspath(path)
        try:
            scan = scan_jar(path)
        except (zipfile.BadZipFile, OSError) as e:
            log_error("Whole JAR: %s" % e)
            show_message_box("Whole JAR", "Could not read %s\n%s" % (path, e))
            return
        note = skipped_summary(scan.other, scan.unsafe)
        if note:
            log_info("Whole JAR: %s" % note)
        if not scan.classes and not scan.nested:
            log_error("Whole JAR: no .class entries in %s" % path)
            show_message_box("Whole JAR", "No .class entries in %s" % path)
            return
        chosen_nested = []
        if scan.nested:
            chosen_nested = choose_nested_jars(
                scan.nested, "Nested JARs in %s (%d)" % (os.path.basename(path), len(scan.nested)))
            left = [name for name in scan.nested if name not in set(chosen_nested)]
            if left:
                log_info("Whole JAR: %s" % nested_left_message(left))
        try:
            extract_classes(path)
        except (zipfile.BadZipFile, OSError) as e:
            log_error("Whole JAR: %s" % e)
            show_message_box("Whole JAR", "Could not read %s\n%s" % (path, e))
            return
        for entry in chosen_nested:
            try:
                written, further, unsafe = unpack_nested_jar(path, entry)
                log_info("Whole JAR: unpacked %s -> %s (%d classes)" % (
                    entry, nested_unpack_dir(path, entry), len(written)))
                if further:
                    log_info("Whole JAR: %s contains %d nested JARs; left packed" % (entry, len(further)))
                if unsafe:
                    log_info("Whole JAR: %s skipped %d entr%s outside the unpack directory" % (
                        entry, len(unsafe), "y" if len(unsafe) == 1 else "ies"))
            except (zipfile.BadZipFile, OSError, ValueError, KeyError) as e:
                log_error("Whole JAR: nested %s: %s" % (entry, e))
        dest = jar_dest(path)
        found = classes_in(dest)
        if not found:
            log_error("Whole JAR: no .class entries in %s" % path)
            show_message_box("Whole JAR", "No .class entries in %s" % path)
            return
        binary = main_class_of(scan.attributes)
        match = find_main_class(found, binary) if binary else None
        label = main_class_note(binary, match) if binary else None
        if label:
            log_info("Whole JAR: %s" % label)
        chosen = choose_classes(
            found, "Whole JAR: %s (%d classes)" % (os.path.basename(path), len(found)),
            main_class=match, main_label=label)
        if not chosen:
            log_info("Whole JAR: unpacked %d classes to %s" % (len(found), dest))
            return
        opened = open_class_tabs(chosen)
        if opened:
            log_info("Whole JAR: opened %d of %d chosen classes (%d unpacked)" % (opened, len(chosen), len(found)))
        elif len(chosen) <= MANY_TABS:
            show_message_box("Whole JAR", "Unpacked to %s but could not open a tab." % dest)
    except Exception as e:
        log_error("Whole JAR: %s" % e)
        show_message_box("Whole JAR", "Failed:\n%s" % e)


def open_class_in_same_tree(bv):
    """chooser of the other classes in this class's unpacked directory (the decompiler reads those
    files itself; this only opens a tab). Main-Class from the .jar beside that directory is preselected."""
    from binaryninja import log_error
    from .classui import class_dir, class_info
    info = class_info(bv)
    root = class_dir(bv, info) if info else None
    if not root or not os.path.isdir(root):
        log_error("Open class: this file is not inside an unpacked JAR directory")
        return
    found = classes_in(root)
    binary = manifest_main_class(root)
    match = find_main_class(found, binary) if binary else None
    label = main_class_note(binary, match) if binary else None
    open_class_tabs(choose_classes(
        found, "Classes in %s" % os.path.basename(root.rstrip(os.sep)),
        main_class=match, main_label=label))


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
        "Unpack a JAR next to itself. Main-Class is preselected; nested JARs can be unpacked too. "
        "The decompiler reads the other classes from that folder.",
        action)
    PluginCommand.register_global(action, "Unpack a JAR, preselect its Main-Class, and choose which classes to open",
                                  load_whole_jar)
    pick = "JVM\\Open class from this JAR..."
    PluginCommand.register(pick, "Choose another class in the same unpacked JAR and open it",
                           open_class_in_same_tree,
                           lambda bv: _has_tree(bv))
