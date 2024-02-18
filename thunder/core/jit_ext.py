import thunder
from typing import Any
from collections.abc import ValuesView, Iterable, Iterator
from collections.abc import Callable, Sequence
import weakref
import random
from functools import partial, wraps
import copy
import contextvars
import dis
import warnings
from enum import Enum, auto
from io import StringIO
import time

from thunder.core.compile_data import compile_data_and_stats, get_cache_option, using_symbolic_values
import thunder.clang as clang

from types import (
    CellType,
    ClassMethodDescriptorType,
    CodeType,
    CoroutineType,
    FrameType,
    FunctionType,
    MethodType,
    MethodDescriptorType,
    ModuleType,
    NoneType,
    BuiltinFunctionType,
    BuiltinMethodType,
    MethodDescriptorType,
    MethodWrapperType,
    WrapperDescriptorType,
    TracebackType,
    CellType,
    ModuleType,
    CodeType,
    BuiltinFunctionType,
    FunctionType,
    MethodType,
    GetSetDescriptorType,
)

import torch
from thunder.core.proxies import (
    proxy,
    Proxy,
    NumberProxy,
    StringProxy,
    TensorProxy,
    make_proxy_name,
    variableify,
    unvariableify,
)
from thunder.core.trace import set_tracectx, reset_tracectx, tracectx
from thunder.core.jit import (
    jit,
    _jit,
    _jit_no_unwrap,
    CapsuleType,
    default_callbacks,
    JIT_CALLBACKS,
    JIT_SIGNALS,
    default_opcode_interpreter,
    _default_lookaside_map,
    default_lookaside,
    JITFrame,
    do_raise,
    get_jitcompilectx,
    JitCompileCtx,
    is_opaque,
    Py_NULL,
    member_descriptor,
    WrappedValue,
    unwrap,
    wrap,
    wrap_const,
    PseudoInst,
    ProvenanceRecord,
    jit_needs_wrap,
)
from thunder.core.langctxs import set_langctx, reset_langctx, Languages, resolve_language
from thunder.core.baseutils import extract_callable_name
from thunder.core.codeutils import get_siginfo, SigInfo
import thunder.core.prims as prims
from thunder.common import transform_for_execution
from thunder.core.options import CACHE_OPTIONS, SHARP_EDGES_OPTIONS
from thunder.core.symbol import Symbol, BoundSymbol, is_traceable

from thunder.extend import Executor
from thunder.common import CompileData, CompileStats
from thunder.core.trace import TraceCtx
from thunder.torch import _torch_to_thunder_function_map
from thunder.clang import _clang_fn_set
from thunder.core.proxies import proxy, Variable
from thunder.core.pytree import tree_map
from thunder.core.compile_data import compile_data_and_stats

#
# jit_ext.py implements extensions of thunder's interpreter
#


#
# Functions and objects related to type properties
#

_atomic_copy_types = {
    type(None),
    type(Ellipsis),
    type(NotImplemented),
    int,
    float,
    bool,
    complex,
    bytes,
    str,
    CodeType,
    type,
    range,
    BuiltinFunctionType,
    weakref.ref,
    property,
}

_immutable_types = {
    type(None),
    type(Ellipsis),
    type(NotImplemented),
    int,
    float,
    bool,
    complex,
    bytes,
    str,
    type,
    range,
    BuiltinFunctionType,
    weakref.ref,
    property,
    FunctionType,
    tuple,
    frozenset,
    slice,
}


def is_immutable(val: Any, /) -> bool:
    return type(val) in _immutable_types


_uncopyable_types = {
    ModuleType,
    contextvars.ContextVar,
}


def is_uncopyable(val: Any, /) -> bool:
    return type(val) in _uncopyable_types


#
# Minimal thunder extension
#
# This extension remaps operations to thunder operations and prevents the interpreter from tracing
#   into symbols
# This extension supports detecting and warning or erroring on "sharp edges" -- behavior in the
#   original Python program that cannot be translated to the thunder program

