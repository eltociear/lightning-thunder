import dis
import sys
import types

from .graph import Node

# this is Python 3.10 specific for the time being.

#  *  0 -- when not jump
#  *  1 -- when jump
#  * -1 -- maximal

# input, output probably would be smart to highlight inplace mods and global side effects
# (e.g. setup_annotations, import_star), too
fixed_stack_effects_detail = {
    "NOP": (0, 0),
    "EXTENDED_ARG": (0, 0),
    # Stack manipulation
    "POP_TOP": (1, 0),
    "ROT_TWO": (2, 2),
    "ROT_THREE": (3, 3),
    "ROT_FOUR": (4, 4),
    "DUP_TOP": (1, 2),
    "DUP_TOP_TWO": (2, 4),
    # Unary operators
    "UNARY_POSITIVE": (1, 1),
    "UNARY_NOT": (1, 1),
    "UNARY_INVERT": (1, 1),
    "SET_ADD": (2, 1),  # these leave the container on the stack
    "LIST_APPEND": (2, 1),
    "MAP_ADD": (3, 1),
    # Binary operators
    "BINARY_POWER": (2, 1),
    "BINARY_MULTIPLY": (2, 1),
    "BINARY_MATRIX_MULTIPLY": (2, 1),
    "BINARY_MODULO": (2, 1),
    "BINARY_ADD": (2, 1),
    "BINARY_SUBTRACT": (2, 1),
    "BINARY_SUBSCR": (2, 1),
    "BINARY_FLOOR_DIVIDE": (2, 1),
    "BINARY_TRUE_DIVIDE": (2, 1),
    "INPLACE_FLOOR_DIVIDE": (2, 1),
    "INPLACE_TRUE_DIVIDE": (2, 1),
    "INPLACE_ADD": (2, 1),
    "INPLACE_SUBTRACT": (2, 1),
    "INPLACE_MULTIPLY": (2, 1),
    "INPLACE_MATRIX_MULTIPLY": (2, 1),
    "INPLACE_MODULO": (2, 1),
    "BINARY_LSHIFT": (2, 1),
    "BINARY_RSHIFT": (2, 1),
    "BINARY_AND": (2, 1),
    "BINARY_XOR": (2, 1),
    "BINARY_OR": (2, 1),
    "INPLACE_POWER": (2, 1),
    "INPLACE_LSHIFT": (2, 1),
    "INPLACE_RSHIFT": (2, 1),
    "INPLACE_AND": (2, 1),
    "INPLACE_XOR": (2, 1),
    "INPLACE_OR": (2, 1),
    "STORE_SUBSCR": (3, 0),
    "DELETE_SUBSCR": (2, 0),
    "GET_ITER": (1, 1),
    "PRINT_EXPR": (1, 0),
    "LOAD_BUILD_CLASS": (0, 1),
    "RETURN_VALUE": (1, 0),
    "IMPORT_STAR": (1, 0),
    "SETUP_ANNOTATIONS": (0, 0),
    "YIELD_VALUE": (1, 1),  # I think
    "YIELD_FROM": (2, 1),  # I am very unsure
    "POP_BLOCK": (0, 0),
    "POP_EXCEPT": (3, 0),
    "STORE_NAME": (1, 0),
    "DELETE_NAME": (0, 0),
    "STORE_ATTR": (2, 0),
    "DELETE_ATTR": (1, 0),
    "STORE_GLOBAL": (1, 0),
    "DELETE_GLOBAL": (0, 0),
    "LOAD_CONST": (0, 1),
    "LOAD_NAME": (0, 1),
    "LOAD_ATTR": (1, 1),
    "COMPARE_OP": (2, 1),
    "IS_OP": (2, 1),
    "CONTAINS_OP": (2, 1),
    "JUMP_IF_NOT_EXC_MATCH": (2, 0),
    "IMPORT_NAME": (2, 1),
    "IMPORT_FROM": (1, 2),
    # Jumps
    "JUMP_FORWARD": (0, 0),
    "JUMP_ABSOLUTE": (0, 0),
    "POP_JUMP_IF_FALSE": (1, 0),
    "POP_JUMP_IF_TRUE": (1, 0),
    "LOAD_GLOBAL": (0, 1),
    "RERAISE": (3, 0),
    "WITH_EXCEPT_START": (7, 8),  # ??!?
    "LOAD_FAST": (0, 1),
    "STORE_FAST": (1, 0),
    "DELETE_FAST": (0, 0),
    # Closures
    "LOAD_CLOSURE": (0, 1),
    "LOAD_DEREF": (0, 1),
    "LOAD_CLASSDEREF": (0, 1),
    "STORE_DEREF": (1, 0),
    "DELETE_DEREF": (0, 0),
    # Iterators and generators
    "GET_AWAITABLE": (1, 1),
    "BEFORE_ASYNC_WITH": (1, 2),
    "GET_AITER": (1, 1),
    "GET_ANEXT": (1, 2),
    "GET_YIELD_FROM_ITER": (1, 1),
    "END_ASYNC_FOR": (7, 0),
    "LOAD_METHOD": (1, 2),
    "LOAD_ASSERTION_ERROR": (0, 1),
    "LIST_TO_TUPLE": (1, 1),
    "GEN_START": (1, 0),
    "LIST_EXTEND": (2, 1),
    "SET_UPDATE": (2, 1),
    "DICT_MERGE": (2, 1),
    "DICT_UPDATE": (2, 1),
    "COPY_DICT_WITHOUT_KEYS": (2, 2),
    "MATCH_CLASS": (3, 2),
    "GET_LEN": (1, 2),
    "MATCH_MAPPING": (1, 2),
    "MATCH_SEQUENCE": (1, 2),
    "MATCH_KEYS": (2, 4),
}


