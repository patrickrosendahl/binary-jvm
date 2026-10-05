"""Class-file parser: reads the structure into Binary Ninja types and keeps the constant pool for the lifter."""
import ctypes
import struct

from binaryninja import Type, TypeBuilder

_type_cache = {}

def _cached_type(key, make):
    # Type objects are immutable; building the same handful over and over costs an FFI round trip each
    t = _type_cache.get(key)
    if t is None:
        t = _type_cache[key] = make()
    return t

U1 = lambda: _cached_type("u1", lambda: Type.int(1, False, "u1"))
U2 = lambda: _cached_type("u2", lambda: Type.int(2, False, "u2"))
U4 = lambda: _cached_type("u4", lambda: Type.int(4, False, "u4"))
U8 = lambda: _cached_type("u8", lambda: Type.int(8, False, "u8"))
F4 = lambda: _cached_type("f4", lambda: Type.float(4))
F8 = lambda: _cached_type("f8", lambda: Type.float(8))
BYTE = lambda: _cached_type("byte", lambda: Type.int(1, False))

#super class for all jvm structures
class JVMStructure():
    def __init__(self, cR):
        self.classReader = cR
        self.structure = TypeBuilder.structure() # a binary ninja structure
        self.structure.packed = True # don't align structure
        self.signature = [] # (kind, name) per member: identical layouts share one named type
        self.itype      = None

    def resultingType(self):
        if self.itype is None:
            key = (self.__class__.__name__, tuple(self.signature))
            types = self.classReader.structTypes
            if key not in types:
                oid = self.__class__.__name__
                count = self.classReader.structTypeCount.get(oid, 0)
                self.classReader.structTypeCount[oid] = count + 1
                id = oid if count == 0 else oid+"("+str(count)+")"
                self.classReader.view.define_type(Type.generate_auto_type_id("source", id), id, Type.structure_type(self.structure))
                types[key] = Type.named_type_from_type(id, Type.structure_type(self.structure))
            self.itype = types[key]
        return self.itype

    def append(self, kind, t, name):
        self.structure.append(t, name)
        self.signature.append((kind, name))

    def readByte(self, name=''):
        self.append("u1", U1(), name)
        return self.classReader.readByte()

    def readArray(self, amount, name='', aType = None, kind = "byte"):
        self.append((kind, amount), Type.array(aType or BYTE(), amount), name)
        return self.classReader.readBytes(amount).decode("latin-1")

    def readShort(self, name=''):
        self.append("u2", U2(), name)
        return self.classReader.readShort()

    def readInt(self, name=''):
        self.append("u4", U4(), name)
        return self.classReader.readInt()

    def readLong(self, name=''):
        self.append("u8", U8(), name)
        return self.classReader.readLong()

    def readFloat(self, name=''):
        self.append("f4", F4(), name)
        return self.classReader.readFloat()

    def readDouble(self, name=''):
        self.append("f8", F8(), name)
        return self.classReader.readDouble()

    def readStruct(self, obj, name=''):
        t = obj.resultingType()
        self.append(("struct", obj.__class__.__name__, tuple(obj.signature)), t, name)
        return obj

    def readUTF(self, name=''):
        tname = ''
        if len(name) > 0:
            tname = name + "_len"
        length = self.readShort(tname)
        if len(name) > 0:
            tname = name + "_data"
        raw = self.readArray(length, tname, aType = self.classReader.charType, kind = "char") # I would prefer Type.char()
        return raw.encode("latin-1").decode("utf-8", "replace")  # (modified) UTF-8