# TODO GTC Add all symbols + methods
# TODO GTC Reuse minimal objects in other executors
# TODO GTC Detect additional sharp edges
#   - inputs that are not function arguments (or their derivatives)
#   - modifying an input
#   - calling a function with a side effect (e.g. randn, print)
# TODO GTC What kind of error should a sharp edge raise?
# TODO GTC Improve sharp edges warnings and errors to show the source line
#   https://github.com/Lightning-AI/lightning-thunder/issues/2099


# Context for the minimal interpreter
class MinimalCtx:
    def __init__(self, *, sharp_edges: SHARP_EDGES_OPTIONS):
        self._sharp_edges: SHARP_EDGES_OPTIONS = sharp_edges

    @property
    def sharp_edges(self) -> SHARP_EDGES_OPTIONS:
        return self._sharp_edges


_minimal_ctx = contextvars.ContextVar("minimalctx")


def set_minimal_ctx(ctx: MinimalCtx) -> Any:
    return _minimal_ctx.set(ctx)


def get_minimal_ctx() -> MinimalCtx:
    return _minimal_ctx.get()


def reset_minimal_ctx(token) -> None:
    _minimal_ctx.reset(token)


# Minimal lookasides

_minimal_lookaside_map = {}

# Translates actual torch functions to their corresponding thunder functions
_minimal_lookaside_map.update(_torch_to_thunder_function_map)


def _minimal_lookaside(fn, *args, **kwargs) -> None | Callable:
    # Identifies the lookaside
    lookaside: None | Callable
    if is_traceable(fn):
        # Performs symbol lookasides
        # NOTE Symbols "lookaside" to themselves; this just prevents their internals from being jitted
        # NOTE clang operations are not symbols, but we still prevent their internals from being jitted
        lookaside = fn
    elif (minimal_lookaside := _minimal_lookaside_map.get(fn, None)) is not None:
        lookaside = minimal_lookaside
    else:
        # Falls through to the interpreter's default lookaside
        lookaside = default_lookaside(fn, *args, **kwargs)

    return lookaside


# Minimal callbacks (necessary for sharp edges)


def _sharp_edge(desc: str, /) -> None:
    sharp_edges: SHARP_EDGES_OPTIONS = get_minimal_ctx().sharp_edges

    s: str = f"{desc} is a sharp edge that cannot be translated to a thunder program unless using interpretation=INTERPRETATION_OPTIONS.TRANSLATE_PYTHON."

    if sharp_edges is SHARP_EDGES_OPTIONS.ERROR:
        raise AssertionError(s)

    if sharp_edges is SHARP_EDGES_OPTIONS.WARN:
        warnings.warn(s)


def _minimal_global_callback(globals_dict: dict, name: str) -> Any:
    value: Any = globals_dict[name]

    # Allows loading global modules.
    #   Some global loads, like these, are so essential that they have to be part of any Python program
    #   translation scheme.
    # TODO GTC Review this check. There may be other types we want to allow. This essentially assumes that
    #   the module is captured at interpretation time, or that global module names will not change for
    #   the lifetime of the program.
    #   We could consider adding a check that the name refers to the same module as it did previously.
    if not isinstance(value, ModuleType):
        _sharp_edge("Loading a global that is not a module")

    return value


_minimal_callbacks: dict[JIT_CALLBACKS, Callable] = {
    JIT_CALLBACKS.GLOBAL_CALLBACK: _minimal_global_callback,
}
_minimal_callbacks = default_callbacks | _minimal_callbacks


# TODO GTC Add debug_log
def minimal_thunder_jit(fn: Callable, /, *, sharp_edges: SHARP_EDGES_OPTIONS) -> Callable:
    ctx: MinimalCtx = MinimalCtx(sharp_edges=sharp_edges)
    jfn = jit(fn, fn_lookaside=_minimal_lookaside, callbacks=_minimal_callbacks)

    def fn_(*args, **kwargs):
        try:
            tok = set_minimal_ctx(ctx)
            return jfn(*args, **kwargs)
        finally:
            reset_minimal_ctx(tok)

    return fn_


#
# Objects and functions related to the meso_thunder_jit context
#


