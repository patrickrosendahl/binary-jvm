"""The "JVM Class" BinaryView: maps each method's code as a segment and defines pool symbols/types."""
import struct
import traceback

from binaryninja import (Architecture, BinaryView, Symbol, SymbolType, SegmentFlag, SectionSemantics, Settings,
                         SettingsScope, Type, TypeBuilder, FunctionParameter, ReturnValue, BaseStructure,
                         CoreVariable, VariableSourceType, MetadataStoreFlag, NamedTypeReferenceClass)

from .constants import *
from .opcodes import decode_instruction
from .classfile import *
from .javatypes import dotted, split_method_descriptor, method_parameter_slots, slot_count
from .methodinfo import method_info

METHOD_POOL_CLASSES = (JVMMethodReference, JVMInterfaceMethodReference, JVMInvokeDynamic)

class JavaTypes:
    """Binary Ninja types for Java values (jvm-35).

    Naming scheme: every class gets one named struct called by its dotted binary name
    (`java.lang.String`, `com.foo.Outer$Inner`); classes other than the one being viewed are opaque
    (no members). A reference is a 4-byte pointer to that struct (`java.lang.String*`). An array is a
    pointer to its element (`int[]` -> `int32_t*`, `String[]` -> `java.lang.String**`, `byte[]` ->
    `int8_t*`): BN has no array-reference type and this keeps element widths right. boolean/byte/char/
    short values are widened to int by the JVM (stack, locals, static-field accesses are 4 bytes), so
    as scalars they are the 4-byte typedefs `jboolean`/`jbyte`/`jchar`/`jshort` (JNI names; `char` and
    `short` would clash with C); int/long/float/double are int32_t/int64_t/float/double.
    The viewed class's struct holds its instance fields (sequential offsets in declaration order,
    4 or 8 bytes each -- arbitrary but stable) and names its superclass as a base structure."""

    SCALARS = {'Z': ("jboolean", False), 'B': ("jbyte", True), 'C': ("jchar", False), 'S': ("jshort", True)}
    ELEMENTS = {'Z': lambda: Type.bool(), 'B': lambda: Type.int(1, True), 'C': lambda: Type.int(2, False),
                'S': lambda: Type.int(2, True)}

    def __init__(self, view):
        self.view = view
        self.refs = {}

    def _named(self, name, make, width=0):
        # a reference (by type id) to the named type, defining it on first use
        t = self.refs.get(name)
        if t is None:
            tid = Type.generate_auto_type_id("jvm", name)
            body = make()
            self.view.define_type(tid, name, body)
            if width:  # typedef: the reference needs the width, or data vars/params of it are 0 bytes
                t = Type.named_type_reference(NamedTypeReferenceClass.TypedefNamedTypeClass, name, tid, width, width)
            else:
                t = Type.named_type_from_type_and_id(tid, name, body)
            self.refs[name] = t
        return t

    def class_struct(self, binary_name):
        """the named struct of a class (defined opaque on first use)"""
        if binary_name.startswith("["):  # CONSTANT_Class can name an array type
            return None
        return self._named(dotted(binary_name), lambda: Type.structure())

    def define_class(self, cls):
        """the viewed class's struct: instance fields + superclass as base structure"""
        builder = TypeBuilder.structure()
        builder.packed = True
        offset = 0
        for f in cls.fields:
            if f.access_flags & ACC_STATIC:
                continue
            t = self.value_type(f.descriptor)
            builder.add_member_at_offset(f.name, t, offset)
            offset += 8 if f.descriptor[0] in "JD" else 4
        if cls.super_name:
            base = self.class_struct(cls.super_name)
            builder.base_structures = [BaseStructure(base, 0, 0)]
        name = dotted(cls.name)
        self.refs.pop(name, None)
        return self._named(name, lambda: builder)

    def scalar(self, ch):
        if ch in self.SCALARS:
            name, sign = self.SCALARS[ch]
            return self._named(name, lambda: Type.int(4, sign), 4)
        if ch == 'I': return Type.int(4)
        if ch == 'J': return Type.int(8)
        if ch == 'F': return Type.float(4)
        if ch == 'D': return Type.float(8)
        if ch == 'V': return Type.void()
        return Type.int(4)

    def value_type(self, desc, element=False):
        """type of a value of this field descriptor (element=True: as an array element, real width)"""
        if desc[0] == '[':
            return Type.pointer_of_width(ADDR_SIZE, self.value_type(desc[1:], element=True))
        if desc[0] == 'L':
            return Type.pointer_of_width(ADDR_SIZE, self.class_struct(desc[1:-1]))
        if element and desc[0] in self.ELEMENTS:
            return self.ELEMENTS[desc[0]]()
        return self.scalar(desc[0])

    def return_value(self, desc, arch):
        t = self.value_type(desc)
        # jvm/jvm_call have no float registers: float results come back in r; long/double in the 8-byte
        # register r64, which must be given explicitly (the conventions' default is the pair rh:r)
        if desc[0] == 'F':
            return ReturnValue(t, CoreVariable.reg(arch.get_reg_index("r")))
        if desc[0] in 'JD':
            return ReturnValue(t, CoreVariable.reg(arch.get_reg_index("r64")))
        return t

