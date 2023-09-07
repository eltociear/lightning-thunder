import copy
import difflib
import dis
import io
import inspect
import itertools
import re
import sys
import textwrap
from typing import Callable, List, Optional
from collections.abc import Iterator

import thunder.core.script.frontend as frontend
import thunder.core.script.protograph as protograph
from thunder.core.script.protograph_passes import apply_protograph_passes
import thunder.core.script.python_ir_data as python_ir_data
from thunder.core.utils import enable_debug_asserts

import pytest

enable_debug_asserts()

PARSE_SPECIFICATION = list[
    tuple[list[tuple[str, str]], list[tuple[int, bool]]]  # (opname, argrepr)  # (target_index, is_jump)
]

# Block index (optional), opname, inputs, outputs
FLOW_SPECIFICATION_ENTRY = tuple[Optional[int], str, tuple[tuple[str, ...], ...], tuple[str, ...]]
FLOW_SPECIFICATION = tuple[FLOW_SPECIFICATION_ENTRY, ...]

TEST_CASES = []
DONT_CHECK_FLOW = "DONT_CHECK_FLOW"


def add_parse_test(parse_spec: Optional[str] = None, flow_spec: Optional[str] = None):
    def wrapper(f):
        TEST_CASES.append((f, parse_spec, flow_spec))
        return f

    return wrapper


@add_parse_test(
    """
    LOAD_FAST                0 (x)                    ║    LOAD_FAST                   x
    LOAD_CONST               1 (1)                    ║    LOAD_CONST                  1
    BINARY_ADD                                        ║    BINARY_ADD
    RETURN_VALUE                                      ║    RETURN_VALUE
""",
    r"""
    BINARY_ADD:      (x, 1) -> out
    RETURN_VALUE:    (out) ->
""",
)
def simple_fn(x):
    return x + 1


@add_parse_test(
    """
        LOAD_FAST                0 (x)                ║    LOAD_FAST                   x
        POP_JUMP_IF_FALSE        6 (to 12)            ║    POP_JUMP_IF_FALSE
        LOAD_FAST                0 (x)                ║        -> 1(False)
        LOAD_CONST               1 (2)                ║        -> 2(True)
        INPLACE_ADD                                   ║
        STORE_FAST               0 (x)                ║    LOAD_FAST                   x
                                                      ║    LOAD_CONST                  2
>>   12 LOAD_FAST                0 (x)                ║    INPLACE_ADD
        LOAD_CONST               2 (1)                ║    STORE_FAST                  x
        BINARY_ADD                                    ║    JUMP_ABSOLUTE
        RETURN_VALUE                                  ║        -> 2(True)
                                                      ║
                                                      ║    LOAD_FAST                   x
                                                      ║    LOAD_CONST                  1
                                                      ║    BINARY_ADD
                                                      ║    RETURN_VALUE
""",
    r"""
    1)  INPLACE_ADD:     (x, 2) -> x_1
    2)  BINARY_ADD:      (U[x_1, x], 1) -> out
    2)  RETURN_VALUE:    (out) ->
""",
)
def simple_if_fn(x):
    if x:
        x += 2
    return x + 1


@add_parse_test(
    """
        LOAD_FAST                1 (mask)             ║    LOAD_FAST                   mask
        LOAD_METHOD              0 (any)              ║    LOAD_METHOD                 any
        CALL_METHOD              0                    ║    CALL_METHOD
        STORE_FAST               4 (has_mask)         ║    STORE_FAST                  has_mask
        LOAD_FAST                2 (layer_0)          ║    LOAD_FAST                   layer_0
        LOAD_FAST                0 (x)                ║    LOAD_FAST                   x
        LOAD_FAST                4 (has_mask)         ║    LOAD_FAST                   has_mask
        POP_JUMP_IF_FALSE       10 (to 20)            ║    POP_JUMP_IF_FALSE
        LOAD_FAST                1 (mask)             ║        -> 1(False)
        JUMP_FORWARD             1 (to 22)            ║        -> 2(True)
                                                      ║
>>   20 LOAD_CONST               0 (None)             ║    LOAD_FAST                   mask
                                                      ║    JUMP_FORWARD
>>   22 CALL_FUNCTION            2                    ║        -> 3(True)
        STORE_FAST               0 (x)                ║
        LOAD_FAST                3 (layer_1)          ║    LOAD_CONST                  None
        LOAD_FAST                0 (x)                ║    JUMP_ABSOLUTE
        LOAD_FAST                4 (has_mask)         ║        -> 3(True)
        POP_JUMP_IF_FALSE       22 (to 44)            ║
        LOAD_FAST                1 (mask)             ║    CALL_FUNCTION
        CALL_FUNCTION            2                    ║    STORE_FAST                  x
        STORE_FAST               0 (x)                ║    LOAD_FAST                   layer_1
        LOAD_FAST                0 (x)                ║    LOAD_FAST                   x
        RETURN_VALUE                                  ║    LOAD_FAST                   has_mask
                                                      ║    POP_JUMP_IF_FALSE
>>   44 LOAD_CONST               0 (None)             ║        -> 4(False)
        CALL_FUNCTION            2                    ║        -> 5(True)
        STORE_FAST               0 (x)                ║
        LOAD_FAST                0 (x)                ║    LOAD_FAST                   mask
        RETURN_VALUE                                  ║    CALL_FUNCTION
                                                      ║    STORE_FAST                  x
                                                      ║    LOAD_FAST                   x
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 6(True)
                                                      ║
                                                      ║    LOAD_CONST                  None
                                                      ║    CALL_FUNCTION
                                                      ║    STORE_FAST                  x
                                                      ║    LOAD_FAST                   x
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 6(True)
                                                      ║
                                                      ║    RETURN_VALUE
""",
    r"""
    0)  LOAD_METHOD:     (mask) -> any
    0)  CALL_METHOD:     (any, mask) -> has_mask
    3)  CALL_FUNCTION:   (layer_0, x, U[mask, None]) -> x_1
    4)  CALL_FUNCTION:   (layer_1, x_1, mask) -> x_2_mask
    5)  CALL_FUNCTION:   (layer_1, x_1, None) -> x_2_no_mask
    6)  RETURN_VALUE:    (U[x_2_mask, x_2_no_mask]) ->
""",
)
def cse_candidate(x, mask, layer_0, layer_1):
    has_mask = mask.any()
    x = layer_0(x, mask if has_mask else None)
    x = layer_1(x, mask if has_mask else None)
    return x


