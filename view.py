"""The "JVM Class" BinaryView: maps each method's code as a segment and defines pool symbols/types."""
import struct
import traceback

from binaryninja import (Architecture, BinaryView, Symbol, SymbolType, SegmentFlag, SectionSemantics, Settings,
                         SettingsScope, Type)

from .constants import *
from .opcodes import decode_instruction, parse_method_descriptor
from .classfile import *
from .lifter import field_type

METHOD_POOL_CLASSES = (JVMMethodReference, JVMInterfaceMethodReference, JVMInvokeDynamic)

def call_value_type(ch):
    # types at invoke call sites; float/double travel as int bits (jvm_call has no float registers)
    if ch == 'V': return Type.void()
    if ch in 'JD': return Type.int(8)
    if ch in 'L[': return Type.pointer_of_width(ADDR_SIZE, Type.void())
    return Type.int(4)

def method_short_name(reader, content):
    """'java/lang/StringBuilder.append' -> 'StringBuilder.append'; invokedynamic -> 'indy.<name>'"""
    if isinstance(content, JVMInvokeDynamic):
        return "indy." + str(reader.poolEntry(content.nat))
    cls = str(reader.poolEntry(content.classReference))
    return cls.rsplit("/", 1)[-1] + "." + str(reader.poolEntry(content.nameAndType))

def completeUpdateWhenDone(event):
    for f in event.view.functions:
        analyze_tables(event.view, f)

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
                        self.define_data_var(pool_address(i), field_type(desc))

            self.define_method_slots(classStruct)

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
            # exception handlers are only reachable through the exception table: give each its own function
            for handler_pc in sorted({entry[2] for entry in code.exception_table}):
                self.add_function(base+handler_pc)
                self.define_auto_symbol(Symbol(SymbolType.FunctionSymbol, base+handler_pc, "%s$catch_%x" % (name, handler_pc)))

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

    def define_method_slots(self, classStruct):
        # invokes are lifted as call(load(pool slot)): type every method pool entry as a function pointer
        # so call sites get parameters and return values
        cc = Architecture[ARCH_NAME].calling_conventions["jvm_call"]
        kinds = self.invoke_kinds(classStruct)
        for i, content in enumerate(self.cR.constantPool.poolContent):
            if not isinstance(content, METHOD_POOL_CLASSES):
                continue
            desc = self.cR.memberDescriptor(i)
            if not desc:
                continue
            args, ret = parse_method_descriptor(desc)
            params = [call_value_type(a) for a in args]
            static = isinstance(content, JVMInvokeDynamic) or kinds.get(i, set()) <= {"invokestatic", "invokedynamic"} and i in kinds
            if not static:
                params = [Type.pointer_of_width(ADDR_SIZE, Type.void())] + params
            func = Type.function(call_value_type(ret), params, calling_convention=cc)
            self.define_data_var(pool_address(i), Type.pointer_of_width(ADDR_SIZE, func))

    def perform_is_executable(self):
        return True

    def perform_get_entry_point(self):
        return getattr(self, "entry_address", 0)
        


def register_view():
    ClassView.register()