def _meso_sharp_edge(desc: str, /) -> None:
    sharp_edges: SHARP_EDGES_OPTIONS = get_minimal_ctx().sharp_edges

    s: str = f"{desc} This is currently considered a sharp edge even with interpretation=INTERPRETATION_OPTIONS.TRANSLATE_PYTHON. For cases in which we are overly strict, please file an issue. Thank you!"

    if sharp_edges is SHARP_EDGES_OPTIONS.ERROR:
        raise AssertionError(s)

    if sharp_edges is SHARP_EDGES_OPTIONS.WARN:
        warnings.warn(s)


class MesoCtx(MinimalCtx):
    def __init__(self, prologue_trace, computation_trace, *, sharp_edges: SHARP_EDGES_OPTIONS):
        super().__init__(sharp_edges=sharp_edges)

        self._prologue_trace = prologue_trace
        self._computation_trace: TraceCtx = computation_trace
        self._constraints = []

    @property
    def prologue_trace(self) -> TraceCtx:
        return self._prologue_trace

    @property
    def computation_trace(self) -> TraceCtx:
        return self._computation_trace

    def add_constraint(self, constraint):
        self._constraints.append(constraint)

    # NOTE All proxies are constructed in the context of the computation trace, and their
    #   names must be added to the prologue trace (this is done when constructing the prologue trace)
    def proxify(self, val: Any, /, *, name: None | str = None, history: tuple, **kwargs) -> Any:
        # NOTE This marker indicates that the local has not yet been created, and so this skips them
        if val is Py_NULL():
            return val

        # Short-circuits if the val is a WrappedValue (in which case it's a constant that doesn't need to be proxied)
        if isinstance(val, WrappedValue) and val.provenance.inst == PseudoInst.CONSTANT:
            return val

        # Short-circuits if val is already a proxy
        # TODO Check for distinct provenances for types that care about that (mutable collections)
        if isinstance(val, Proxy):
            return val

        if isinstance(val, str):
            return proxy(val, name=name, history=history)

        # TODO Add history
        if isinstance(val, torch.Tensor):
            return proxy(val, name=name, history=history)

        return proxy(val, name=name, history=history)


lit_callbacks: dict[JIT_CALLBACKS, Callable] = {}


def register_lit_callback(key: JIT_CALLBACKS) -> Callable:
    def decorator(fn: Callable):
        assert key not in lit_callbacks
        lit_callbacks[key] = fn
        return fn

    return decorator


#
# lit lookasides
#

# TODO Add all lit operation translations (see https://github.com/Lightning-AI/lightning-thunder/issues/1804)
_lit_lookaside_map = {}

_lit_lookaside_map.update({k: jit_needs_wrap(v) for k, v in _torch_to_thunder_function_map.items()})


# lookaside for getattr. We record the provenance of the attribute but for the core attribute getting, we
# rely on the default JIT getattr lookaside (as returned from default_lookaside)


def _lit_getattr_lookaside(obj: Any, name: str, *maybe_default: Any):
    getattr_lookaside = default_lookaside(getattr)
    assert getattr_lookaside is not None

    value = getattr_lookaside(obj, name, *maybe_default)
    if value is JIT_SIGNALS.EXCEPTION_RAISED:
        return value

    assert isinstance(value, WrappedValue)
    assert isinstance(name, WrappedValue)
    if not isinstance(value.value, Proxy):
        ctx: MesoCtx = get_meso_ctx()
        p = ctx.proxify(value.value, name=name.value, history=value.provenance)
        if p is not value.value:
            value.register_proxy(p)
        return value

    return value


_lit_lookaside_map[getattr] = _lit_getattr_lookaside


# TODO Expand on this
@jit_needs_wrap
def _lit_hasattr_lookaside(obj: Any, name: str):
    hasattr_lookaside = default_lookaside(hasattr) or hasattr
    return hasattr_lookaside(obj, name)


_lit_lookaside_map[hasattr] = _lit_hasattr_lookaside


# We want to record a constraint when we go from proxy -> value here.
# At the same time Python expects to (but we might think to loosen the requirement
# to return a bool for the JIT, return a proxy with origin informaiton and postpone
# recording the constraint to conditional jumps and such.
def _lit_bool_lookaside(wrapped_x: Any) -> bool | JIT_SIGNALS:
    assert isinstance(wrapped_x, WrappedValue)
    bool_lookaside = default_lookaside(bool) or bool
    return bool_lookaside(wrapped_x)