@add_parse_test(
    """
        LOAD_GLOBAL              0 (range)            ║    LOAD_GLOBAL                 range
        LOAD_CONST               1 (4)                ║    LOAD_CONST                  4
        CALL_FUNCTION            1                    ║    CALL_FUNCTION
        GET_ITER                                      ║    GET_ITER
                                                      ║    JUMP_ABSOLUTE
>>    8 FOR_ITER                 6 (to 22)            ║        -> 1(True)
        STORE_FAST               2 (_)                ║
        LOAD_FAST                0 (x)                ║    FOR_ITER
        LOAD_FAST                1 (y)                ║        -> 2(False)
        INPLACE_ADD                                   ║        -> 4(True)
        STORE_FAST               0 (x)                ║
        JUMP_ABSOLUTE            4 (to 8)             ║    FOR_ITER__NoJumpEpilogue
                                                      ║        -> 3(False)
>>   22 LOAD_FAST                0 (x)                ║
        RETURN_VALUE                                  ║    STORE_FAST                  _
                                                      ║    LOAD_FAST                   x
                                                      ║    LOAD_FAST                   y
                                                      ║    INPLACE_ADD
                                                      ║    STORE_FAST                  x
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 1(True)
                                                      ║
                                                      ║    FOR_ITER__JumpEpilogue
                                                      ║        -> 5(True)
                                                      ║
                                                      ║    LOAD_FAST                   x
                                                      ║    RETURN_VALUE
""",
    r"""
    0)  CALL_FUNCTION:              (range, 4) -> range_generator
    0)  GET_ITER:                   (range_generator) -> range_iter
    2)  FOR_ITER__NoJumpEpilogue:   (range_iter) -> _
    3)  INPLACE_ADD:                (U[x_1, x], y) -> x_1
    5)  RETURN_VALUE:               (U[x_1, x]) ->
""",
)
def simple_loop_fn(x, y):
    # NOTE:
    #   preprocessing doesn't understand that `range(4)` guarantees at least
    #   one pass through the loop which is why the return is `U[x_1, x]`
    #   instead of `x_1`.
    for _ in range(4):
        x += y
    return x


@add_parse_test(
    """
        LOAD_GLOBAL              0 (range)            ║    LOAD_GLOBAL                 range
        LOAD_CONST               1 (10)               ║    LOAD_CONST                  10
        CALL_FUNCTION            1                    ║    CALL_FUNCTION
        GET_ITER                                      ║    GET_ITER
                                                      ║    JUMP_ABSOLUTE
>>    8 FOR_ITER                 9 (to 28)            ║        -> 1(True)
        STORE_FAST               1 (i)                ║
        LOAD_FAST                1 (i)                ║    FOR_ITER
        LOAD_FAST                0 (x)                ║        -> 2(False)
        COMPARE_OP               4 (>)                ║        -> 5(True)
        POP_JUMP_IF_FALSE       13 (to 26)            ║
        POP_TOP                                       ║    FOR_ITER__NoJumpEpilogue
        LOAD_FAST                1 (i)                ║        -> 3(False)
        RETURN_VALUE                                  ║
                                                      ║    STORE_FAST                  i
>>   26 JUMP_ABSOLUTE            4 (to 8)             ║    LOAD_FAST                   i
                                                      ║    LOAD_FAST                   x
>>   28 LOAD_FAST                1 (i)                ║    COMPARE_OP                  >
        RETURN_VALUE                                  ║    POP_JUMP_IF_FALSE
                                                      ║        -> 4(False)
                                                      ║        -> 6(True)
                                                      ║
                                                      ║    POP_TOP
                                                      ║    LOAD_FAST                   i
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 8(True)
                                                      ║
                                                      ║    FOR_ITER__JumpEpilogue
                                                      ║        -> 7(True)
                                                      ║
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 1(True)
                                                      ║
                                                      ║    LOAD_FAST                   i
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 8(True)
                                                      ║
                                                      ║    RETURN_VALUE
""",
    r"""
    0)  CALL_FUNCTION:              (range, 10) -> range_generator
    0)  GET_ITER:                   (range_generator) -> range_iter
    2)  FOR_ITER__NoJumpEpilogue:   (range_iter) -> i
    3)  COMPARE_OP:                 (i, x) -> cmp
    8)  RETURN_VALUE:               (U[MISSING, i]) ->
""",
)
def loop_with_break(x):
    for i in range(10):
        if i > x:
            break
    return i