def stack_effect_detail(opname: str, oparg: int, *, jump: bool = False):
    if opname in fixed_stack_effects_detail:
        return fixed_stack_effects_detail[opname]
    elif opname == "ROT_N":
        return (oparg, oparg)
    elif opname in {"BUILD_TUPLE", "BUILD_LIST", "BUILD_SET", "BUILD_STRING"}:
        return (oparg, 1)
    elif opname == "BUILD_MAP":
        return (2 * oparg, 1)
    elif opname == "BUILD_CONST_KEY_MAP":
        return (oparg + 1, 1)
    elif opname in {"JUMP_IF_TRUE_OR_POP", "JUMP_IF_FALSE_OR_POP"}:
        return (1, 1) if jump else (1, 0)
    elif opname == "SETUP_FINALLY":
        return (0, 6) if jump else (0, 0)
    # Exception handling
    elif opname == "RAISE_VARARGS":
        return (oparg, 0)
    # Functions and calls
    elif opname == "CALL_FUNCTION":
        return (oparg + 1, 1)
    elif opname == "CALL_METHOD":
        return (oparg + 2, 1)
    elif opname == "CALL_FUNCTION_KW":
        return (oparg + 2, 1)
    elif opname == "CALL_FUNCTION_EX":
        return (2 + ((oparg & 0x01) != 0), 1)
    elif opname == "MAKE_FUNCTION":
        return (
            2 + ((oparg & 0x01) != 0) + ((oparg & 0x02) != 0) + ((oparg & 0x04) != 0) + ((oparg & 0x08) != 0),
            1,
        )
    elif opname == "BUILD_SLICE":
        return (oparg, 1)
    elif opname == "SETUP_ASYNC_WITH":
        return (1, 6) if jump else (0, 0)  # ??
    elif opname == "FORMAT_VALUE":
        return (2, 1) if ((oparg & 0x04) != 0) else (1, 1)
    elif opname == "UNPACK_SEQUENCE":
        return (1, oparg)
    elif opname == "UNPACK_EX":
        return (1, (oparg & 0xFF) + (oparg >> 8) + 1)
    elif opname == "FOR_ITER":
        return (1, 0) if jump else (0, 1)
    else:
        raise ValueError(f"Invalid opname {opname}")


def get_instruction(opname, arg):
    i = dis.Instruction(
        opname=opname,
        opcode=dis.opmap[opname],
        arg=arg,
        argval=None,
        argrepr=None,
        offset=None,
        starts_line=None,
        is_jump_target=None,
    )
    return i


