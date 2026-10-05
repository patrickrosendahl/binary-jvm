"""Names and pseudo-memory layout shared by the architecture, lifter and view."""

ARCH_NAME = "JVM"
VIEW_NAME = "JVM Class"

ADDR_SIZE = 4
NUM_LOCAL_REGS = 64  # locals 0..63 are registers (covers >99.9% of methods); higher wide-indexed locals live in pseudo memory
LOCALS_ADDR = 0x8000
MAX_INSTR_LENGTH = 0x10000  # table/lookupswitch; reads stop at the end of the method's segment anyway

METHOD_BASE = 0x1000000    # method i's code is mapped at METHOD_BASE + METHOD_STRIDE*i
METHOD_STRIDE = 0x100000

PSEUDOMEMORY_PRIMITIVES = 0xE0000000 #Pointer to not existing memory containing symbols with names of the primitives these fields represent
PSEUDOMEMORY_TABLE      = 0xF0000000 #Pointer to not existing memory containing symbols with string content of contant table entries
POOL_STRIDE = 8  # pool entries sit 8 bytes apart so a 4/8-byte static field access covers exactly one symbol

def pool_address(index):
    return PSEUDOMEMORY_TABLE + index*POOL_STRIDE

def method_address(index):
    return METHOD_BASE + METHOD_STRIDE*index