@add_parse_test(
    """
        LOAD_FAST                1 (k)                ║    LOAD_FAST                   k
        GET_ITER                                      ║    GET_ITER
                                                      ║    JUMP_ABSOLUTE               2
>>    4 FOR_ITER                17 (to 40)            ║        -> 1(True)
        STORE_FAST               2 (_)                ║
        LOAD_FAST                0 (x)                ║    FOR_ITER
        LOAD_CONST               1 (1)                ║        -> 2(False)
        INPLACE_ADD                                   ║        -> 5(True)
        STORE_FAST               0 (x)                ║
        LOAD_GLOBAL              0 (done_fn)          ║    FOR_ITER__NoJumpEpilogue
        LOAD_FAST                1 (k)                ║        -> 3(False)
        CALL_FUNCTION            1                    ║
        POP_JUMP_IF_FALSE       19 (to 38)            ║    STORE_FAST                  _
        LOAD_FAST                0 (x)                ║    LOAD_FAST                   x
        LOAD_CONST               2 (2)                ║    LOAD_CONST                  1
        INPLACE_MULTIPLY                              ║    INPLACE_ADD
        STORE_FAST               0 (x)                ║    STORE_FAST                  x
        POP_TOP                                       ║    LOAD_GLOBAL                 done_fn
        LOAD_FAST                0 (x)                ║    LOAD_FAST                   k
        RETURN_VALUE                                  ║    CALL_FUNCTION
                                                      ║    POP_JUMP_IF_FALSE
>>   38 JUMP_ABSOLUTE            2 (to 4)             ║        -> 4(False)
                                                      ║        -> 6(True)
>>   40 LOAD_FAST                0 (x)                ║
        LOAD_CONST               1 (1)                ║    LOAD_FAST                   x
        INPLACE_SUBTRACT                              ║    LOAD_CONST                  2
        STORE_FAST               0 (x)                ║    INPLACE_MULTIPLY
        LOAD_FAST                0 (x)                ║    STORE_FAST                  x
        RETURN_VALUE                                  ║    POP_TOP
                                                      ║    LOAD_FAST                   x
                                                      ║    JUMP_ABSOLUTE               27
                                                      ║        -> 8(True)
                                                      ║
                                                      ║    FOR_ITER__JumpEpilogue
                                                      ║        -> 7(True)
                                                      ║
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 1(True)
                                                      ║
                                                      ║    LOAD_FAST                   x
                                                      ║    LOAD_CONST                  1
                                                      ║    INPLACE_SUBTRACT
                                                      ║    STORE_FAST                  x
                                                      ║    LOAD_FAST                   x
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 8(True)
                                                      ║
                                                      ║    RETURN_VALUE
""",
    r"""
    0)  GET_ITER:                   (k) -> k_iter
    2)  FOR_ITER__NoJumpEpilogue:   (k_iter) -> _
    3)  INPLACE_ADD:                (U[x, x_1], 1) -> x_1
    3)  CALL_FUNCTION:              (done_fn, k) -> break_cnd
    4)  INPLACE_MULTIPLY:           (x_1, 2) -> x_break_path
    7)  INPLACE_SUBTRACT:           (U[x, x_1], 1) -> x_normal_path
    8)  RETURN_VALUE:               (U[x_break_path, x_normal_path]) ->
""",
)
def loop_with_else(x, k):
    for _ in k:
        x += 1

        # Without this break we will always execute the `else` branch, so Python
        # will optimize out the jump and inline the subtraction after the loop.
        #
        # And if we don't gate it behind a conditional it the parser is smart
        # enough to elide the jump to the start of the loop.
        if done_fn(k):
            x *= 2
            break
    else:
        x -= 1

    return x


def done_fn(_):
    return True


@add_parse_test(
    """
        LOAD_FAST                0 (x)       ║
        LOAD_CONST               1 (2)       ║
        BUILD_TUPLE              2           ║
        STORE_FAST               1 (t)       ║
        LOAD_FAST                1 (t)       ║
        UNPACK_SEQUENCE          2           ║
        STORE_FAST               2 (a)       ║
        STORE_FAST               3 (_)       ║
        LOAD_FAST                2 (a)       ║
        RETURN_VALUE                         ║
        """,
    """
        0)  BUILD_TUPLE:     (x, 2) -> OUTPUT_0
        0)  RETURN_VALUE:    (x) ->
        """,
)
def tuple_fold(x):
    t = (x, 2)
    a, _ = t
    return a


@add_parse_test(
    """
        LOAD_FAST                0 (x)       ║
        LOAD_CONST               1 (2)       ║
        BUILD_TUPLE              2           ║
        STORE_FAST               1 (t)       ║
        LOAD_FAST                1 (t)       ║
        UNPACK_EX                1           ║
        STORE_FAST               2 (a)       ║
        STORE_FAST               3 (_)       ║
        LOAD_FAST                2 (a)       ║
        RETURN_VALUE                         ║
        """,
    # TODO(robieta, apaz-cli): Figure out UNPACK_EX indexing.
    flow_spec=DONT_CHECK_FLOW,
)
def tuple_fold_ex(x):
    t = (x, 2)
    a, *_ = t
    return a


