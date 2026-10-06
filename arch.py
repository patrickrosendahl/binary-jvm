"""The JVM Architecture: decoding, instruction text and the calling convention."""
import threading

from binaryninja import (Architecture, CallingConvention, RegisterInfo, IntrinsicInfo, InstructionInfo,
                         InstructionTextToken, InstructionTextTokenType, BranchType, Endianness)

from .constants import *
from .opcodes import *
from .lifter import lift_instruction, INTRINSICS, LiftContext, reader_for_il
from .classfile import reader_for_view
from .methodinfo import method_info, instruction_branches

# get_instruction_info gets no view, but the exception edges depend on the method's exception table:
# analyze_basic_blocks (which calls get_instruction_info for the function, on the same thread) records
# the class reader of the function being analysed here, keyed by thread
_analysing = {}

def int_token(text, value=None):
    if value is None:
        return InstructionTextToken(InstructionTextTokenType.IntegerToken, text)
    return InstructionTextToken(InstructionTextTokenType.IntegerToken, text, value)

def addr_token(value):
    return InstructionTextToken(InstructionTextTokenType.PossibleAddressToken, "0x%x" % value, value)

def sep_token():
    return InstructionTextToken(InstructionTextTokenType.OperandSeparatorToken, ", ")

def pool_token(index):
    return int_token("Pool@%d" % index, pool_address(index))

def wide_tokens(value):
    tokens = [InstructionTextToken(InstructionTextTokenType.TextToken, "%s " % InstructionNames[value[0]]), int_token("var_%d" % value[1])]
    if len(value) == 3:
        tokens += [sep_token(), int_token("%d" % value[2])]
    return tokens

OperandTokens = [
    lambda value: [],                                                          # TYPE_NONE
    lambda value: [int_token("%d" % value)],                                   # TYPE_BYTE
    lambda value: [int_token("%d" % value)],                                   # TYPE_2BYTE
    lambda value: [int_token("var_%d" % value)],                               # TYPE_INDEX
    lambda value: [int_token("var_%d" % value[0]), sep_token(), int_token("%d" % value[1])],  # TYPE_IINC
    lambda value: [addr_token(value)],                                         # TYPE_2BRANCH
    lambda value: [int_token("%d" % value[1]), InstructionTextToken(InstructionTextTokenType.TextToken, ".."), int_token("%d" % value[2]), sep_token(), InstructionTextToken(InstructionTextTokenType.TextToken, "default "), addr_token(value[0])],  # TYPE_TABLESWITCH
    lambda value: [int_token("%d" % value[1]), InstructionTextToken(InstructionTextTokenType.TextToken, " cases"), sep_token(), InstructionTextToken(InstructionTextTokenType.TextToken, "default "), addr_token(value[0])],  # TYPE_LOOKUPSWITCH
    lambda value: [pool_token(value)],                                         # TYPE_2INDEX
    lambda value: [pool_token(value[0])],                                      # TYPE_INTERFACE
    lambda value: [pool_token(value[0])],                                      # TYPE_DYNAMIC
    lambda value: [int_token("A@0x%.2x" % value, value+PSEUDOMEMORY_PRIMITIVES)],  # TYPE_ATYPE
    wide_tokens,                                                               # TYPE_WIDE
    lambda value: [pool_token(value[0]), sep_token(), int_token("%d" % value[1])],  # TYPE_MULTIARRAY
    lambda value: [addr_token(value)],                                         # TYPE_4BRANCH
    lambda value: [pool_token(value)],                                         # TYPE_LDC
]