def insert_before(new_n, n):
    idx = n.block.nodes.index(n)
    n.block.nodes.insert(idx, new_n)
    new_n.block = n.block


def insert_after(new_n, n):
    idx = n.block.nodes.index(n)
    n.block.nodes.insert(idx + 1, new_n)
    new_n.block = n.block


def undo_ssa(gr):
    def get_value(v, n, inpidx=None):
        if n.i.opname == "CALL_METHOD" and inpidx == 1:
            return
        if v.is_const:
            idx = len(consts)
            consts.append(v.value)
            new_n = Node(i=get_instruction(opname="LOAD_CONST", arg=idx), outputs=[v], inputs=[])
            insert_before(new_n, n)
        elif v.parent is not None:
            get_value(v.parent, n)
            if n.i.opname == "CALL_METHOD" and inpidx == 0:
                print("###inputs", n.inputs, v, v in n.inputs)
                try:
                    idx = names.index(v.name)
                except ValueError:
                    idx = len(names)
                    names.append(v.name)
                new_n = Node(
                    i=get_instruction(opname="LOAD_METHOD", arg=idx),
                    outputs=[v, v.parent],
                    inputs=[v.parent],
                )
                insert_before(new_n, n)
            elif n.i.opname == "LOAD_ATTR":
                print("###load attr", n.outputs, n.i.argval)
                pass
            else:
                try:
                    idx = names.index(v.name)
                except ValueError:
                    idx = len(names)
                    names.append(v.name)
                new_n = Node(
                    i=get_instruction(opname="LOAD_ATTR", arg=idx),
                    outputs=[v],
                    inputs=[v.parent],
                )
                insert_before(new_n, n)
        elif v.is_global:  # make binding the globals optional?
            if v.value not in consts:
                consts.append(v.value)
            idx = consts.index(v.value)
            new_n = Node(i=get_instruction(opname="LOAD_CONST", arg=idx), outputs=[v], inputs=[])
            insert_before(new_n, n)
        else:
            idx = local_vars.index(v)
            # assert idx >= 0
            new_n = Node(i=get_instruction(opname="LOAD_FAST", arg=idx), outputs=[v], inputs=[])
            insert_before(new_n, n)

    for bl in gr.blocks:
        for n in bl.nodes:
            n.block = bl

    local_vars = [v for v in gr.local_variables_at_start if v is not None]
    consts = []
    names = []

    # inputs in phi values
    for idx, i in enumerate(local_vars):
        last_n = None
        for v in i.phi_values:
            try:
                idx2 = local_vars.index(v)
            except ValueError:
                idx2 = len(local_vars)
                local_vars.append(v)
            new_n = Node(i=get_instruction(opname="LOAD_FAST", arg=idx), outputs=[i], inputs=[])
            if last_n is None:
                insert_before(new_n, gr.blocks[0].nodes[0])
            else:
                insert_after(new_n, last_n)
            last_n = new_n
            new_n = Node(i=get_instruction(opname="STORE_FAST", arg=idx2), outputs=[i], inputs=[])
            insert_after(new_n, last_n)
            last_n = new_n

    names = []

    for bl in gr.blocks:
        for n in bl.nodes[:]:
            print(n)
            for inpidx, i in enumerate(n.inputs):
                get_value(i, n=n, inpidx=inpidx)
            for o in n.outputs[::-1]:
                # what about values that are UnionValue?
                idx = len(local_vars)
                local_vars.append(o)
                new_n = Node(
                    i=get_instruction(opname="STORE_FAST", arg=idx),
                    outputs=[],
                    inputs=[o],
                )
                insert_after(new_n, n)
                last_n = new_n
                for v in o.phi_values:
                    try:
                        idx2 = local_vars.index(v)
                    except ValueError:
                        idx2 = len(local_vars)
                        local_vars.append(v)
                    new_n = Node(
                        i=get_instruction(opname="LOAD_FAST", arg=idx),
                        outputs=[o],
                        inputs=[],
                    )
                    insert_after(new_n, last_n)
                    last_n = new_n
                    new_n = Node(
                        i=get_instruction(opname="STORE_FAST", arg=idx2),
                        outputs=[],
                        inputs=[o],
                    )
                    insert_after(new_n, last_n)
                    last_n = new_n
    return local_vars, names, consts