@add_parse_test(
    """
        LOAD_FAST                0 (x)                ║    LOAD_FAST                   x
        POP_JUMP_IF_FALSE       17 (to 34)            ║    POP_JUMP_IF_FALSE
                                                      ║        -> 1(False)
>>    4 LOAD_FAST                1 (inner)            ║        -> 7(True)
        GET_ITER                                      ║
                                                      ║    LOAD_FAST                   inner
>>    8 FOR_ITER                 6 (to 22)            ║    GET_ITER
        STORE_FAST               2 (_)                ║    JUMP_ABSOLUTE
        LOAD_FAST                0 (x)                ║        -> 2(True)
        LOAD_CONST               1 (2)                ║
        INPLACE_TRUE_DIVIDE                           ║    FOR_ITER
        STORE_FAST               0 (x)                ║        -> 3(False)
        JUMP_ABSOLUTE            4 (to 8)             ║        -> 5(True)
                                                      ║
>>   22 LOAD_FAST                0 (x)                ║    FOR_ITER__NoJumpEpilogue
        LOAD_CONST               2 (1)                ║        -> 4(False)
        INPLACE_FLOOR_DIVIDE                          ║
        STORE_FAST               0 (x)                ║    STORE_FAST                  _
        LOAD_FAST                0 (x)                ║    LOAD_FAST                   x
        POP_JUMP_IF_TRUE         2 (to 4)             ║    LOAD_CONST                  2
                                                      ║    INPLACE_TRUE_DIVIDE
>>   34 LOAD_FAST                1 (inner)            ║    STORE_FAST                  x
        LOAD_ATTR                0 (count)            ║    JUMP_ABSOLUTE               to 8
        RETURN_VALUE                                  ║        -> 2(True)
                                                      ║
                                                      ║    FOR_ITER__JumpEpilogue
                                                      ║        -> 6(True)
                                                      ║
                                                      ║    LOAD_FAST                   x
                                                      ║    LOAD_CONST                  1
                                                      ║    INPLACE_FLOOR_DIVIDE
                                                      ║    STORE_FAST                  x
                                                      ║    LOAD_FAST                   x
                                                      ║    POP_JUMP_IF_TRUE
                                                      ║        -> 7(False)
                                                      ║        -> 1(True)
                                                      ║
                                                      ║    LOAD_FAST                   inner
                                                      ║    LOAD_ATTR                   count
                                                      ║    RETURN_VALUE
""",
    r"""
    GET_ITER:                   (inner) -> inner_iter
    FOR_ITER__NoJumpEpilogue:   (inner_iter) -> _
    INPLACE_TRUE_DIVIDE:        (U[x_2, x_1, x], 2) -> x_1
    INPLACE_FLOOR_DIVIDE:       (U[x_2, x_1, x], 1) -> x_2
    LOAD_ATTR:                  (inner) -> inner_count
    RETURN_VALUE:               (inner_count) ->
""",
)
def nested_loop_fn(x, inner):
    while x:
        for _ in inner:
            x /= 2
        x //= 1
    return inner.count


@add_parse_test(
    """
        LOAD_FAST                1 (ctx)              ║    LOAD_FAST                   ctx
        CALL_FUNCTION            0                    ║    CALL_FUNCTION
        SETUP_WITH              13 (to 32)            ║    SETUP_WITH
        STORE_FAST               2 (c)                ║        -> 1(False)
        LOAD_FAST                0 (x)                ║        -> 3(True)
        LOAD_CONST               1 (1)                ║
        INPLACE_ADD                                   ║    SETUP_WITH__NoJumpEpilogue
        STORE_FAST               0 (x)                ║        -> 2(False)
        POP_BLOCK                                     ║
        LOAD_CONST               0 (None)             ║    STORE_FAST                  c
        DUP_TOP                                       ║    LOAD_FAST                   x
        DUP_TOP                                       ║    LOAD_CONST                  1
        CALL_FUNCTION            3                    ║    INPLACE_ADD
        POP_TOP                                       ║    STORE_FAST                  x
        LOAD_CONST               0 (None)             ║    POP_BLOCK
        RETURN_VALUE                                  ║    LOAD_CONST                  None
                                                      ║    DUP_TOP
>>   32 WITH_EXCEPT_START                             ║    DUP_TOP
        POP_JUMP_IF_TRUE        19 (to 38)            ║    CALL_FUNCTION
        RERAISE                  1                    ║    POP_TOP
                                                      ║    LOAD_CONST                  None
>>   38 POP_TOP                                       ║    JUMP_ABSOLUTE
        POP_TOP                                       ║        -> 7(True)
        POP_TOP                                       ║
        POP_EXCEPT                                    ║    SETUP_WITH__JumpEpilogue
        POP_TOP                                       ║        -> 4(True)
        LOAD_CONST               0 (None)             ║
        RETURN_VALUE                                  ║    WITH_EXCEPT_START
                                                      ║    POP_JUMP_IF_TRUE
                                                      ║        -> 5(False)
                                                      ║        -> 6(True)
                                                      ║
                                                      ║    RERAISE
                                                      ║
                                                      ║    POP_TOP
                                                      ║    POP_TOP
                                                      ║    POP_TOP
                                                      ║    POP_EXCEPT
                                                      ║    POP_TOP
                                                      ║    LOAD_CONST                  None
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 7(True)
                                                      ║
                                                      ║    RETURN_VALUE
""",
    flow_spec=DONT_CHECK_FLOW,
)
def context_manager(x, ctx):
    with ctx() as c:
        x += 1


# TODO(robieta): re-enable when we can support generators
# @add_parse_test()
def simple_generator(k, suffix):
    if k < 0:
        yield None

    yield from range(k)

    yield from suffix


class TestClass:
    def f(self, x):
        return self.__name__ + x


add_parse_test(
    """
    LOAD_FAST                0 (self)                     ║    LOAD_FAST                   self
    LOAD_ATTR                0 (__name__)                 ║    LOAD_ATTR                   __name__
    LOAD_FAST                1 (x)                        ║    LOAD_FAST                   x
    BINARY_ADD                                            ║    BINARY_ADD
    RETURN_VALUE                                          ║    RETURN_VALUE
""",
    r"""
    LOAD_ATTR:       (self) -> self_name
    BINARY_ADD:      (self_name, x) -> result
    RETURN_VALUE:    (result) ->
""",
)(TestClass().f)


class TestClassWithSuper(TestClass):
    def f(self, x):
        return super().f(x) + 1