_lit_lookaside_map[bool] = _lit_bool_lookaside

# Adds proxy methods
# NOTE These methods map to themselves, which prevents the interpreter from looking into them
#   This is OK because these methods are written in a tracing-safe manner, and trying to
#   interpreter their internals is unnecessary and would just add complexity at this time


@jit_needs_wrap
def prop_lookaside_helper(meth, /, *args, **kwargs):
    res = meth(*args, **kwargs)
    return res


def prop_lookaside_wrap(attr_getter):
    def fn(obj, /, *args, **kwargs):
        attr = attr_getter(obj)

        if callable(attr):

            def fn_(*args, **kwargs):
                return prop_lookaside_helper(attr, *args, **kwargs)

        else:
            return attr

        return fn_

    return fn


def get_methods_properties(typ):
    for meth_name in dir(typ):
        meth = getattr(typ, meth_name)
        if isinstance(meth, (MethodType, BuiltinMethodType, MethodDescriptorType, WrapperDescriptorType)) and (
            getattr(meth, "__objclass__", None) == typ or (getattr(meth, "__self__", None) == typ)
        ):
            yield meth, meth
        elif isinstance(meth, FunctionType):
            yield meth, meth  # __getattr__
        elif isinstance(meth, property):
            if meth.fget is not None:
                yield meth.fget, prop_lookaside_wrap(meth.fget)


_lit_lookaside_map.update(
    {
        **{fn: jit_needs_wrap(la) for fn, la in get_methods_properties(NumberProxy)},
        **{fn: jit_needs_wrap(la) for fn, la in get_methods_properties(TensorProxy)},
        prop_lookaside_helper: prop_lookaside_helper,
        # review how this works...
        NumberProxy.__add__: jit_needs_wrap(NumberProxy.__add__),
        NumberProxy.__bool__: jit_needs_wrap(NumberProxy.__bool__),  # TODO Review returning a BoolProxy from this
        NumberProxy.__neg__: jit_needs_wrap(NumberProxy.__neg__),
        NumberProxy.__sub__: jit_needs_wrap(NumberProxy.__sub__),
        NumberProxy.__floordiv__: jit_needs_wrap(NumberProxy.__floordiv__),
        NumberProxy.__le__: jit_needs_wrap(NumberProxy.__ge__),
        NumberProxy.__ge__: jit_needs_wrap(NumberProxy.__le__),
        TensorProxy.__add__: jit_needs_wrap(TensorProxy.__add__),
        TensorProxy.__mul__: jit_needs_wrap(TensorProxy.__mul__),
        TensorProxy.__sub__: jit_needs_wrap(TensorProxy.__sub__),
    }
)

# TODO Implement safety --- UNSAFE, PERMISSIVE, SAFE
_safe_functions: set = {
    dict.get,  # TODO Review safety of this
    FunctionType.__new__,
    isinstance,
    member_descriptor.__get__,  # TODO Review the safety of this
    MethodDescriptorType.__get__,  # TODO Review the safety of this
    type,
    tuple.__len__,
    tuple.__getitem__,
    FunctionType.__get__,  # TODO: review safety
    torch._C._get_tracing_state,  # TODO: review safety
    object.__new__,
    object.__init__,
    callable,
    NoneType.__bool__,
    dict.__len__,
    dict.__contains__,
    dict.__getitem__,
    contextvars.ContextVar.get,
    type.__or__,
    list.__new__,
    list.__init__,
    list.__getitem__,
    reversed.__new__,
    CellType.__new__,
    GetSetDescriptorType.__get__,
    Exception.__new__,
    StopIteration.__init__,
}


