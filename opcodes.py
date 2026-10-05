"""JVM opcode tables and the instruction decoder (pure Python, no Binary Ninja dependency)."""
import struct

InstructionNames = [
'nop', 'aconst_null', 'iconst_m1', 'iconst_0', 'iconst_1', 'iconst_2', 'iconst_3', 'iconst_4', 
'iconst_5', 'lconst_0', 'lconst_1', 'fconst_0', 'fconst_1', 'fconst_2', 'dconst_0', 'dconst_1', 
'bipush', 'sipush', 'ldc', 'ldc_w', 'ldc2_w', 'iload', 'lload', 'fload', 
'dload', 'aload', 'iload_0', 'iload_1', 'iload_2', 'iload_3', 'lload_0', 'lload_1', 
'lload_2', 'lload_3', 'fload_0', 'fload_1', 'fload_2', 'fload_3', 'dload_0', 'dload_1', 
'dload_2', 'dload_3', 'aload_0', 'aload_1', 'aload_2', 'aload_3', 'iaload', 'laload', 
'faload', 'daload', 'aaload', 'baload', 'caload', 'saload', 'istore', 'lstore', 
'fstore', 'dstore', 'astore', 'istore_0', 'istore_1', 'istore_2', 'istore_3', 'lstore_0', 
'lstore_1', 'lstore_2', 'lstore_3', 'fstore_0', 'fstore_1', 'fstore_2', 'fstore_3', 'dstore_0', 
'dstore_1', 'dstore_2', 'dstore_3', 'astore_0', 'astore_1', 'astore_2', 'astore_3', 'iastore', 
'lastore', 'fastore', 'dastore', 'aastore', 'bastore', 'castore', 'sastore', 'pop', 
'pop2', 'dup', 'dup_x1', 'dup_x2', 'dup2', 'dup2_x1', 'dup2_x2', 'swap', 
'iadd', 'ladd', 'fadd', 'dadd', 'isub', 'lsub', 'fsub', 'dsub', 
'imul', 'lmul', 'fmul', 'dmul', 'idiv', 'ldiv', 'fdiv', 'ddiv', 
'irem', 'lrem', 'frem', 'drem', 'ineg', 'lneg', 'fneg', 'dneg', 
'ishl', 'lshl', 'ishr', 'lshr', 'iushr', 'lushr', 'iand', 'land', 
'ior', 'lor', 'ixor', 'lxor', 'iinc', 'i2l', 'i2f', 'i2d', 
'l2i', 'l2f', 'l2d', 'f2i', 'f2l', 'f2d', 'd2i', 'd2l', 
'd2f', 'i2b', 'i2c', 'i2s', 'lcmp', 'fcmpl', 'fcmpg', 'dcmpl', 
'dcmpg', 'ifeq', 'ifne', 'iflt', 'ifge', 'ifgt', 'ifle', 'if_icmpeq', 
'if_icmpne', 'if_icmplt', 'if_icmpge', 'if_icmpgt', 'if_icmple', 'if_acmpeq', 'if_acmpne', 'goto', 
'jsr', 'ret', 'tableswitch', 'lookupswitch', 'ireturn', 'lreturn', 'freturn', 'dreturn', 
'areturn', 'return', 'getstatic', 'putstatic', 'getfield', 'putfield', 'invokevirtual', 'invokespecial', 
'invokestatic', 'invokeinterface', 'invokedynamic', 'new', 'newarray', 'anewarray', 'arraylength', 'athrow', 
'checkcast', 'instanceof', 'monitorenter', 'monitorexit', 'wide', 'multianewarray', 'ifnull', 'ifnonnull', 
'goto_w', 'jsr_w', None, None, None, None, None, None, 
None, None, None, None, None, None, None, None, 
None, None, None, None, None, None, None, None, 
None, None, None, None, None, None, None, None, 
None, None, None, None, None, None, None, None, 
None, None, None, None, None, None, None, None, 
None, None, None, None, None, None, None, None]

# operand bytes following the opcode; switches and wide are variable and sized in decode_instruction
InstructionLengths = [0,1,2,1,2,2,0,0,2,4,4,1,3,3,4,1]

TYPE_NONE   = 0
TYPE_BYTE   = 1
TYPE_2BYTE  = 2
TYPE_INDEX  = 3
TYPE_IINC   = 4
TYPE_2BRANCH = 5
TYPE_TABLESWITCH = 6
TYPE_LOOKUPSWITCH = 7
TYPE_2INDEX = 8
TYPE_INTERFACE = 9
TYPE_DYNAMIC = 10
TYPE_ATYPE = 11
TYPE_WIDE = 12
TYPE_MULTIARRAY = 13
TYPE_4BRANCH = 14
TYPE_LDC = 15

InstructionFormat = [
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_BYTE, TYPE_2BYTE, TYPE_LDC, TYPE_2INDEX, TYPE_2INDEX, TYPE_INDEX, TYPE_INDEX, TYPE_INDEX, 
TYPE_INDEX, TYPE_INDEX, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_INDEX, TYPE_INDEX, 
TYPE_INDEX, TYPE_INDEX, TYPE_INDEX, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_IINC, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_2BRANCH ,TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, 
TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, TYPE_2BRANCH, 
TYPE_2BRANCH, TYPE_INDEX, TYPE_TABLESWITCH, TYPE_LOOKUPSWITCH, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_2INDEX, TYPE_2INDEX, TYPE_2INDEX, TYPE_2INDEX, TYPE_2INDEX, TYPE_2INDEX, 
TYPE_2INDEX, TYPE_INTERFACE, TYPE_DYNAMIC, TYPE_2INDEX, TYPE_ATYPE, TYPE_2INDEX, TYPE_NONE, TYPE_NONE, 
TYPE_2INDEX, TYPE_2INDEX, TYPE_NONE, TYPE_NONE, TYPE_WIDE, TYPE_MULTIARRAY, TYPE_2BRANCH, TYPE_2BRANCH, 
TYPE_4BRANCH, TYPE_4BRANCH, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, 
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE,
TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE, TYPE_NONE]