UTF_8 = 1
INTEGER = 3
FLOAT = 4
LONG = 5
DOUBLE = 6
CLASS_REFERENCE = 7
STRING_REFERENCE = 8
FIELD_REFERENCE = 9
METHOD_REFERENCE = 10
INTERFACE_REFERENCE = 11
NAME_AND_TYPE = 12
METHOD_HANDLE = 15
METHOD_TYPE = 16
DYNAMIC = 17
INVOKE_DYNAMIC = 18
MODULE = 19
PACKAGE = 20
class JVMConstantPool(JVMStructure):
    
    def __init__(self, r, size):
        JVMStructure.__init__(self, r)
        self.poolSize = size
        self.classReader.constantPool = self
        self.read()
    
    def getTagSize(self, tag):
        if(tag == UTF_8): return -1
        if(tag == INTEGER): return 4
        if(tag == FLOAT): return 4
        if(tag == LONG): return 8
        if(tag == DOUBLE): return 8
        if(tag == CLASS_REFERENCE): return 2
        if(tag == STRING_REFERENCE): return 2
        if(tag == FIELD_REFERENCE): return 4
        if(tag == METHOD_REFERENCE): return 4
        if(tag == INTERFACE_REFERENCE): return 4
        if(tag == NAME_AND_TYPE): return 4
        if(tag == METHOD_HANDLE): return 3
        if(tag == METHOD_TYPE): return 2
        if(tag == DYNAMIC): return 4
        if(tag == INVOKE_DYNAMIC): return 4
        if(tag == MODULE): return 2
        if(tag == PACKAGE): return 2
        return 0
        
    def getTagName(self, tag):
        if(tag == UTF_8): return "UTF_8"
        if(tag == INTEGER): return "INTEGER"
        if(tag == FLOAT): return "FLOAT"
        if(tag == LONG): return "LONG"
        if(tag == DOUBLE): return "DOUBLE"
        if(tag == CLASS_REFERENCE): return "CLASS_REFERENCE"
        if(tag == STRING_REFERENCE): return "STRING_REFERENCE"
        if(tag == FIELD_REFERENCE): return "FIELD_REFERENCE"
        if(tag == METHOD_REFERENCE): return "METHOD_REFERENCE"
        if(tag == INTERFACE_REFERENCE): return "INTERFACE_REFERENCE"
        if(tag == NAME_AND_TYPE): return "NAME_AND_TYPE"
        if(tag == METHOD_HANDLE): return "METHOD_HANDLE"
        if(tag == METHOD_TYPE): return "METHOD_TYPE"
        if(tag == DYNAMIC): return "DYNAMIC"
        if(tag == INVOKE_DYNAMIC): return "INVOKE_DYNAMIC"
        if(tag == MODULE): return "MODULE"
        if(tag == PACKAGE): return "PACKAGE"
        return None
    
    def read(self):
        self.poolContent = [None]*self.poolSize
        self.poolLocation = [0]*self.poolSize
        i = 0
        while i<self.poolSize-1:
            entryName = "constant_pool["+str(i+1)+"]"
            self.poolLocation[i+1] = self.classReader.index()
            tag = self.classReader.readByte() #self.readByte(entryName+"_tag")
            self.classReader.idx -= 1
            size = self.getTagSize(tag)
            if size == 0:
                print("Exception: Error Reading Constant Pool ("+str(tag)+")")
                return
            if  (tag == UTF_8):   self.poolContent[i+1] = self.readStruct(JVMUTF8Info(self.classReader),entryName)
            elif(tag == INTEGER): self.poolContent[i+1] = self.readStruct(JVMIntegerInfo(self.classReader),entryName)
            elif(tag == FLOAT):   self.poolContent[i+1] = self.readStruct(JVMFloatInfo(self.classReader),entryName)
            elif(tag == LONG):    
                self.poolContent[i+1] = self.readStruct(JVMLongInfo(self.classReader),entryName)
                i += 1
            elif(tag == DOUBLE): 
                self.poolContent[i+1] = self.readStruct(JVMDoubleInfo(self.classReader),entryName)
                i += 1
            elif(tag == CLASS_REFERENCE):     self.poolContent[i+1]    = self.readStruct(JVMClassReference(self.classReader),entryName)
            elif(tag == STRING_REFERENCE):    self.poolContent[i+1]    = self.readStruct(JVMStringReference(self.classReader),entryName)
            elif(tag == FIELD_REFERENCE):     self.poolContent[i+1]    = self.readStruct(JVMFieldReference(self.classReader),entryName)
            elif(tag == METHOD_REFERENCE):    self.poolContent[i+1]    = self.readStruct(JVMMethodReference(self.classReader),entryName)
            elif(tag == INTERFACE_REFERENCE): self.poolContent[i+1]    = self.readStruct(JVMInterfaceMethodReference(self.classReader),entryName)
            elif(tag == NAME_AND_TYPE):       self.poolContent[i+1]    = self.readStruct(JVMNameAndTypeDescriptor(self.classReader),entryName)
            elif(tag == METHOD_HANDLE):       self.poolContent[i+1]    = self.readStruct(JVMMethodHandle(self.classReader),entryName)
            elif(tag == METHOD_TYPE):         self.poolContent[i+1]    = self.readStruct(JVMMethodType(self.classReader),entryName)
            elif(tag == DYNAMIC):             self.poolContent[i+1]    = self.readStruct(JVMDynamic(self.classReader),entryName)
            elif(tag == INVOKE_DYNAMIC):      self.poolContent[i+1]    = self.readStruct(JVMInvokeDynamic(self.classReader),entryName)
            elif(tag == MODULE):              self.poolContent[i+1]    = self.readStruct(JVMModuleReference(self.classReader),entryName)
            elif(tag == PACKAGE):             self.poolContent[i+1]    = self.readStruct(JVMPackageReference(self.classReader),entryName)
            i += 1

    def get(self, index):
        index &= 0xFFFF
        if index >= len(self.poolContent):
            return None
        return self.poolContent[index]
  