add_parse_test(
    """
    LOAD_GLOBAL              0 (super)                    ║    LOAD_GLOBAL                 super
    CALL_FUNCTION            0                            ║    CALL_FUNCTION
    LOAD_METHOD              1 (f)                        ║    LOAD_METHOD                 f
    LOAD_FAST                1 (x)                        ║    LOAD_FAST                   x
    CALL_METHOD              1                            ║    CALL_METHOD
    LOAD_CONST               1 (1)                        ║    LOAD_CONST                  1
    BINARY_ADD                                            ║    BINARY_ADD
    RETURN_VALUE                                          ║    RETURN_VALUE
""",
    r"""
    CALL_FUNCTION:   (super) -> super_self
    LOAD_METHOD:     (super_self) -> f
    CALL_METHOD:     (f, super_self, x) -> f_result
    BINARY_ADD:      (f_result, 1) -> output
    RETURN_VALUE:    (output) ->
""",
)(TestClassWithSuper().f)


def make_nonlocal_test():
    x: int

    @add_parse_test(
        """
    LOAD_DEREF               0 (x)                        ║    LOAD_DEREF                  x
    LOAD_CONST               1 (1)                        ║    LOAD_CONST                  1
    ROT_TWO                                               ║    ROT_TWO
    STORE_FAST               0 (y)                        ║    STORE_FAST                  y
    STORE_DEREF              0 (x)                        ║    STORE_DEREF                 x
    LOAD_FAST                0 (y)                        ║    LOAD_FAST                   y
    RETURN_VALUE                                          ║    RETURN_VALUE
    """,
        r"""
    RETURN_VALUE:    (x) ->
    """,
    )
    def access_nonlocal():
        nonlocal x
        y, x = x, 1
        return y


# Re-enable when nonlocal variables are supported.
# make_nonlocal_test()


@add_parse_test(
    """
        SETUP_FINALLY           12 (to 26)            ║    SETUP_FINALLY
        LOAD_FAST                0 (f)                ║        -> 1(False)
        LOAD_METHOD              0 (write)            ║        -> 3(True)
        LOAD_CONST               1 ('Test')           ║
        CALL_METHOD              1                    ║    SETUP_FINALLY__NoJumpEpilogue
        POP_TOP                                       ║        -> 2(False)
        POP_BLOCK                                     ║
        LOAD_FAST                0 (f)                ║    LOAD_FAST                   f
        LOAD_METHOD              1 (close)            ║    LOAD_METHOD                 write
        CALL_METHOD              0                    ║    LOAD_CONST                  'Test'
        POP_TOP                                       ║    CALL_METHOD
        LOAD_CONST               0 (None)             ║    POP_TOP
        RETURN_VALUE                                  ║    POP_BLOCK
                                                      ║    LOAD_FAST                   f
>>   26 LOAD_FAST                0 (f)                ║    LOAD_METHOD                 close
        LOAD_METHOD              1 (close)            ║    CALL_METHOD
        CALL_METHOD              0                    ║    POP_TOP
        POP_TOP                                       ║    LOAD_CONST                  None
        RERAISE                  0                    ║    RETURN_VALUE
                                                      ║
                                                      ║    SETUP_FINALLY__JumpEpilogue
                                                      ║        -> 4(True)
                                                      ║
                                                      ║    LOAD_FAST                   f
                                                      ║    LOAD_METHOD                 close
                                                      ║    CALL_METHOD
                                                      ║    POP_TOP
                                                      ║    RERAISE
""",
    r"""
2)  LOAD_METHOD:                    (f) -> f_write
2)  CALL_METHOD:                    (f_write, f, Test) -> write_result
2)  LOAD_METHOD:                    (f) -> f_close
2)  CALL_METHOD:                    (f_close, f) -> close_result
2)  RETURN_VALUE:                   (None) ->
3)  SETUP_FINALLY__JumpEpilogue:    () -> __unused_0, __unused_1, __unused_2, __unused_3, __unused_4, __unused_5
4)  LOAD_METHOD:                    (f) -> f_close_finally_branch
4)  CALL_METHOD:                    (f_close_finally_branch, f) -> close_result_finally_branch
""",
)
def try_finally(f):
    try:
        f.write("Test")
    finally:
        f.close()


