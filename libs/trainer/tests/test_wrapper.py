import sys

import pytest
import torch

from bbhnet.data.transforms.transform import Transform
from bbhnet.trainer.wrapper import trainify


@pytest.fixture(scope="session")
def outdir(tmpdir_factory):
    out_dir = tmpdir_factory.mktemp("out")
    return out_dir


@pytest.fixture(params=[True, False])
def validate(request):
    return request.param


@pytest.fixture(params=[True, False])
def preprocess(request):
    return request.param


class dataset:
    def __init__(self, batches):
        self.batches = batches

    def __iter__(self):
        self.i = 0
        return self

    def __next__(self):
        if self.i == self.batches:
            raise StopIteration
        x = torch.randn(8, 2, 512).type(torch.float32)
        y = torch.randint(0, 2, size=(8, 1)).type(torch.float32)
        self.i += 1
        return x, y

    def to(self, device):
        return


class Preprocessor(Transform):
    def __init__(self):
        super().__init__()
        self.factor = self.add_parameter(2)

    def forward(self, x):
        return self.factor * x


@pytest.fixture
def get_data(validate, preprocess):
    def fn(batches: int):
        train_dataset = dataset(batches)
        valid_dataset = dataset(batches) if validate else None
        preprocessor = Preprocessor if preprocess else None
        return train_dataset, valid_dataset, preprocessor

    return fn


@pytest.fixture(params=[True, False])
def unique_args(request):
    return request.param


@pytest.fixture
def data_fn(unique_args, get_data):
    # make sure we can have functions that overlap their args
    if not unique_args:

        def fn(batches: int, max_epochs: int, **kwargs):
            return get_data(batches)

    else:

        def fn(batches: int, **kwargs):
            return get_data(batches)

    return fn


def test_wrapper(data_fn, preprocess, outdir, unique_args):
    fn = trainify(data_fn)

    # make sure we can run the function as-is with regular arguments
    if unique_args:
        train_dataset, valid_dataset = fn(4)
    else:
        train_dataset, valid_dataset = fn(4, 1)

    for i, (X, y) in enumerate(train_dataset):
        continue
    assert i == 3

    # call function passing keyword args
    # for train function
    result = fn(
        4,
        outdir=outdir,
        max_epochs=1,
        arch="resnet",
        layers=[2, 2, 2],
    )
    assert len(result["train_loss"]) == 1

    sys.argv = [
        None,
        "--outdir",
        str(outdir),
        "--batches",
        "4",
        "--max-epochs",
        "1",
        "resnet",
        "--layers",
        "2",
        "2",
    ]

    # since trainify wraps function w/ typeo
    # looks for args from command line
    # i.e. from sys.argv
    result = fn()
    assert len(result["train_loss"]) == 1

    # TODO: check that if preprocess, there's
    # an extra parameter in the model. use a
    # mock in dataset to check that if validate,
    # it gets called twice as many times as
    # expected