def method_short_name(reader, content):
    """'java/lang/StringBuilder.append' -> 'StringBuilder.append'; invokedynamic -> 'indy.<name>'"""
    if isinstance(content, JVMInvokeDynamic):
        return "indy." + str(reader.poolEntry(content.nat))
    cls = str(reader.poolEntry(content.classReference))
    return cls.rsplit("/", 1)[-1] + "." + str(reader.poolEntry(content.nameAndType))

MAX_STORE_LENGTH = 4  # longest xstore (wide astore): a local's LVT range starts right after its store

def name_locals(view, func, reader):
    """jvm-35: name/type the local-register variables l<n> from the LocalVariableTable. Runs after
    analysis: BN splits a register into several variables (different `index`) only known then. Each
    variable gets the LVT entry its definitions fall into; if they fall into several (a reused slot),
    the entry covering most code wins. Parameters are already named by the function type."""
    index = (func.start - METHOD_BASE) // METHOD_STRIDE
    methods = getattr(reader.classStruct, "methods", [])
    if not 0 <= index < len(methods) or reader.jtypes is None:
        return
    method = methods[index]
    entries = {}
    for start, length, name, desc, slot in method.local_variables():
        if name and desc and slot < NUM_LOCAL_REGS:
            entries.setdefault(slot, []).append((start, length, name, desc))
    if not entries or func.mlil is None:
        return
    base = method_address(index)
    params = set(func.parameter_vars)
    arch = func.arch
    for var in func.vars:
        if var.source_type != VariableSourceType.RegisterVariableSourceType or var in params:
            continue
        reg = arch.get_reg_name(var.storage)
        if not reg.startswith("l") or not reg[1:].isdigit() or int(reg[1:]) not in entries:
            continue
        if func.is_var_user_defined(var):
            continue  # named before (a second completion event, or a reopened .bndb)
        hits = {}
        for d in func.mlil.get_var_definitions(var):
            off = d.address - base
            for start, length, name, desc in entries[int(reg[1:])]:
                if start - MAX_STORE_LENGTH <= off < start + length:
                    hits[(name, desc)] = max(hits.get((name, desc), 0), length)
        if hits:
            (name, desc), _ = max(hits.items(), key=lambda kv: (kv[1], kv[0]))
            func.create_user_var(var, reader.jtypes.value_type(desc), name)

def define_components(view, reader):
    """jvm-41: component tree package -> class holding the methods, the class-file header and the
    class's own static fields. Components are saved in a .bndb (and restored only after init), so
    nothing is created when the class component already exists."""
    cls = reader.classStruct
    parts = cls.name.split("/")
    if view.get_component_by_path("/" + "/".join(parts)) is not None:
        return
    parent = None
    for i in range(len(parts)):
        existing = view.get_component_by_path("/" + "/".join(parts[:i+1]))
        parent = existing or view.create_component(parts[i], parent)
    for method in cls.methods:
        func = view.get_function_at(method_address(method.index)) if method.code_attribute else None
        if func is not None:
            parent.add_function(func)
    addrs = [CLASSFILE_BASE]
    for i, content in enumerate(reader.constantPool.poolContent):
        if isinstance(content, JVMFieldReference) and str(reader.poolEntry(content.classReference)) == cls.name:
            addrs.append(pool_address(i))
    for addr in addrs:
        var = view.get_data_var_at(addr)
        if var is not None:
            parent.add_data_variable(var)