@add_parse_test(
    """
        SETUP_FINALLY           35 (to 72)            ║    SETUP_FINALLY
        SETUP_FINALLY            7 (to 18)            ║        -> 1(False)
        LOAD_FAST                0 (f)                ║        -> 5(True)
        LOAD_METHOD              0 (write)            ║
        LOAD_CONST               1 ('Test')           ║    SETUP_FINALLY__NoJumpEpilogue
        CALL_METHOD              1                    ║        -> 2(False)
        POP_TOP                                       ║
        POP_BLOCK                                     ║    SETUP_FINALLY
        JUMP_FORWARD            13 (to 44)            ║        -> 3(False)
                                                      ║        -> 6(True)
>>   18 DUP_TOP                                       ║
        LOAD_GLOBAL              1 (OSError)          ║    SETUP_FINALLY__NoJumpEpilogue
        JUMP_IF_NOT_EXC_MATCH    21 (to 42)           ║        -> 4(False)
        POP_TOP                                       ║
        POP_TOP                                       ║    LOAD_FAST                   f
        POP_TOP                                       ║    LOAD_METHOD                 write
        LOAD_FAST                1 (log)              ║    LOAD_CONST                  'Test'
        LOAD_CONST               2 ('Fail')           ║    CALL_METHOD
        CALL_FUNCTION            1                    ║    POP_TOP
        POP_TOP                                       ║    POP_BLOCK
        POP_EXCEPT                                    ║    JUMP_FORWARD                to 44
        JUMP_FORWARD             8 (to 58)            ║        -> 12(True)
                                                      ║
>>   42 RERAISE                  0                    ║    SETUP_FINALLY__JumpEpilogue
                                                      ║        -> 11(True)
>>   44 POP_BLOCK                                     ║
        LOAD_FAST                0 (f)                ║    SETUP_FINALLY__JumpEpilogue
        LOAD_METHOD              2 (close)            ║        -> 7(True)
        CALL_METHOD              0                    ║
        POP_TOP                                       ║    DUP_TOP
        LOAD_CONST               0 (None)             ║    LOAD_GLOBAL                 OSError
        RETURN_VALUE                                  ║    JUMP_IF_NOT_EXC_MATCH
                                                      ║        -> 8(False)
>>   58 POP_BLOCK                                     ║        -> 9(True)
        LOAD_FAST                0 (f)                ║
        LOAD_METHOD              2 (close)            ║    POP_TOP
        CALL_METHOD              0                    ║    POP_TOP
        POP_TOP                                       ║    POP_TOP
        LOAD_CONST               0 (None)             ║    LOAD_FAST                   log
        RETURN_VALUE                                  ║    LOAD_CONST                  'Fail'
                                                      ║    CALL_FUNCTION
>>   72 LOAD_FAST                0 (f)                ║    POP_TOP
        LOAD_METHOD              2 (close)            ║    POP_EXCEPT
        CALL_METHOD              0                    ║    JUMP_FORWARD
        POP_TOP                                       ║        -> 10(True)
        RERAISE                  0                    ║
                                                      ║    RERAISE
                                                      ║
                                                      ║    POP_BLOCK
                                                      ║    LOAD_FAST                   f
                                                      ║    LOAD_METHOD                 close
                                                      ║    CALL_METHOD
                                                      ║    POP_TOP
                                                      ║    LOAD_CONST                  None
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 13(True)
                                                      ║
                                                      ║    LOAD_FAST                   f
                                                      ║    LOAD_METHOD                 close
                                                      ║    CALL_METHOD
                                                      ║    POP_TOP
                                                      ║    RERAISE
                                                      ║
                                                      ║    POP_BLOCK
                                                      ║    LOAD_FAST                   f
                                                      ║    LOAD_METHOD                 close
                                                      ║    CALL_METHOD
                                                      ║    POP_TOP
                                                      ║    LOAD_CONST                  None
                                                      ║    JUMP_ABSOLUTE
                                                      ║        -> 13(True)
                                                      ║
                                                      ║    RETURN_VALUE
""",
    flow_spec=DONT_CHECK_FLOW,
)
def try_except_finally(f, log):
    try:
        f.write("Test")
    except OSError:
        log("Fail")
    finally:
        f.close()


def assert_parse_matches_spec(proto_graph: protograph.ProtoGraph, expected: PARSE_SPECIFICATION) -> None:
    block_to_index = {protoblock: idx for idx, protoblock in enumerate(proto_graph)}
    assert len(tuple(proto_graph)) == len(block_to_index)
    assert len(block_to_index) == len(expected)
    for protoblock, (expected_instructions, expected_jumps) in zip(proto_graph, expected):
        # It's tedious to include every arg in the spec (particularly since a
        # lot of them are just indicies that distract from visual inspection),
        # so we allow them to be omitted.
        for i, (opname, argrepr) in zip(protoblock.raw_instructions, expected_instructions):
            assert i.opname == opname
            assert i.argrepr == argrepr or not argrepr
        assert tuple((block_to_index[target], is_jump) for target, is_jump in protoblock.jump_targets) == tuple(
            expected_jumps
        )


def suggest_parse_spec(proto_graph: protograph.ProtoGraph):
    block_to_index = {protoblock: idx for idx, protoblock in enumerate(proto_graph)}

    lines = []
    for protoblock in proto_graph:
        lines.extend(f"{i.opname:<27} {i.argrepr}" for i in protoblock.raw_instructions)
        lines.extend(f"    -> {block_to_index[target]}({is_jump})" for target, is_jump in protoblock.jump_targets)
        lines.append("")

    assert not lines[-1]
    return lines[:-1]


def assert_flow_matches_spec(
    observed_flow: FLOW_SPECIFICATION,
    expected_flow: FLOW_SPECIFICATION,
) -> None:
    assert len(observed_flow) == len(expected_flow)

    observed_outputs = tuple(itertools.chain(*(outputs for _, _, _, outputs in observed_flow)))
    expected_outputs = tuple(itertools.chain(*(outputs for _, _, _, outputs in expected_flow)))
    assert len(observed_outputs) == len(expected_outputs)
    assert len(expected_outputs) == len(set(expected_outputs))
    name_map = {observed: expected for observed, expected in zip(observed_outputs, expected_outputs)}

    def to_str(block_idx, opname, inputs, outputs, name_map):
        block_segment = f"{block_idx})  " if block_idx is not None else ""
        inputs = tuple(tuple(name_map.get(i, i) for i in inputs_i) for inputs_i in inputs)
        inputs_block = ", ".join(f"U[{', '.join(sorted(i))}]" if len(i) > 1 else i[0] for i in inputs)
        outputs_block = ", ".join(name_map.get(i, i) for i in outputs)
        return f"{block_segment}{opname}: ({inputs_block}) -> {outputs_block}"

    for (observed_block_idx, *observed), (expected_block_idx, *expected) in zip(observed_flow, expected_flow):
        # Allow block to be omitted.
        observed_block_idx = observed_block_idx if expected_block_idx is not None else None
        assert to_str(observed_block_idx, *observed, name_map) == to_str(expected_block_idx, *expected, {})


