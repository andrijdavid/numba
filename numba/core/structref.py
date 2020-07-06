"""Utilities for defining a mutable struct.

A mutable struct is passed by reference;
hence, structref (a reference to a struct).

"""

from numba.core import types, imputils, cgutils
from numba.core.datamodel import default_manager, models
from numba.core.extending import (
    infer_getattr,
    lower_getattr_generic,
    lower_setattr_generic,
    box,
    unbox,
    NativeValue,
    intrinsic,
)
from numba.core.typing.templates import AttributeTemplate


class _Utils:
    def __init__(self, context, builder, struct_type):
        self.context = context
        self.builder = builder
        self.struct_type = struct_type

    def new_struct_ref(self, mi):
        context = self.context
        builder = self.builder
        struct_type = self.struct_type

        st = cgutils.create_struct_proxy(struct_type)(context, builder)
        st.meminfo = mi
        return st

    def get_struct_ref(self, val):
        context = self.context
        builder = self.builder
        struct_type = self.struct_type

        return cgutils.create_struct_proxy(struct_type)(
            context, builder, value=val
        )

    def get_data_pointer(self, val):
        context = self.context
        builder = self.builder
        struct_type = self.struct_type

        structval = self.get_struct_ref(val)
        meminfo = structval.meminfo
        data_ptr = context.nrt.meminfo_data(builder, meminfo)

        valtype = struct_type.get_data_type()
        model = context.data_model_manager[valtype]
        alloc_type = model.get_value_type()
        data_ptr = builder.bitcast(data_ptr, alloc_type.as_pointer())
        return data_ptr

    def get_data_struct(self, val):
        context = self.context
        builder = self.builder
        struct_type = self.struct_type

        data_ptr = self.get_data_pointer(val)
        valtype = struct_type.get_data_type()
        dataval = cgutils.create_struct_proxy(valtype)(
            context, builder, ref=data_ptr
        )
        return dataval


def define_attributes(struct_typeclass):
    @infer_getattr
    class StructAttribute(AttributeTemplate):
        key = struct_typeclass

        def generic_resolve(self, typ, attr):
            if attr in typ.field_dict:
                attrty = typ.field_dict[attr]
                return attrty

    @lower_getattr_generic(struct_typeclass)
    def struct_getattr_impl(context, builder, typ, val, attr):
        utils = _Utils(context, builder, typ)
        dataval = utils.get_data_struct(val)
        ret = getattr(dataval, attr)
        fieldtype = typ.field_dict[attr]
        return imputils.impl_ret_borrowed(context, builder, fieldtype, ret)

    @lower_setattr_generic(struct_typeclass)
    def struct_setattr_impl(context, builder, sig, args, attr):
        [inst_type, val_type] = sig.args
        [instance, val] = args
        utils = _Utils(context, builder, inst_type)
        dataval = utils.get_data_struct(instance)
        # read old
        old_value = getattr(dataval, attr, val)
        # incref new value
        context.nrt.incref(builder, val_type, val)
        # decref old value (must be last in case new value is old value)
        context.nrt.decref(builder, val_type, old_value)
        # write new
        setattr(dataval, attr, val)


def define_boxing(struct_type, obj_ctor):
    @box(struct_type)
    def box_struct_ref(typ, val, c):
        """
        Convert a raw pointer to a Python int.
        """
        utils = _Utils(c.context, c.builder, typ)
        struct_ref = utils.get_struct_ref(val)
        meminfo = struct_ref.meminfo

        mip_type = types.MemInfoPointer(types.voidptr)
        boxed_meminfo = c.box(mip_type, meminfo)

        ctor_pyfunc = c.pyapi.unserialize(c.pyapi.serialize_object(obj_ctor))
        ty_pyobj = c.pyapi.unserialize(c.pyapi.serialize_object(typ))
        res = c.pyapi.call_function_objargs(
            ctor_pyfunc, [ty_pyobj, boxed_meminfo],
        )
        c.pyapi.decref(ctor_pyfunc)
        c.pyapi.decref(ty_pyobj)
        c.pyapi.decref(boxed_meminfo)
        return res

    @unbox(struct_type)
    def unbox_struct_ref(typ, obj, c):
        mi_obj = c.pyapi.object_getattr_string(obj, "_mi")

        mip_type = types.MemInfoPointer(types.voidptr)

        mi = c.unbox(mip_type, mi_obj).value

        utils = _Utils(c.context, c.builder, typ)
        struct_ref = utils.new_struct_ref(mi)
        out = struct_ref._getvalue()

        c.pyapi.decref(mi_obj)
        return NativeValue(out)


def register(struct_type):
    default_manager.register(struct_type, models.StructRefModel)
    define_attributes(struct_type)


@intrinsic
def new(typingctx, struct_type):
    from numba.experimental.jitclass.base import imp_dtor

    inst_type = struct_type.instance_type

    def codegen(context, builder, signature, args):
        # FIXME: mostly the same as jitclass ctor_impl()
        model = context.data_model_manager[inst_type.get_data_type()]
        alloc_type = model.get_value_type()
        alloc_size = context.get_abi_sizeof(alloc_type)

        meminfo = context.nrt.meminfo_alloc_dtor(
            builder,
            context.get_constant(types.uintp, alloc_size),
            imp_dtor(context, builder.module, inst_type),
        )
        data_pointer = context.nrt.meminfo_data(builder, meminfo)
        data_pointer = builder.bitcast(data_pointer, alloc_type.as_pointer())

        # Nullify all data
        builder.store(cgutils.get_null_value(alloc_type), data_pointer)

        inst_struct = context.make_helper(builder, inst_type)
        inst_struct.meminfo = meminfo

        return inst_struct._getvalue()

    sig = inst_type(struct_type)
    return sig, codegen