def completeUpdateWhenDone(event):
    view = event.view
    reader = reader_for_view(view)
    for f in view.functions:
        analyze_tables(view, f)
        if reader is not None:
            name_locals(view, f, reader)
    if reader is not None and getattr(reader, "pending_components", False):
        reader.pending_components = False
        define_components(view, reader)

def analyze_tables(view, dispatcher):
    table_jumps = []
    
    for token,addr in dispatcher.instructions:
        if ('lookupswitch' in str(token[0]) or 'tableswitch' in str(token[0])):
            table_jumps.append(addr)
            
    for addr in table_jumps:
        #if len(dispatcher.get_indirect_branches_at(addr)) != 0:
        #    continue
        decoded = decode_instruction(view.read(addr, MAX_INSTR_LENGTH), addr)
        if decoded[0] == "lookupswitch":
            pair_array = 2
        elif decoded[0] == "tableswitch":
            pair_array = 3
        else:
            continue

        value = decoded[3]
        targets = sorted({p[1] for p in value[pair_array]} | {value[0]})
        known = sorted({b.dest_addr for b in dispatcher.get_indirect_branches_at(addr)})
        if known == targets:
            continue  # already applied; setting again would retrigger analysis forever
        dispatcher.set_user_indirect_branches(addr, [(view.arch, t) for t in targets])
        
        #Do comments later as they update the binary
        dispatcher.set_comment_at(value[0], "Default Branch")
        for p in value[pair_array]:
            ad = ""
            if p[1] == value[0]:
                ad = " [Default Branch]"
            dispatcher.set_comment_at(p[1], "Branch Condition: "+str(p[0])+ad)
    
ACC_STATIC = 0x0008