def flow_spec_for_fn(fn: Callable) -> Iterator[FLOW_SPECIFICATION_ENTRY]:
    fn = fn.__func__ if inspect.ismethod(fn) else fn
    signature = inspect.signature(fn)

    proto_graph = apply_protograph_passes(frontend.parse_bytecode(fn))

    flat_node_flow = []
    for block_idx, protoblock in enumerate(proto_graph):
        for instruction, n in protoblock.node_flow:
            new_outputs = [o for o in n.outputs if isinstance(o, protograph.IntermediateValue) and o not in n.inputs]
            flat_node_flow.append((block_idx, instruction, n.inputs, new_outputs))

    # Map function arguments to string names.
    num_args = len(inspect.signature(fn).parameters)
    arg_map = {
        v: name
        for (name, scope), v in proto_graph.root.begin_state
        if scope == python_ir_data.VariableScope.LOCAL and name in signature.parameters
    }
    assert all(isinstance(v, frontend.ExternalRef) for v in arg_map)

    # Check that values have a single producer and assign them placeholder names.
    output_map = {}
    for block_idx, instruction, inputs, outputs in flat_node_flow:
        for output in outputs:
            output_map.setdefault(output, f"OUTPUT_{len(output_map)}")

    def value_to_key(v):
        MISSING = object()
        if (out := arg_map.get(v, MISSING)) is not MISSING:
            return (out,)

        elif (out := output_map.get(v, MISSING)) is not MISSING:
            return (out,)

        elif isinstance(v, frontend.AbstractPhiValue):
            constituents = [value_to_key(vi) for vi in v.constituents]
            assert all(len(i) == 1 for i in constituents)
            return tuple(i[0] for i in constituents)

        elif isinstance(v, frontend.ExternalRef):
            if v.key.scope == python_ir_data.VariableScope.CONST:
                return (str(v.key.identifier),)
            elif v.key.scope == python_ir_data.VariableScope.LOCAL and v.key.identifier not in signature.parameters:
                return ("MISSING",)
            return (v.key.identifier,)

        elif isinstance(v, frontend.ValueMissing):
            return ("MISSING",)

        else:
            raise ValueError(f"Unknown value: {v}")

    # Filter to instructions which produced a new value. (Or "RETURN_VALUE")
    for block_idx, instruction, inputs, outputs in flat_node_flow:
        if outputs or instruction.opname == "RETURN_VALUE":
            yield (
                block_idx,
                instruction.opname,
                tuple(value_to_key(i) for i in inputs),
                tuple(output_map[output] for output in outputs),
            )


# =============================================================================
# == String manipulation helpers ==============================================
# =============================================================================
def dis_str(fn: Callable) -> str:
    dis.dis(fn, file=(file := io.StringIO()))
    file.seek(0)
    raw_lines = file.read().splitlines(keepends=False)
    pattern = re.compile(r"(^\s*[0-9]*\s*).")
    index = min(len(match.groups()[0]) for line in raw_lines if (match := pattern.search(line)))

    # Remove line numbers and empty lines.
    lines = [line[index:].rstrip() for line in raw_lines if line.strip()]

    # Remove instruction numbers (except for jump targets) since all instructions
    # are now the same size.
    pattern = re.compile(r"^\s*(>>\s)?\s*([0-9]+)\s(.*)$")
    new_lines = []
    for line in lines:
        match = pattern.search(line)
        assert pattern
        jump_target_prefix, instruction_number, remainder = match.groups()
        if jump_target_prefix is None:
            new_lines.append(f"{' ' * 8}{remainder}")
        else:
            new_lines.extend(("", f">> {instruction_number:>4} {remainder}"))

    return "\n".join(new_lines)


def split_column_blocks(s: str, split_sequence: str):
    segments = tuple(l.split(split_sequence) for l in s.splitlines(keepends=False))
    return tuple(
        "\n".join(l.rstrip() for l in column_lines) for column_lines in itertools.zip_longest(*segments, fillvalue="")
    )


def extract_parse_spec(spec_str: str) -> tuple[str, PARSE_SPECIFICATION]:
    spec_lines = spec_str.splitlines(keepends=False)
    expected = [([], [])]
    instruction_pattern = re.compile(r"^([a-zA-Z_]+)(.*)$")
    jump_pattern = re.compile(r"^\s*-> ([0-9]+)\((True|False)\)\s*$")
    for line in textwrap.dedent("\n".join(spec_lines)).strip().splitlines(keepends=False):
        instructions, jumps = expected[-1]
        if match := instruction_pattern.search(line):
            opname, arg = match.groups()
            instructions.append((opname, arg.strip()))
        elif match := jump_pattern.search(line):
            jump_index, is_jump = match.groups()
            jump_index = int(jump_index)
            is_jump = {"True": True, "False": False}[is_jump]
            if not is_jump:
                assert jump_index == len(expected), "Invalid spec: Fallthrough does not point to the next block"
            jumps.append((jump_index, is_jump))
        else:
            assert not line.strip(), line
            expected.append(([], []))

    return expected


def extract_flow_spec(spec_str: str) -> Iterator[FLOW_SPECIFICATION_ENTRY]:
    line_pattern = re.compile(r"^([0-9]+\))?\s*([a-zA-Z_]+):\s+\((.*)\)\s+->\s+(.*)$")
    for line in textwrap.dedent(spec_str).strip().splitlines(keepends=False):
        if match := line_pattern.search(line.strip()):
            block, opname, inputs, outputs = match.groups()
        elif return_match := re.search(r"RETURN_VALUE:\s+\((.*)\).*$", line):
            block, opname, inputs, outputs = (None, "RETURN_VALUE", return_match.groups()[0], "")
        else:
            raise ValueError(f"Unrecognized line: {line}")

        remaining = inputs
        parsed_inputs = []
        while match := re.search(r"^([^\[^\]+]*)U\[([^\]]+)\](.*)$", remaining):
            prefix, union, remaining = match.groups()
            parsed_inputs.extend((i,) for i in prefix.split(", ") if i)
            parsed_inputs.append(tuple(union.split(", ")))
        parsed_inputs.extend((i,) for i in remaining.split(", ") if i)
        outputs = tuple(i for i in outputs.split(", ") if i)

        yield int(block[:-1]) if block else None, opname, tuple(parsed_inputs), outputs