# this function is taken from PyTorch Dynamo (c) 2022 by Facebook/Meta licensed
# as per https://github.com/pytorch/pytorch/blob/master/LICENSE
def linetable_writer(first_lineno):
    """Used to create typing.CodeType.co_linetable See
    https://github.com/python/cpython/blob/main/Objects/lnotab_notes.txt This is the internal format of the line number
    table if Python >= 3.10."""
    assert sys.version_info >= (3, 10)
    linetable = []
    lineno = first_lineno
    lineno_delta = 0
    byteno = 0

    def _update(byteno_delta, lineno_delta):
        while byteno_delta != 0 or lineno_delta != 0:
            byte_offset = max(0, min(byteno_delta, 254))
            line_offset = max(-127, min(lineno_delta, 127))
            assert byte_offset != 0 or line_offset != 0
            byteno_delta -= byte_offset
            lineno_delta -= line_offset
            linetable.extend((byte_offset, line_offset & 0xFF))

    def update(lineno_new, byteno_new):
        nonlocal lineno, lineno_delta, byteno
        byteno_delta = byteno_new - byteno
        byteno = byteno_new
        _update(byteno_delta, lineno_delta)
        lineno_delta = lineno_new - lineno
        lineno = lineno_new

    def end(total_bytes):
        _update(total_bytes - byteno, lineno_delta)

    return linetable, update, end


def generate_function(gr):
    local_vars, names, consts = undo_ssa(gr)

    bc = []
    linetable, linetable_update, linetable_end = linetable_writer(0)
    address_map = {}
    ctr = 0
    for bl in gr.blocks:
        # assumes first block is function start
        for n in bl.nodes:
            address_map[n] = ctr
            ctr += 2 if len(n.jump_targets) == 2 else 1

    for bl in gr.blocks:
        for n in bl.nodes:
            if n.line_no is not None:
                linetable_update(n.line_no, address_map[n])
            if n.i.opcode in dis.hasjabs:
                # print(len(n.jump_targets), address_map[n], [address_map[t[1].nodes[0]] for t in n.jump_targets])
                # print(n.jump_targets)
                arg = address_map[n.jump_targets[-1][1].nodes[0]]
            elif n.i.opcode in dis.hasjrel:
                arg = address_map[n] - address_map[n]
            else:
                arg = n.i.arg
                if arg is None:
                    arg = 0
            assert arg < 256  # or signed?
            bc.append(n.i.opcode)
            bc.append(arg)
            if len(n.jump_targets) > 1:
                assert len(n.jump_targets) == 2
                i = get_instruction(opname="JUMP_ABSOLUTE", arg=address_map[n.jump_targets[0][1].nodes[0]])
                bc.append(i.opcode)
                bc.append(i.arg)

    linetable_end(len(bc))
    linetable = bytes(linetable)
    bc_bytes = bytes(bc)

    lv_at_start = [v for v in gr.local_variables_at_start if v is not None]
    co_argcount = len(lv_at_start)
    co_posonlyargcount = 0
    co_kwonlyargcount = 0
    co_nlocals = len(local_vars)
    co_stacksize = 10  # TODO
    co_flags = 0
    co_codestring = bc_bytes
    co_consts = tuple(consts)
    co_names = tuple(names)
    co_varnames = tuple(v.name for v in lv_at_start) + tuple(
        f"_tmp_{i}" for i in range(len(local_vars) - len(lv_at_start))
    )
    co_filename = "__none__"
    co_name = "__none__"
    co_firstlineno = 0
    co_linetable = linetable  # XXX
    co_freevars = ()
    co_cellvars = ()

    c = types.CodeType(
        co_argcount,  # int
        co_posonlyargcount,  # int
        co_kwonlyargcount,  # int
        co_nlocals,  # int
        co_stacksize,  # int
        co_flags,  # int
        co_codestring,  # bytes
        co_consts,  # tuple
        co_names,  # tuple
        co_varnames,  # tuple
        co_filename,  # string
        co_name,  # string
        co_firstlineno,  # integer
        co_linetable,  # bytes
        co_freevars,  # tuple
        co_cellvars,  # tuple
    )

    return types.FunctionType(c, {})