class ClassView(BinaryView):
    name = VIEW_NAME
    long_name = "JVM Class Format"

    def __init__(self, data):
        BinaryView.__init__(self, parent_view = data, file_metadata = data.file)
        self.platform = Architecture[ARCH_NAME].standalone_platform
        
    @classmethod
    def is_valid_for_data(self, data):
        hdr = data.read(0, 6)
        if len(hdr) < 4:
            return False
        if struct.unpack(">I", hdr[0:4])[0] != 0xCAFEBABE:
            return False
        return True

    def init(self):
        try:
            # catch-handler functions overlap their method; don't let BN turn shared code into tail calls
            for key in ("core.function.translateTailCalls", "core.function.analyzeTailCalls"):
                Settings().set_bool(key, False, self, SettingsScope.SettingsResourceScope)
            self.cR = JVMClassReader(self,self.parent_view)
            register_reader(self, self.cR)
            
            self.cR.charType = self.parse_type_string("char")[0] # for some reason in my binary ninja version Type.char() is not accessable
            
            classStruct = JVMClassStructure(self.cR) # read class structure and add symbols
            self.cR.classStruct = classStruct
           
            self.add_auto_segment(CLASSFILE_BASE, self.cR.index(), 0, self.cR.index(), SegmentFlag.SegmentReadable)
            self.add_auto_section("<data>", CLASSFILE_BASE, self.cR.index(), SectionSemantics.ReadOnlyCodeSectionSemantics)
            self.define_data_var(CLASSFILE_BASE, classStruct.resultingType())
            self.define_class_types(classStruct)
            self.define_methods(classStruct)
            
            for i in range(len(self.cR.constantPool.poolContent)):
                content = self.cR.constantPool.poolContent[i]
                t = SymbolType.DataSymbol
                if ("instance" in str(type(content))) or ("jvm" in str(type(content))):
                    if content.__class__ == JVMMethodReference or content.__class__ == JVMInterfaceMethodReference or content.__class__ == JVMInvokeDynamic:
                        t = SymbolType.ImportAddressSymbol
                    elif content.__class__ == JVMStringReference:
                        t = SymbolType.DataSymbol
                    elif content.__class__ == JVMUTF8Info:
                        t = SymbolType.DataSymbol
                elif "str" in str(type(content)):
                    t = SymbolType.DataSymbol
               
                if t == SymbolType.ImportAddressSymbol:
                    self.define_user_symbol(Symbol(t, pool_address(i), method_short_name(self.cR, content),
                                                   full_name=str(content), raw_name=str(content)))
                else:
                    self.define_user_symbol(Symbol(t, pool_address(i), str(content), full_name="pool_"+str(i)))
                
            primitive_names = ["Not Used","Not Used","Not Used","Not Used","Boolean","Char","Float","Double","Byte","Short","Int","Long"]
            for i in range(4,12):
                self.define_user_symbol(Symbol(SymbolType.DataSymbol, i+PSEUDOMEMORY_PRIMITIVES, primitive_names[i], full_name="primitive_"+str(i)))

            # static fields are lifted as loads/stores of their pool entry: type them by descriptor
            for i, content in enumerate(self.cR.constantPool.poolContent):
                if isinstance(content, JVMFieldReference):
                    desc = self.cR.memberDescriptor(i)
                    if desc:
                        self.define_data_var(pool_address(i), self.jtypes.value_type(desc))

            self.define_method_slots(classStruct)
            self.type_methods(classStruct)
            self.store_metadata(CLASS_METADATA_KEY, class_metadata(classStruct), MetadataStoreFlag.MetadataStorePersistent)
            # reopened from a .bndb: its components are restored after init -- decide once analysis is done
            self.cR.pending_components = self.file.has_database
            if not self.cR.pending_components:
                define_components(self, self.cR)

            self.add_analysis_completion_event(completeUpdateWhenDone)
            
            return True
        except:
            print(traceback.format_exc())
            return False
        
    def define_methods(self, classStruct):
        # each method with code gets its own segment, function and symbol
        names = set()
        self.entry_address = 0  # the class-file header, unless some method qualifies (see below)
        entry_rank = 3
        for method in classStruct.methods:
            if method.code_attribute is None:
                continue
            code = method.code_attribute.attribute
            name = method.name
            posfix = 1
            while name in names:
                name = method.name+"("+str(posfix)+")"
                posfix += 1
            names.add(name)
            base = method_address(method.index)
            length = code.end_address-code.start_address
            self.add_auto_segment(base, length, code.start_address, length, SegmentFlag.SegmentReadable | SegmentFlag.SegmentExecutable)
            self.add_function(base)
            self.define_auto_symbol(Symbol(SymbolType.FunctionSymbol, base, name))
            # entry point: public static void main(String[]), else <clinit>, else the first method
            if method.name == "main" and method.descriptor == "([Ljava/lang/String;)V" and method.access_flags & ACC_STATIC:
                rank = 0
            elif method.name == "<clinit>":
                rank = 1
            else:
                rank = 2
            if rank < entry_rank:
                self.entry_address, entry_rank = base, rank
            # exception handlers are part of the method (the lifter models the exception edges, jvm-42);
            # the exception table is kept for the Pseudo-Java printer: [[start_pc, end_pc, handler_pc,
            # catch class ("" = any)], ...], pcs relative to the method's base
            if code.exception_table:
                func = self.get_function_at(base)
                if func is not None:
                    func.store_metadata("jvm.exception_table", [
                        [start, end, handler, str(self.cR.poolEntry(catch)) if catch else ""]
                        for start, end, handler, catch in code.exception_table])
                    mi = method_info(self.cR, base)
                    for handler in (mi.conflicts if mi is not None else ()):
                        # a handler whose type test continues differently per try range (lifted as an
                        # indirect jump; not seen in javac output)
                        func.set_user_indirect_branches(base + handler, [(func.arch, t) for t in mi.conflict_targets(handler)])

    def invoke_kinds(self, classStruct):
        # pool index -> set of invoke opcodes that use it (decides whether the call has a receiver)
        kinds = {}
        for method in classStruct.methods:
            if method.code_attribute is None:
                continue
            code = method.code_attribute.attribute
            data = memoryview(self.cR.data)[code.start_address:code.end_address]
            base = method_address(method.index)
            off = 0
            while off < len(data):
                name, operand, length, value = decode_instruction(data[off:], base+off)
                if name is None:
                    break
                if name.startswith("invoke"):
                    kinds.setdefault(value[0] if isinstance(value, tuple) else value, set()).add(name)
                off += length
        return kinds

    def define_class_types(self, classStruct):
        # jvm-35: the viewed class's struct first (so it is not defined opaque), then every class in the pool
        self.jtypes = self.cR.jtypes = JavaTypes(self)
        self.class_type = self.jtypes.define_class(classStruct)
        for name in classStruct.referenced_class_names():
            self.jtypes.class_struct(name)

    def define_method_slots(self, classStruct):
        # invokes are lifted as call(load(pool slot)): type every method pool entry as a function pointer
        # so call sites get parameters and return values. The lifter passes argument i (receiver first) in
        # a<i>, so each parameter gets that register as its location (lets floats travel in a<i> too).
        arch = Architecture[ARCH_NAME]
        cc = arch.calling_conventions["jvm_call"]
        kinds = self.invoke_kinds(classStruct)
        for i, content in enumerate(self.cR.constantPool.poolContent):
            if not isinstance(content, METHOD_POOL_CLASSES):
                continue
            desc = self.cR.memberDescriptor(i)
            if not desc:
                continue
            args, ret = split_method_descriptor(desc)
            types = [self.jtypes.value_type(a) for a in args]
            static = isinstance(content, JVMInvokeDynamic) or kinds.get(i, set()) <= {"invokestatic", "invokedynamic"} and i in kinds
            if not static:
                owner = self.jtypes.class_struct(str(self.cR.poolEntry(content.classReference)))
                types = [Type.pointer_of_width(ADDR_SIZE, owner if owner is not None else Type.void())] + types
            if len(types) > NUM_ARG_REGS:
                continue  # lifted as an intrinsic
            params = [FunctionParameter(t, "", CoreVariable.reg(arch.get_reg_index("a%d" % n)))
                      for n, t in enumerate(types)]
            func = Type.function(self.jtypes.return_value(ret, arch), params, calling_convention=cc)
            self.define_data_var(pool_address(i), Type.pointer_of_width(ADDR_SIZE, func))

    def method_function_type(self, method, arch, cc):
        """the function type of a method from its descriptor: parameters live in their local slots l<n>
        (custom locations, since long/double take two slots), `this` first for instance methods"""
        static = bool(method.access_flags & ACC_STATIC)
        slots = method_parameter_slots(method.descriptor, static)
        if slots and slots[-1][0] + slot_count(slots[-1][1]) > NUM_LOCAL_REGS:
            return None
        names = method.parameter_names()
        if names is not None and len(names) != len(slots):
            names = None  # synthetic/mandated parameters missing from MethodParameters: don't guess
        at_entry = {slot: name for start, length, name, desc, slot in method.local_variables() if start == 0 and name}
        def loc(slot):
            return CoreVariable.reg(arch.get_reg_index("l%d" % slot))
        params = []
        if not static:
            params.append(FunctionParameter(Type.pointer_of_width(ADDR_SIZE, self.class_type), "this", loc(0)))
        for k, (slot, desc) in enumerate(slots):
            name = (names[k] if names and names[k] else None) or at_entry.get(slot, "")
            params.append(FunctionParameter(self.jtypes.value_type(desc), name, loc(slot)))
        _, ret = split_method_descriptor(method.descriptor)
        return Type.function(self.jtypes.return_value(ret, arch), params, calling_convention=cc)

    def type_methods(self, classStruct):
        """jvm-35: function types from descriptors, local variable names/types from LocalVariableTable"""
        arch = Architecture[ARCH_NAME]
        cc = arch.calling_conventions["jvm"]
        for method in classStruct.methods:
            if method.code_attribute is None:
                continue
            addr = method_address(method.index)
            func, sym = self.get_function_at(addr), self.get_symbol_at(addr)
            if func is None or sym is None:
                continue
            ftype = self.method_function_type(method, arch, cc)
            if ftype is not None:
                # attached to the function symbol with full confidence; Function.set_auto_type() before the
                # first analysis is replaced by the inferred type
                self.define_auto_symbol_and_var_or_function(sym, ftype, type_confidence=255)

    def perform_is_executable(self):
        return True

    def perform_get_entry_point(self):
        return getattr(self, "entry_address", 0)
        


def register_view():
    ClassView.register()
