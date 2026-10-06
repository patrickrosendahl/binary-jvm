from .arch import register_arch
from .view import register_view

register_arch()
register_view()

try:
    from . import pseudo_java
    pseudo_java.register()
except Exception:  # the language is optional; never break the architecture/view registration
    import traceback
    traceback.print_exc()