def _build_regs():
    regs = {
        "s":   RegisterInfo("s", ADDR_SIZE),
        "lr":  RegisterInfo("lr", ADDR_SIZE),
        "r":   RegisterInfo("r", 4),   # 4-byte return value (int/float/reference)
        "rh":  RegisterInfo("rh", 4),  # high half of the conventions' default 8-byte return (unused by the lifter)
        # long/double return value: an independent 8-byte register, named as the explicit return location
        # of every long/double function type (view.JavaTypes.return_value)
        "r64": RegisterInfo("r64", 8),
    }
    for n in range(NUM_LOCAL_REGS):
        regs["l%d" % n] = RegisterInfo("l%d" % n, 8)
        regs["l%d_lo" % n] = RegisterInfo("l%d" % n, 4, 0)
    # outgoing invoke arguments, one register per argument (receiver first)
    for n in range(NUM_ARG_REGS):
        regs["a%d" % n] = RegisterInfo("a%d" % n, 8)
        regs["a%d_lo" % n] = RegisterInfo("a%d" % n, 4, 0)
    # operand-stack entry n: st<n>_lo holds a category-1 value (int/float/reference), st<n> a long/double.
    # Independent registers, not sub-registers: an entry holds 4- and 8-byte values at different points of
    # a method, and a partial write of a shared register would make BN merge them (.d/.q accessors)
    for n in range(NUM_STACK_REGS):
        regs["st%d" % n] = RegisterInfo("st%d" % n, 8)
        regs["st%d_lo" % n] = RegisterInfo("st%d_lo" % n, 4)
    # pending exception (0 = none): set by throwing calls (caller-saved in jvm_call) and athrow, tested
    # after throw sites inside try ranges, moved onto the operand stack at handler entry
    regs["exc"] = RegisterInfo("exc", ADDR_SIZE)
    # operands of a compare fused with the following if (lcmp/dcmp*: cmpa/cmpb, fcmp*: the _lo registers)
    for name in ("cmpa", "cmpb"):
        regs[name] = RegisterInfo(name, 8)
        regs[name + "_lo"] = RegisterInfo(name + "_lo", 4)
    return regs

class JVM(Architecture):
    
    name = ARCH_NAME
    address_size = ADDR_SIZE
    default_int_size = 4
    max_instr_length = MAX_INSTR_LENGTH
    regs = _build_regs()
    stack_pointer = "s"
    link_reg = "lr"
    endianness = Endianness.BigEndian
    intrinsics = {name: IntrinsicInfo([], []) for name in INTRINSICS}

    def analyze_basic_blocks(self, func, context):
        key = threading.get_ident()
        try:
            _analysing[key] = reader_for_view(func.view)
        except Exception:
            _analysing[key] = None
        try:
            super().analyze_basic_blocks(func, context)
        finally:
            _analysing.pop(key, None)

    def get_instruction_info(self, data, addr):
        instr, operand, length, value = decode_instruction(data, addr)
        if instr is None:
            return None

        result = InstructionInfo()
        result.length = length
        reader = _analysing.get(threading.get_ident())
        mi = method_info(reader, addr) if reader is not None else None
        for kind, target in instruction_branches(instr, value, addr, length, mi):
            result.add_branch(BranchType[kind], target)
        return result

    def get_instruction_text(self, data, addr):
        instr, operand, length, value = decode_instruction(data, addr)
        if instr is None:
            return None
        tokens = []
        tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, "%-14s " % instr))
        tokens += OperandTokens[operand](value)
        return tokens, length
        
    def get_instruction_low_level_il(self, data, addr, il):
        instr, operand, length, value = decode_instruction(data, addr)
        if instr is None:
            return None
        lift_instruction(il, instr, value, LiftContext(addr, length, reader_for_il(il)))
        return length


class JVMCallingConvention(CallingConvention):
    # method arguments arrive in the first local slots
    name = "jvm"
    int_arg_regs = ["l%d_lo" % n for n in range(NUM_LOCAL_REGS)]
    int_return_reg = "r"
    high_int_return_reg = "rh"
    # the platform default convention also decides which registers a call defines in LLIL SSA (before
    # the call's own type is applied): r64 must be among them, or a long/double result read from r64
    # after an invoke resolves to the value before the call
    caller_saved_regs = ["r", "rh", "r64"]
    eligible_for_heuristics = False


class JVMCallCallingConvention(CallingConvention):
    # invoke* call sites: arguments in a<n> (4-byte values in a<n>_lo, like edi in rdi), so a call
    # never clobbers the caller's locals l<n>
    name = "jvm_call"
    int_arg_regs = ["a%d" % n for n in range(NUM_ARG_REGS)]
    # results like methods return them: r, long/double in r64 (explicit return location on the function
    # types; as the convention's int_return_reg an 8-byte register yields no call outputs in BN 6.1)
    int_return_reg = "r"
    high_int_return_reg = "rh"
    caller_saved_regs = ["r", "rh", "r64", "exc"]  # exc: any call may throw
    eligible_for_heuristics = False


def register_arch():
    JVM.register()
    arch = Architecture[ARCH_NAME]
    cc = JVMCallingConvention(arch, "jvm")
    arch.register_calling_convention(cc)
    arch.default_calling_convention = cc
    arch.standalone_platform.default_calling_convention = cc
    arch.register_calling_convention(JVMCallCallingConvention(arch, "jvm_call"))