def slot_size(descriptor_char):
    return 8 if descriptor_char in "JD" else 4

def parse_method_descriptor(desc):
    """'(I[JLjava/lang/String;)V' -> (['I', '[', 'L'], 'V')"""
    args = []
    i = desc.index('(') + 1
    while desc[i] != ')':
        start = i
        while desc[i] == '[':
            i += 1
        if desc[i] == 'L':
            i = desc.index(';', i)
        i += 1
        args.append(desc[start])
    return args, desc[i+1]

WIDE_OPCODES = set(range(0x15, 0x1a)) | set(range(0x36, 0x3b)) | {0xa9}  # xload, xstore, ret
MAX_SWITCH_CASES = 0x4000  # code_length is < 64KiB, so no real switch is larger

def decode_instruction(data, addr):
        """returns (name, operand type, length, operand value); on truncated data name is None and length
        is the number of bytes needed (if known)"""
        if len(data) < 1:
            return None, None, None, None
        opcode = data[0]
        instr = InstructionNames[opcode]
        if instr is None:
            return None, None, None, None

        operand = InstructionFormat[opcode]
        length = 1 + InstructionLengths[operand]
        if len(data) < length:
            return None, None, None, length

        if operand == TYPE_NONE:
            value = None
        elif operand == TYPE_BYTE:
            value = struct.unpack(">b", data[1:2])[0]
        elif operand == TYPE_2BYTE:
            value = struct.unpack(">h", data[1:3])[0]
        elif operand == TYPE_INDEX:
            value = struct.unpack(">B", data[1:2])[0]
        elif operand == TYPE_LDC:
            value = struct.unpack(">B", data[1:2])[0]
        elif operand == TYPE_2INDEX:
            value = struct.unpack(">H", data[1:3])[0]
        elif operand == TYPE_IINC:
            value = struct.unpack(">Bb", data[1:3])
        elif operand == TYPE_2BRANCH:
            value = addr+struct.unpack(">h", data[1:3])[0]
        elif operand == TYPE_4BRANCH:
            value = addr+struct.unpack(">i", data[1:5])[0]
        elif operand in (TYPE_TABLESWITCH, TYPE_LOOKUPSWITCH):
            # operands are 4-byte aligned relative to the method's code start; methods are mapped at
            # 4-byte aligned bases, so the absolute address works
            base = 1 + (-(addr+1)) % 4
            if operand == TYPE_TABLESWITCH:
                if len(data) < base+12:
                    return None, None, None, base+12
                default, low, high = struct.unpack(">iii", data[base:base+12])
                count = high-low+1
                if count < 0 or count > MAX_SWITCH_CASES:
                    return None, None, None, None
                length = base+12+count*4
                if len(data) < length:
                    return None, None, None, length
                offsets = []
                for i in range(count):
                    offsets.append((low+i, addr+struct.unpack(">i", data[base+12+i*4:base+16+i*4])[0]))
                value = (addr+default, low, high, offsets)
            else:
                if len(data) < base+8:
                    return None, None, None, base+8
                default, npairs = struct.unpack(">ii", data[base:base+8])
                if npairs < 0 or npairs > MAX_SWITCH_CASES:
                    return None, None, None, None
                length = base+8+npairs*8
                if len(data) < length:
                    return None, None, None, length
                offsets = []
                for i in range(npairs):
                    match, offset = struct.unpack(">ii", data[base+8+i*8:base+16+i*8])
                    offsets.append((match, addr+offset))
                value = (addr+default, npairs, offsets)
        elif operand == TYPE_INTERFACE:
            index, count = struct.unpack(">HB", data[1:4])
            value = (index, count)
        elif operand == TYPE_DYNAMIC:
            index = struct.unpack(">H", data[1:3])[0]
            value = (index, index)
        elif operand == TYPE_ATYPE:
            value = struct.unpack(">B", data[1:2])[0]
        elif operand == TYPE_WIDE:
            op2 = data[1]
            if op2 == 0x84: #iinc
                length = 6
                if len(data) < length:
                    return None, None, None, length
                value = struct.unpack(">BHh", data[1:6])
            elif op2 in WIDE_OPCODES:
                value = struct.unpack(">BH", data[1:4])
            else:
                return None, None, None, None
        elif operand == TYPE_MULTIARRAY:
            value = struct.unpack(">HB", data[1:4])
        else:
            value = None
        return instr, operand, length, value

CONDITIONAL_BRANCHES = {"ifnull", "ifnonnull", "if_acmpne", "if_acmpeq", "if_icmple", "if_icmpgt", "if_icmpge", "if_icmplt",
                        "if_icmpne", "if_icmpeq", "ifle", "ifgt", "ifge", "iflt", "ifne", "ifeq"}
RETURNS = {"ireturn", "lreturn", "freturn", "dreturn", "areturn", "return", "ret"}