# TODO Document this function (with steps)
def lit_lookaside(fn, *args, **kwargs) -> None | Callable:
    # Identifies the lookaside
    lookaside: None | Callable
    if isinstance(fn, Symbol) or fn in _clang_fn_set:
        # Performs symbol lookasides
        # NOTE Symbols "lookaside" to themselves; this just prevents their internals from being jitted
        # NOTE clang operations are not symbols, but we still prevent their internals from being jitted
        lookaside = jit_needs_wrap(fn)
    elif (lit_lookaside := _lit_lookaside_map.get(fn, None)) is not None:
        lookaside = lit_lookaside
    else:
        # Falls through to the interpreter's default lookaside
        lookaside = default_lookaside(fn, *args, **kwargs)

    if lookaside is None:
        if is_opaque(fn) and fn not in _safe_functions:
            _meso_sharp_edge(
                f"Trying to call opaque function {extract_callable_name(fn)}, but it's unsupported. Please file an issue requesting supporting."
            )

        return None

    return lookaside


#
# lit callbacks
#

get_meso_ctx = get_minimal_ctx


def _lit_const_callback(value: Any) -> WrappedValue:
    return value


# TODO Do we need to warn here? It would find its way in the wrap callback
def _lit_global_callback(globals_dict: dict, name: str) -> Any:
    # Allows loading the torch module
    value = globals_dict[name]
    if (
        value is torch
        or (value is torch.nn.modules.module._global_backward_pre_hooks)
        or (value is torch.nn.modules.module._global_backward_hooks)
        or (value is torch.nn.modules.module._global_forward_hooks)
        or (value is torch.nn.modules.module._global_forward_pre_hooks)
        or (value is torch.nn.functional)
        or (value is thunder.core.proxies.get_langctx)
        or (value is prop_lookaside_helper)
    ):
        return value

    _meso_sharp_edge(f"Tried to loading global {name}. Global support is limited.")
    return value


def collect_provenance_inst(pr):
    inst = pr.inst
    if isinstance(inst, dis.Instruction):
        inst = inst.opname
    else:
        inst = inst.value
    res = {inst}
    for i in pr.inputs:
        res |= collect_provenance_inst(i)
    return res


safe_provenance_inst = {
    "INPUT_ARGS",
    "INPUT_KWARGS",
    "INPUT_FN",
    "LOAD_ATTR",
    "CONSTANT",
    "BINARY_SUBSCR",
}


def _lit_wrap_callback(value):
    ctx: MesoCtx = get_meso_ctx()

    uvalue = value.value
    if isinstance(uvalue, torch.Tensor):
        # we always want to proxy torch.Tensor, even const
        p = ctx.proxify(uvalue, history=value.provenance)
        if p is not uvalue:
            value.register_proxy(p)
        # TODO: other caching modes
        ctx.add_constraint((clang.check_tensor_shape_and_metadata, p))
    elif value.provenance.inst is PseudoInst.CONSTANT:
        pass
    elif callable(uvalue):
        pass  # we only care if it is called
    elif type(uvalue) in (tuple, list, dict, CellType, ModuleType, set):
        pass  # basic containers are OK, too, subclasses?
    elif isinstance(uvalue, (float, int, complex, str)) and not isinstance(uvalue, Proxy):
        if not (collect_provenance_inst(value.provenance) - safe_provenance_inst):
            p = ctx.proxify(uvalue, history=value.provenance)
            assert p.history is not None, f"{p.history}, {value.provenance} {type(p)}"
            if p is not uvalue:
                value.register_proxy(p)
            # TODO: other caching modes
            if isinstance(uvalue, str):
                ctx.add_constraint((clang.check_string_value, p, uvalue))
            else:
                ctx.add_constraint((clang.check_number_type_and_value, p, uvalue))
        else:
            _meso_sharp_edge(
                f"We are using a (non-const) value of type {type(uvalue).__name__} with provenance {value.provenance}, which is not identified as an input."
            )
    else:
        _meso_sharp_edge(
            f"We are using a (non-const) value of unknown type {type(uvalue).__name__}, which may or may not be safe."
        )


lit_callbacks: dict[JIT_CALLBACKS, Callable] = {
    JIT_CALLBACKS.CONST_CALLBACK: _lit_const_callback,
    JIT_CALLBACKS.GLOBAL_CALLBACK: _lit_global_callback,
    JIT_CALLBACKS.WRAP_CALLBACK: _lit_wrap_callback,
}
lit_callbacks = default_callbacks | lit_callbacks


