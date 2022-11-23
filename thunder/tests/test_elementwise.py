from torch.testing import assert_close

import thunder

# TODO: sample across executor_types and devices
from thunder.tests import executor_type, supported_device_types

from .framework import ops, run_snippet
from .opinfos import elementwise_binary_ops, elementwise_unary_ops

# Tests for elementwise binary operators


# Snippets run a single test using a single sample
# TODO: should snippets be able to access the original opinfo? -- No
def snippet_torch_consistency(op, torch_op, sample):
    def foo(*args, **kwargs):
        return op(*args, **kwargs)

    traced_foo = thunder.make_traced(foo, executor=executor_type)
    thunder_result = traced_foo(*sample.args, **sample.kwargs)

    torch_result = torch_op(*sample.args, **sample.kwargs)

    assert_close(thunder_result, torch_result)


# TODO: consider structuring tests like this to be autogenerated
#   using a snippet and an "extractor" that constructs the args and kwargs for the snippet
@ops(
    elementwise_unary_ops + elementwise_binary_ops,
    supported_device_types=supported_device_types,
)
def test_torch_consistency(op, device, dtype):
    for sample in op.sample_inputs(device, dtype):
        result = run_snippet(
            snippet_torch_consistency,
            op,
            device,
            dtype,
            op.op,
            op.torch_reference,
            sample,
        )
        if result is not None:
            return result


# TODO: test that the operator variant works properly
