import thunder.core.lang as tlang

import torch
from torch.testing import make_tensor


class SampleInput(object):
    """Represents sample inputs to a function."""

    __slots__ = [
        "args",
        "kwargs",
    ]

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


opinfos = []

# TODO: require use of generic Thunder dtypes (once they exist)
class OpInfo(object):
    """Operator information and helper functions for acquiring it."""

    def __init__(
        self,
        op,
        *,
        name=None,
        device_types,
        dtypes,
        sample_input_generator,
        method_variant=None,
        operator_variant=None,
        torch_reference=None,
        numpy_reference=None,
        test_directives=(),
    ):
        self.op = op
        self.name = name if name is not None else op.__name__
        self._device_types = device_types
        self._dtypes = dtypes
        self.sample_input_generator = sample_input_generator
        self.method_variant = method_variant
        self.operator_variant = operator_variant
        self.torch_reference = torch_reference
        self.numpy_reference = numpy_reference
        self.test_directives = test_directives

    def __call__(self, *args, **kwargs):
        """Calls the function variant of the operator."""
        return self.op(*args, **kwargs)

    # TODO: different sample inputs must be generated depending on the language context
    def sample_inputs(self, device_type, dtype, *, requires_grad=False, **kwargs):
        return self.sample_input_generator(
            self, device_type, dtype, requires_grad, **kwargs
        )

    def device_types(self):
        return set(self._device_types)

    def dtypes(self, device_type=None):
        if device_type is not None:
            raise NotImplementedError

        return set(self._dtypes)


#
# Elementwise Binary OpInfos
#

# TODO: create elementwise binary OpInfo subclass and maybe auto add to list
elementwise_binary_ops = []

# TODO: extend this generator
def elementwise_binary_generator(op, device, dtype, requires_grad, **kwargs):
    a = make_tensor((4, 4), device=device, dtype=dtype)

    yield SampleInput(a, a)

    b = make_tensor((4, 1), device=device, dtype=dtype)
    yield SampleInput(b, a)


# TODO: update dtypes with Thunder dtypes (when they exist)
add_opinfo = OpInfo(
    tlang.add,
    device_types=("cuda",),
    dtypes=(torch.float16, torch.float32, torch.float64),
    sample_input_generator=elementwise_binary_generator,
    torch_reference=torch.add,
)
elementwise_binary_ops.append(add_opinfo)

sub_opinfo = OpInfo(
    tlang.sub,
    device_types=("cuda",),
    dtypes=(torch.float16, torch.float32, torch.float64),
    sample_input_generator=elementwise_binary_generator,
    torch_reference=torch.sub,
)
elementwise_binary_ops.append(sub_opinfo)


# Puts all opinfos into the "opinfos" list
opinfos.extend(elementwise_binary_ops)