def get_computation_inputs(computation_trace):
    inputs_list = []
    inputs_set = set()
    for bsym in computation_trace.bound_symbols:
        v: Variable
        for v in bsym.flat_variableified_proxy_args:
            if v.proxy.history is not None:
                if v not in inputs_set:
                    inputs_list.append(v)
                    inputs_set.add(v)
    return inputs_list


def unpack_inputs(ctx, prologue_trace, inputs):
    already_unpacked: dict[int, Proxy] = {}

    # Unpacks the inputs in the prologue trace
    # TODO Generate unpacking constraints
    def unpack(v: Variable | Proxy) -> Proxy:
        p: Proxy
        if isinstance(v, Proxy):
            p = v
        else:
            p = v.proxy

        assert p.history is not None
        if id(p) in already_unpacked:
            return p

        # Adds the name to the prologue trace
        if not prologue_trace.has_name(p.name):
            prologue_trace.add_name(p.name)

        def from_input(provenance, *, new_output=False):
            if new_output:
                if provenance.inst == PseudoInst.INPUT_ARGS:
                    name = "args"
                elif provenance.inst == PseudoInst.INPUT_KWARGS:
                    name = "kwargs"
                elif provenance.inst == PseudoInst.INPUT_FN:
                    name = "fn"

                output = Proxy(name=name)
                provenance.proxy = output
            else:
                output = p
                provenance.proxy = output
            if provenance.inst == PseudoInst.INPUT_FN:
                bsym = prims.unpack_function_obj.bind(output, output=output)
            else:
                bsym = prims.unpack_trivial.bind(output, output=output)
            prologue_trace.bound_symbols.append(bsym)
            return output

        def from_load_attr(provenance, *, new_output=False):
            inputs = [from_provenance(i, new_output=True) for i in provenance.inputs]
            if new_output:
                output = Proxy("obj")
            else:
                output = p
            bsym = prims.unpack_attr.bind(inputs[0], inputs[1], output=output)
            prologue_trace.bound_symbols.append(bsym)
            return output

        def from_constant(provenance, *, new_output=False):
            if isinstance(provenance.value, (int, str)):
                return provenance.value
            else:
                raise NotImplementedError(f"constant of type {type(provenance.value)} {provenance.value}")

        def from_binary_subscr(provenance, *, new_output=False):
            inputs = [from_provenance(i, new_output=True) for i in provenance.inputs]
            obj, idx = inputs
            if new_output:
                output = Proxy("subscr")  # name? collectify?
            else:
                output = p
            if isinstance(idx, (int, str)):
                if isinstance(idx, int):
                    idx = int(idx)
                elif isinstance(idx, str):
                    idx = str(idx)
                bsym = prims.unpack_getitem.bind(obj, idx, output=output)
                prologue_trace.bound_symbols.append(bsym)
            else:
                raise NotImplementedError(f"Unpacking from BINARY_SUBSCR with elaborate inputs {inputs=} {provenance}")
            return output

        def from_opaque(provenance, *, new_output=False):
            fn = provenance.inputs[0]
            args = provenance.inputs[1]
            if fn.inst != PseudoInst.CONSTANT:
                raise NotImplementedError(f"unpacking from nonconstant opaque function")
            if fn.value.__name__ == "__getitem__":
                idx, obj = args.inputs
                # This should be solved in the JIT...
                return from_provenance(
                    ProvenanceRecord(PseudoInst.BINARY_SUBSCR, inputs=[obj, idx]), new_output=new_output
                )
            elif fn.value == GetSetDescriptorType.__get__:
                # todo: find a more elegant way?
                # Arg 1 is the object we want to get the attribute from
                # Arg 2 is the GetSetDescriptor, which contains the arrgument name as .__name__
                assert len(args.inputs) == 3
                assert args.inputs[2].inst == PseudoInst.CONSTANT and isinstance(
                    args.inputs[2].value, GetSetDescriptorType
                )
                return from_provenance(
                    ProvenanceRecord(
                        PseudoInst.LOAD_ATTR,
                        inputs=[
                            args.inputs[1],
                            ProvenanceRecord(PseudoInst.CONSTANT, inputs=[], value=args.inputs[2].value.__name__),
                        ],
                    )
                )
            raise NotImplementedError(f"unpacking from OPAQUE {fn.value} {provenance}")

        def from_provenance(provenance, *, new_output=False):
            if hasattr(provenance, "proxy"):
                return provenance.proxy  # bind?

            inst = provenance.inst
            if isinstance(inst, dis.Instruction):
                inst = inst.opname

            d = {
                "INPUT_ARGS": from_input,
                "INPUT_KWARGS": from_input,
                "INPUT_FN": from_input,
                "LOAD_ATTR": from_load_attr,
                "CONSTANT": from_constant,
                "BINARY_SUBSCR": from_binary_subscr,
                "OPAQUE": from_opaque,
            }

            unpack_fn = d.get(inst)
            if unpack_fn is None:
                raise NotImplementedError(f"Unpacking from {inst} {provenance}")
            res = unpack_fn(provenance, new_output=new_output)
            provenance.proxy = res
            return res

        assert isinstance(p.history, ProvenanceRecord), p.history
        with tracectx(prologue_trace):
            from_provenance(p.history)

        already_unpacked[id(p)] = p

        # Adds cache constraints
        # TODO Consider refactoring these contraints
        # TODO Constrain on rank, device, and dtype
        if isinstance(p, TensorProxy):
            with tracectx(prologue_trace):
                prims.assert_tensor_metadata(p, p.shape, p.device, p.dtype, p.requires_grad)

        return p

    prologue_outputs = []
    for v in inputs:
        prologue_outputs.append(unpack(v))
    prologue_outputs = tuple(prologue_outputs)

    with tracectx(prologue_trace):
        for prim, *args in ctx._constraints:
            for a in args:
                if isinstance(a, Proxy):
                    unpack(a)
            prim(*args)

        prims.python_return(prologue_outputs)

    return prologue_outputs