class JVMUTF8Info(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.value = self.readUTF("value")
        
    def __str__(self):
        return self.value
        
class JVMIntegerInfo(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.value = self.readInt("value")
        
    def __str__(self):
        return str(self.value)
     
class JVMFloatInfo(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.value = self.readFloat("value")
        
    def __str__(self):
        return str(self.value)
     
class JVMLongInfo(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.value = self.readLong("value")
        
    def __str__(self):
        return str(self.value)

     
class JVMDoubleInfo(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.value = self.readDouble("value")
        
    def __str__(self):
        return str(self.value)
      
class JVMClassReference(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.index = self.readShort("index")
        
    def __str__(self):
        return str(self.poolContent.get(self.index))
        
class JVMStringReference(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.index = self.readShort("index")
        
    def __str__(self):
        return '"'+str(self.poolContent.get(self.index))+'"'
        
class JVMFieldReference(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.classReference   = self.readShort("classReference")
        self.nameAndType      = self.readShort("nameAndType")
        
    def __str__(self):
        return str(self.poolContent.get(self.classReference))+"."+str(self.poolContent.get(self.nameAndType))
    
class JVMMethodReference(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.classReference   = self.readShort("classReference")
        self.nameAndType      = self.readShort("nameAndType")
        
    def __str__(self):
        return str(self.poolContent.get(self.classReference))+"."+str(self.poolContent.get(self.nameAndType))
        
class JVMInterfaceMethodReference(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.classReference   = self.readShort("classReference")
        self.nameAndType      = self.readShort("nameAndType")
        
    def __str__(self):
        return str(self.poolContent.get(self.classReference))+"."+str(self.poolContent.get(self.nameAndType))
        
class JVMNameAndTypeDescriptor(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.identifier            = self.readShort("identifier")
        self.encodedTypeDescriptor = self.readShort("encodedTypeDescriptor")
        
    def __str__(self):
        return str(self.poolContent.get(self.identifier))#+"("+str(self.poolContent[self.encodedTypeDescriptor])+")"
        
class JVMMethodHandle(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.kind  = self.readByte("kind")
        self.index = self.readShort("index")
        
    def __str__(self):
        try:
            return str(self.poolContent.get(self.index))
        except Exception:
            return "==Error Parsing=="
        
class JVMMethodType(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.index = self.readShort("index")
        
    def __str__(self):
        try:
            return str(self.poolContent.get(self.index))
        except Exception:
            return "==Error Parsing=="

class JVMInvokeDynamic(JVMStructure):

    def __init__(self,r):
        JVMStructure.__init__(self, r)
        self.poolContent = self.classReader.constantPool
        self.read()
        
    def read(self):
        self.tag   = self.readByte("tag")
        self.bootstrap = self.readShort("bootstrap")
        self.nat = self.readShort("nat")         
        
    def __str__(self):
        try:
            tpl = self.classReader.getBootstrap(self.bootstrap)
            mtd = self.poolContent.get(tpl[0])
            pars = [str(self.poolContent.get(tpl[2][i])) for i in range(tpl[1])]
            if str(mtd) == "java/lang/invoke/LambdaMetafactory.metafactory": #probably not a good idea to work with the serialized version here, doesn't really matter though
                return str(mtd)+" "+pars[1]
            else:
                return str(mtd)+" "+str(pars)
        except Exception:
            return "dynamic:"+str(self.poolContent.get(self.nat))

class JVMDynamic(JVMInvokeDynamic):
    pass

class JVMModuleReference(JVMClassReference):
    pass

class JVMPackageReference(JVMClassReference):
    pass
        
class JVMFieldInfo(JVMStructure):
        
    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
        
    def read(self):
        self.access_flags = self.readShort("access_flags")
        self.name_index   = self.readShort("name_index")
        self.descriptor_index = self.readShort("descriptor_index")
        self.attributes_count = self.readShort("attributes_count")
        self.attributes = [None]*self.attributes_count
        for i in range(self.attributes_count):
            self.attributes[i] = self.readStruct(JVMAttributeInfo(self.classReader), "attribute["+str(i)+"]")
            
class JVMMethodInfo(JVMStructure):

    def __init__(self, r, ind):
        JVMStructure.__init__(self, r)
        self.index = ind
        self.read()
        
    def read(self):
        self.access_flags = self.readShort("access_flags")
        self.name_index   = self.readShort("name_index")
        self.descriptor_index = self.readShort("descriptor_index")
        self.attributes_count = self.readShort("attributes_count")
        self.attributes = [None]*self.attributes_count
        self.code_attribute = None
        for i in range(self.attributes_count):
            self.attributes[i] = self.readStruct(JVMAttributeInfo(self.classReader),"attribute["+str(i)+"]")
            if self.attributes[i].attributeType == "Code":
                self.code_attribute = self.attributes[i]
             
        self.name = str(self.classReader.constantPool.get(self.name_index))
        self.descriptor = str(self.classReader.constantPool.get(self.descriptor_index))

class JVMAttributeInfo(JVMStructure):
    
    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.attribute_name_index = self.readShort("attribute_name_index")
        self.attribute_length   = self.readInt("attribute_length")
        self.attributeType = str(self.classReader.constantPool.get(self.attribute_name_index))
        self.attribute            = None 
        if self.attributeType == "Code":
            self.attribute = self.readStruct(JVMCodeAttribute(self.classReader),"attribute")
        elif self.attributeType == "BootstrapMethods":
            self.attribute = self.readStruct(JVMBootstrapMethods(self.classReader),"attribute")
        elif self.attributeType == "ConstantValue":
            self.attribute = self.readStruct(JVMConstantValueAttribute(self.classReader),"attribute")
        elif self.attributeType == "Exceptions":
            self.attribute = self.readStruct(JVMExceptionsAttribute(self.classReader),"attribute")
        elif self.attributeType == "InnerClasses":
            self.attribute = self.readStruct(JVMInnerClassesAttribute(self.classReader),"attribute")
        elif self.attributeType == "EnclosingMethod":
            self.attribute = self.readStruct(JVMEnclosingMethodAttribute(self.classReader),"attribute")
        elif self.attributeType == "Synthetic":
            self.attribute = self.readStruct(JVMSyntheticAttribute(self.classReader),"attribute")
        elif self.attributeType == "Signature":
            self.attribute = self.readStruct(JVMSignatureAttribute(self.classReader),"attribute")
        elif self.attributeType == "SourceFile":
            self.attribute = self.readStruct(JVMSourceFileAttribute(self.classReader),"attribute")
        elif self.attributeType == "LineNumberTable":
            self.attribute = self.readStruct(JVMLineNumberTableAttribute(self.classReader),"attribute")
        elif self.attributeType == "LocalVariableTable":
            self.attribute = self.readStruct(JVMLocalVariableTableAttribute(self.classReader),"attribute")
        elif self.attributeType == "Deprecated":
            self.attribute = self.readStruct(JVMDeprecatedAttribute(self.classReader),"attribute")
        else:
            self.readArray(self.attribute_length, "attribute")
 
class JVMDeprecatedAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        pass
        
class JVMLocalVariableTypeTableAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.local_variable_type_table_length  = self.readShort("local_variable_type_table_length")
        self.local_variable_type_table = []
        for i in range(self.local_variable_type_table_length):
            self.local_variable_type_table.append([self.readShort("start_pc["+str(i)+"]"),self.readShort("length["+str(i)+"]"),self.readShort("name_index["+str(i)+"]"),self.readShort("signature_index["+str(i)+"]"),self.readShort("index["+str(i)+"]")])
 
class JVMLocalVariableTableAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.local_variable_table_length  = self.readShort("local_variable_table_length")
        self.local_variable_table = []
        for i in range(self.local_variable_table_length):
            self.local_variable_table.append([self.readShort("start_pc["+str(i)+"]"),self.readShort("length["+str(i)+"]"),self.readShort("name_index["+str(i)+"]"),self.readShort("descriptor_index["+str(i)+"]"),self.readShort("index["+str(i)+"]")])
  
class JVMLineNumberTableAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.line_number_table_length  = self.readShort("line_number_table_length")
        self.line_number_table = []
        for i in range(self.line_number_table_length):
            self.line_number_table.append([self.readShort("start_pc["+str(i)+"]"),self.readShort("line_number["+str(i)+"]")])

#SourceDebugExtension 
 
class JVMSourceFileAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.sourcefile_index  = self.readShort("sourcefile_index")
        
 
class JVMSignatureAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.signature_index  = self.readShort("signature_index")
        
 
class JVMSyntheticAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        pass
 
class JVMEnclosingMethodAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.class_index  = self.readShort("class_index")
        self.method_index = self.readShort("method_index")
        
class JVMInnerClassesAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.number_of_classes = self.readShort("number_of_classes")
        self.classes = []
        for i in range(self.number_of_classes):
            self.classes.append([self.readShort("inner_class_info_index["+str(i)+"]"),self.readShort("outer_class_info_index["+str(i)+"]"),self.readShort("inner_name_index["+str(i)+"]"),self.readShort("inner_class_access_flags["+str(i)+"]")])
            
class JVMExceptionsAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.number_of_exceptions = self.readShort("number_of_exceptions")
        self.exception_index_table = []
        for i in range(self.number_of_exceptions):
            self.exception_index_table.append(self.readShort("exception_index_table["+str(i)+"]"))
        
class JVMConstantValueAttribute(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.constantvalue_index  = self.readShort("constantvalue_index")
        
        
class JVMCodeAttribute(JVMStructure):
    
    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
    
    def read(self):
        self.max_stack = self.readShort("max_stack")
        self.max_locals = self.readShort("max_locals")
        self.code_length = self.readInt("code_length")
        self.start_address = self.classReader.index()
        self.code = self.readArray(self.code_length,"code")
        self.end_address = self.classReader.index()
        self.exception_table_length = self.readShort("exception_table_length")
        self.exception_table = []
        for i in range(self.exception_table_length):
            # TODO Exception structure
            self.exception_table.append((self.readShort("start_pc["+str(i)+"]"),self.readShort("end_pc["+str(i)+"]"),self.readShort("handler_pc["+str(i)+"]"),self.readShort("catch_type["+str(i)+"]")))
        self.attributes_count = self.readShort("attributes_count")
        self.attributes = []
        for i in range(self.attributes_count):
            self.attributes.append(self.readStruct(JVMAttributeInfo(self.classReader),"attribute["+str(i)+"]"))

class JVMBootstrapMethods(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
        
    def read(self):
        self.num_bootstrap_methods = self.readShort("num_bootstrap_methods")
        self.bootstrap_methods = []
        for i in range(self.num_bootstrap_methods):
            bootstrap_method_ref = self.readShort("bootstrap_method_ref["+str(i)+"]")
            num_bootstrap_arguments = self.readShort("num_bootstrap_arguments["+str(i)+"]")
            bootstrap_arguments = []
            for j in range(num_bootstrap_arguments):
                bootstrap_arguments.append(self.readShort("bootstrap_arguments["+str(j)+"]"))
            self.bootstrap_methods.append([bootstrap_method_ref,num_bootstrap_arguments,bootstrap_arguments])
            
class JVMClassStructure(JVMStructure):

    def __init__(self, r):
        JVMStructure.__init__(self, r)
        self.read()
        
    def read(self):
        self.magic           = self.readInt("magic")
        self.minor_version   = self.readShort("minor_version")
        self.major_version   = self.readShort("major_version")
        self.constant_pool_count = self.readShort("constant_pool_count")
        self.constantPool    = self.readStruct(JVMConstantPool(self.classReader, self.constant_pool_count), "constantPool")
        self.access_flags    = self.readShort("access_flags")
        self.this_class      = self.readShort("this_class")
        self.super_class     = self.readShort("super_class")
        self.interface_count = self.readShort("interface_count")
        self.interfaces      = []
        for i in range(self.interface_count):
            self.interfaces.append(self.readShort("interface["+str(i)+"]"))
        self.fields_count = self.readShort("fields_count")
        self.fields          = []
        for i in range(self.fields_count):
            self.fields.append(self.readStruct(JVMFieldInfo(self.classReader),"field["+str(i)+"]"))
        self.methods_count = self.readShort("methods_count")
        self.methods         = []
        for i in range(self.methods_count):
            self.methods.append(self.readStruct(JVMMethodInfo(self.classReader, i),"method["+str(i)+"]"))
        self.attributes_count = self.readShort("attributes_count")
        self.attributes      = []
        for i in range(self.attributes_count):
            attr = self.readStruct(JVMAttributeInfo(self.classReader),"attribute["+str(i)+"]")
            self.attributes.append(attr)
            if attr.attributeType == "BootstrapMethods":
                self.classReader.setBootstrap(attr)
        
        
class JVMClassReader():
    def __init__(self,vi,da):
        self.view = vi
        self.data = da.read(0, da.length) # read the file once; per-field reads through the view are slow
        self.idx = 0    
        self.classStruct = None
        self.constantPool = None
        self.bootstrap_attribute = None
        self.structTypes = {}
        self.structTypeCount = {}
    def reset(self):
        self.idx = 0
    def unpack(self, fmt, size):
        value = struct.unpack_from(fmt, self.data, self.idx)[0]
        self.idx += size
        return value
    def readLong(self):
        return self.unpack(">Q", 8)
    def readDouble(self):
        return self.unpack(">d", 8)
    def readFloat(self):
        return self.unpack(">f", 4)
    def readInt(self):
        return self.unpack(">I", 4)
    def readShort(self):
        return self.unpack(">H", 2)
    def readByte(self):
        value = self.data[self.idx]
        self.idx += 1
        return value
    def readBytes(self, amount):
        value = self.data[self.idx:self.idx+amount]
        self.idx += amount
        return value
    def index(self):
        return self.idx

    def poolEntry(self, index):
        if self.constantPool is None:
            return None
        return self.constantPool.get(index)

    def memberDescriptor(self, index):
        """type descriptor of a Fieldref/Methodref/InterfaceMethodref/InvokeDynamic/Dynamic pool entry"""
        entry = self.poolEntry(index)
        if isinstance(entry, (JVMFieldReference, JVMMethodReference, JVMInterfaceMethodReference)):
            nat = self.poolEntry(entry.nameAndType)
        elif isinstance(entry, JVMInvokeDynamic):
            nat = self.poolEntry(entry.nat)
        else:
            return None
        if not isinstance(nat, JVMNameAndTypeDescriptor):
            return None
        desc = self.poolEntry(nat.encodedTypeDescriptor)
        return str(desc) if isinstance(desc, JVMUTF8Info) else None
        
    #This is some special Dynamic Invocation code
    def setBootstrap(self,attr):
        self.bootstrap_attribute = attr
      
    def getBootstrap(self,index):
        if self.bootstrap_attribute == None: return None
        return self.bootstrap_attribute.attribute.bootstrap_methods[index]


# Registry of parsed classes, keyed by the core BinaryView handle, so the lifter can resolve
# constant-pool descriptors (stack effects of invokes / field accesses, ldc constants).
_class_readers = {}

def view_key(view):
    return ctypes.addressof(view.handle.contents)

def register_reader(view, reader):
    _class_readers[view_key(view)] = reader

def reader_for_view(view):
    return _class_readers.get(view_key(view))
