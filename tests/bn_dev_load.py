# Run inside Binary Ninja via the script bridge:  bnrun tests/bn_dev_load.py
# Copies the plugin package to /tmp/jvm_devN/, renames the architecture/view to "JVM-devN" / "JVM Class devN"
# and imports it, so the lifter can be iterated without restarting Binary Ninja (BN can't unregister an
# architecture or view type). Earlier dev view types are disabled. The Pseudo-Java language follows ARCH_NAME.
import importlib.util, os, shutil, sys
import binaryninja as _bn

REPO = globals().get("JVM_REPO", "/Users/patrick/dev/binary-jvm")  # prepend JVM_REPO = "..." to load another checkout (e.g. a worktree)
_dev = getattr(_bn, "_jvm_dev", None)
if _dev is None:
    _dev = _bn._jvm_dev = {"n": 0, "views": []}
for _v in _dev["views"]:
    _v.is_valid_for_data = classmethod(lambda cls, data: False)
_dev["n"] += 1
_n = _dev["n"]
_name = "jvm_dev%d" % _n
_dst = os.path.join("/tmp", _name)
shutil.rmtree(_dst, ignore_errors=True)
os.makedirs(_dst)
for _f in os.listdir(REPO):
    if _f.endswith(".py"):
        shutil.copy(os.path.join(REPO, _f), _dst)
_c = os.path.join(_dst, "constants.py")
_src = open(_c).read()
_src = _src.replace('ARCH_NAME = "JVM"', 'ARCH_NAME = "JVM-dev%d"' % _n).replace('VIEW_NAME = "JVM Class"', 'VIEW_NAME = "JVM Class dev%d"' % _n)
open(_c, "w").write(_src)
_spec = importlib.util.spec_from_file_location(_name, os.path.join(_dst, "__init__.py"), submodule_search_locations=[_dst])
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_name] = _mod
_spec.loader.exec_module(_mod)  # registers arch + calling convention + view
_dev["views"].append(sys.modules[_name + ".view"].ClassView)
_dev["pkg"] = _mod
_dev["ns"] = {"VIEW_NAME": sys.modules[_name + ".constants"].VIEW_NAME, "ARCH_NAME": sys.modules[_name + ".constants"].ARCH_NAME}
# the Pseudo-Java language name derives from ARCH_NAME ("Pseudo-Java JVM-devN"), so it is fresh per load too
_pj = sys.modules.get(_name + ".pseudo_java")
_dev["ns"]["LANGUAGE_NAME"] = getattr(_pj, "LANGUAGE_NAME", None)
print("registered", _dev["ns"]["ARCH_NAME"], "/", _dev["ns"]["VIEW_NAME"], "/", _dev["ns"]["LANGUAGE_NAME"])