# =============================================================================
# == Paramerized tests ========================================================
# =============================================================================
@pytest.mark.skipif(
    not python_ir_data.SUPPORTS_PREPROCESSING,
    reason=f"Python version {sys.version_info=} does not support preprocessing",
)
@pytest.mark.parametrize(
    ("fn", "parse_spec", "flow_spec"),
    TEST_CASES,
    ids=[
        f"test_parse_{fn.__self__.__class__.__name__ + '().' if hasattr(fn, '__self__') else ''}{fn.__name__}"
        for fn, _, _ in TEST_CASES
    ],
)
def test_parse(fn, parse_spec: Optional[str], flow_spec: Optional[str]):
    fn_dis = textwrap.dedent(dis_str(fn)).rstrip()
    if parse_spec is None:
        dis_lines = fn_dis.splitlines(keepends=False)
        parse_lines = suggest_parse_spec(frontend.parse_bytecode(fn))

        print(f"\nProposed spec: {fn.__name__}\n{'-' * 80}")
        for dis_line, parse_line in itertools.zip_longest(dis_lines, parse_lines, fillvalue=""):
            print(f"{dis_line:<50}    ║    {parse_line}")

        pytest.skip("No parse spec provided.")

    expected_dis, expected_blocks = split_column_blocks(parse_spec, "    ║")
    expected_dis = textwrap.dedent(expected_dis).rstrip()
    if fn_dis.strip() != expected_dis.strip():
        diff = "\n".join(difflib.unified_diff(fn_dis.splitlines(), expected_dis.splitlines()))
        pytest.skip(
            f"Disassembed input does not match:\n{diff}\nCannot test using this Python version. {sys.version_info}"
        )

    observed_flow = tuple(flow_spec_for_fn(fn)) if flow_spec != DONT_CHECK_FLOW else None
    if flow_spec is None and observed_flow is not None:
        print(f"\nProposed flow: {fn.__name__}\n{'-' * 80}")
        for block_idx, opname, inputs, outputs in observed_flow:
            inputs = ", ".join(i[0] if len(i) == 1 else f"U[{', '.join(i)}]" for i in inputs)
            print(f"{block_idx})  {opname + ':':<16} ({inputs}) -> {', '.join(outputs)}")

        # breakpoint()
        pytest.skip("No flow spec provided.")

    assert_parse_matches_spec(frontend.parse_bytecode(fn), extract_parse_spec(expected_blocks))
    if observed_flow is not None:
        assert_flow_matches_spec(observed_flow, tuple(extract_flow_spec(flow_spec)))


def test_debug_print_protoflows(capfd):
    proto_graph = apply_protograph_passes(frontend.parse_bytecode(tuple_fold))
    _ = capfd.readouterr()
    proto_graph.debug_print_protoflows()
    msg = textwrap.dedent(capfd.readouterr().out).strip()
    expected = textwrap.dedent("""
        Protoblock 0:
                              Inputs, Outputs
                  BUILD_TUPLE, (0, 1) -> (2)
              UNPACK_SEQUENCE, (2) -> (1, 0)
                 RETURN_VALUE, (0) -> ()
    """).strip()
    assert msg == expected, msg


def test_abstract_value():
    x = protograph._AbstractValue()
    assert x == x
    assert x in {x}
    with pytest.raises(NotImplementedError):
        copy.copy(x)

    y = protograph._AbstractValue()
    assert x != y
    assert x not in {y}
    assert x in {x, y}
    assert len({x, y}) == 2

    assert x.substitute({x: y}) == y


def test_value_missing():
    x = protograph.ValueMissing()
    assert x == protograph.ValueMissing()
    assert x in {protograph.ValueMissing()}

    # Sanity check that it doesn't always compare equal
    assert x != protograph._AbstractValue()
    assert x != protograph.AbstractValue()


def test_external_ref():
    key = protograph.VariableKey("self", python_ir_data.VariableScope.LOCAL)
    x = protograph.ExternalRef(key)
    y = protograph.ExternalRef(key)

    assert x == y
    assert x in {y}


def test_abstract_phivalue():
    x = protograph.AbstractValue()
    y = protograph.AbstractValue()
    xy = protograph.AbstractPhiValue((x, y))

    # Deduplicate.
    assert len(xy.constituents) == 2
    assert protograph.AbstractPhiValue((x, x, y)) == xy

    # Flatten.
    assert xy == protograph.AbstractPhiValue((x, protograph.AbstractPhiValue((x, y))))

    # Replace constituents.
    x_prime = protograph.IntermediateValue()
    y_prime = protograph.IntermediateValue()
    xy_prime = xy.substitute({x: x_prime, y: y_prime})
    assert xy_prime == protograph.AbstractPhiValue((x_prime, y_prime))

    # Direct replacement takes precidence.
    z = protograph.IntermediateValue()
    assert xy.substitute({x: x_prime, y: y_prime, xy: z}) is z

    # Direct replacements still need to propagate.
    a = protograph.IntermediateValue()
    b = protograph.IntermediateValue()
    ab = protograph.AbstractPhiValue((a, b))
    assert xy.substitute({xy: xy_prime, x_prime: a, y_prime: b}) == ab