def meso_thunder_interpreter(
    fn: Callable, args, kwargs, /, *, sharp_edges: SHARP_EDGES_OPTIONS
) -> tuple[TraceCtx, TraceCtx]:
    co: CACHE_OPTIONS = get_cache_option()
    if co not in {CACHE_OPTIONS.CONSTANT_VALUES, CACHE_OPTIONS.NO_CACHING}:
        raise NotImplementedError(f"Only constant constraints is supported")

    prologue_trace: TraceCtx = TraceCtx(fn)
    computation_trace: TraceCtx = TraceCtx()

    si = SigInfo("prologue")
    si.varargs = ("args", None)
    si.varkwargs = ("kwargs", None)
    prologue_trace._siginfo = si

    ctx: MesoCtx = MesoCtx(prologue_trace, computation_trace, sharp_edges=sharp_edges)
    jfn = jit(
        fn,
        fn_lookaside=lit_lookaside,
        callbacks=lit_callbacks,
        with_provenance_tracking=True,
        uncacheable_classes=(torch.Tensor, int, float, str, NoneType),
    )

    with tracectx(computation_trace):
        try:
            tok = set_minimal_ctx(ctx)
            result = jfn(*args, **kwargs)
        finally:
            reset_minimal_ctx(tok)

        prims.python_return(result)

    computation_inputs = get_computation_inputs(computation_trace)
    prologue_outputs = unpack_inputs(ctx, prologue_trace, computation_inputs)

    # Unpacks inputs into the computation trace
    # TODO This currently does the unpacks at the end of he trace, then moves them to the beginning, there's
    #   almost certainly a more elegant way to do this
    with tracectx(computation_trace):
        p: Proxy
        for p in prologue_outputs:
            prims.unpack_trivial(p)

    bsyms = computation_trace.bound_symbols
    computation_trace.bound_symbols = bsyms[-len(prologue_outputs) :] + bsyms[: -len(prologue_outputs)]

    si = SigInfo("computation")
    si.args = [(v.proxy.name, None) for v in computation_inputs]
    computation_trace._siginfo = si
    computation_trace.args = prologue_outputs

    return prologue_trace, computation_trace
