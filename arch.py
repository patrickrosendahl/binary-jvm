"""The JVM Architecture: decoding, instruction text and the calling convention."""
from binaryninja import (Architecture, CallingConvention, RegisterInfo, IntrinsicInfo, InstructionInfo,
                         InstructionTextToken, InstructionTextTokenType, BranchType, Endianness)

from .constants import *
from .opcodes import *
from .lifter import InstructionIL, INTRINSICS, LiftContext, reader_for_il

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
        "r":   RegisterInfo("r", 4),   # return value; rh:r for long/double
        "rh":  RegisterInfo("rh", 4),
    }
    for n in range(NUM_LOCAL_REGS):
        regs["l%d" % n] = RegisterInfo("l%d" % n, 8)
        regs["l%d_lo" % n] = RegisterInfo("l%d" % n, 4, 0)
    # outgoing invoke arguments, one register per argument (receiver first)
    for n in range(NUM_ARG_REGS):
        regs["a%d" % n] = RegisterInfo("a%d" % n, 8)
        regs["a%d_lo" % n] = RegisterInfo("a%d" % n, 4, 0)
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

    def get_instruction_info(self, data, addr):
        instr, operand, length, value = decode_instruction(data, addr)
        if instr is None:
            return None

        result = InstructionInfo()
        result.length = length
        if instr in ("goto", "goto_w"):
            result.add_branch(BranchType.UnconditionalBranch, value)
        elif instr in ("jsr", "jsr_w"):
            result.add_branch(BranchType.CallDestination, value)
        elif instr in ("tableswitch", "lookupswitch"):
            # targets come from the lifted compare chain (and analyze_tables)
            result.add_branch(BranchType.IndirectBranch)
        elif instr in RETURNS:
            result.add_branch(BranchType.FunctionReturn)
        elif instr == "athrow":
            result.add_branch(BranchType.ExceptionBranch)
        elif instr in CONDITIONAL_BRANCHES:
            result.add_branch(BranchType.TrueBranch, value)
            result.add_branch(BranchType.FalseBranch, addr + length)
            
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
        InstructionIL[instr](il, value, LiftContext(addr, length, reader_for_il(il)))
        return length


class JVMCallingConvention(CallingConvention):
    # method arguments arrive in the first local slots
    name = "jvm"
    int_arg_regs = ["l%d_lo" % n for n in range(NUM_LOCAL_REGS)]
    int_return_reg = "r"
    high_int_return_reg = "rh"
    eligible_for_heuristics = False


class JVMCallCallingConvention(CallingConvention):
    # invoke* call sites: arguments in a<n> (4-byte values in a<n>_lo, like edi in rdi), so a call
    # never clobbers the caller's locals l<n>
    name = "jvm_call"
    int_arg_regs = ["a%d" % n for n in range(NUM_ARG_REGS)]
    # results like methods return them: r, or rh:r for long/double (an 8-byte return register yields no
    # call outputs in BN 6.1 on this 32-bit architecture)
    int_return_reg = "r"
    high_int_return_reg = "rh"
    caller_saved_regs = ["r", "rh"]
    eligible_for_heuristics = False


def register_arch():
    JVM.register()
    arch = Architecture[ARCH_NAME]
    cc = JVMCallingConvention(arch, "jvm")
    arch.register_calling_convention(cc)
    arch.default_calling_convention = cc
    arch.standalone_platform.default_calling_convention = cc
    arch.register_calling_convention(JVMCallCallingConvention(arch, "jvm_call"))
