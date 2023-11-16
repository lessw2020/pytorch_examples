import argparse

import torch
import torch.distributed as dist

from torch.distributed._tensor import DeviceMesh
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.tensor.parallel import (
    PairwiseParallel,
    parallelize_module,
)


from torch.distributed._shard.sharded_tensor import ShardedTensor
from torch.distributed._tensor import DTensor, Replicate, sharding_prop
from torch.distributed._tensor.device_mesh import init_device_mesh
import os

from utils import cleanup, torchrun_setup, ToyModel
try:
    from torch.distributed.tensor.parallel import (
        SequenceParallel
    )
    SP_AVAILABLE = True
except BaseException as e:
    pass


"""
This is the script to test 2D Parallel which combines Tensor/Sequence
parallel with Fully Sharded Data Parallel (TP/SP + FSDP) on a toy model
in the SPMD style. We show an E2E working flow from forward, backward
and optimization.

We enabled Fully Sharded Data Parallel + Tensor Parallel in
separate parallel dimensions:
    Data Parallel ("dp") across hosts
    Tensor Parallel ("tp") within each host

 We use a simple diagram to illustrate below:

======================================================================
------------       ------------       ------------       ------------
| Host 1   |       | Host 2   |       |          |       | Host N   |
| 8 GPUs   |       | 8 GPUs   |       |          |       | 8 GPUs   |
|          |       |          |       |    ...   |       |          |
| (TP)     |       | (TP)     |       |          |       | (TP)     |
|[0,1,..,7]|       |[8,9..,15]|       |          |       |[8N-8,8N-7|
|          |       |          |       |          |       | .., 8N-1]|
|          |       |          |       |          |       |          |
------------       ------------       ------------       ------------
FSDP:
[0, 8, ..., 8N-8], [1, 9, ..., 8N-7], ..., [7, 15, ..., 8N-1]
======================================================================

More details can be seen in the slide:
https://docs.google.com/presentation/d/17g6WqrO00rP3MsxbRENsPpjrlSkwiA_QB4r93_eB5is/
"""


def demo_2d(args):
    """
    Main body of the demo of a basic version of tensor parallel by using
    PyTorch native APIs.
    """
    torchrun_setup()


    _rank = int(os.environ["RANK"])
    _local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(_local_rank)
    _world_size = int(os.environ["WORLD_SIZE"])
    _local_world_size = int(os.environ["LOCAL_WORLD_SIZE"])

    def rank_print(msg):
        """helper function to print only on rank 0"""
        if _rank==0:
            print(f"{msg}")

    print(f"Running basic Megatron style TP example on rank {_rank}.")

    assert (
        _world_size % args.tp_size == 0
    ), f"World size {_world_size} needs to be divisible by TP size {args.tp_size}"

    device = f"cuda"

    # create a sharding plan based on the given world_size.

    dp_size = _world_size // args.tp_size

    # Create a device mesh with 2 dimensions.
    # First dim is the data parallel dimension
    # and second dim is the tensor parallel dimension.
    device_mesh = init_device_mesh(device, (dp_size, args.tp_size), mesh_dim_names=("dp","tp"))
    assert device_mesh is not None, "unable to create valid device mesh"

    rank_print(f"Device Mesh created: {device_mesh=}")
    tp_mesh = device_mesh["tp"]
    dp_mesh = device_mesh["dp"]

    # To support identical inputs for TP groups, we need the dp process group
    dp_pg = device_mesh.get_dim_groups()[0]

    # For TP, input needs to be same across all TP ranks.
    # while for SP, input can be different across all ranks.
    # We will use dp_rank for setting the random seed
    # to mimic the behavior of the dataloader.
    dp_rank = _rank if args.run_seq_parallel else dist.get_rank(dp_pg)


    # create model and move it to GPU with id rank
    model = ToyModel().cuda(_rank)
    # Create an optimizer for the parallelized module.
    lr = 3e-3
    rank_print(f"Creating AdamW optimizer with learning rate {lr}")
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)

    # Parallelize the module based on the given Parallel Style.
    parallel_style = SequenceParallel() if args.run_seq_parallel else PairwiseParallel()
    model = parallelize_module(model, tp_mesh, parallel_style)

    # Init FSDP using the dp device mesh
    model = FSDP(model, device_mesh = dp_mesh)


    # Training loop:
    # Perform a num of iterations of forward/backward
    # and optimizations for the sharded module.
    for i in range(args.iter_nums):
        # seeding to ensure idential inputs for TP pairs (when running TP)
        torch.manual_seed(i + dp_rank)
        inp = torch.rand(20, 10).cuda(_rank)

        output = model(inp)
        output.sum().backward()
        optimizer.step()
        rank_print(f"2D iter {i} complete")

    rank_print(f"2D training successfully completed!")
    cleanup()


if __name__ == "__main__":
    n_gpus = torch.cuda.device_count()
    parser = argparse.ArgumentParser()
    # This is passed in via cmd
    parser.add_argument("--world_size", type=int, default=n_gpus)
    parser.add_argument("--iter_nums", type=int, default=10)
    parser.add_argument("--run_seq_parallel", type=bool, default=False)
    parser.add_argument("--tp_size", type=int, default=2)
    args = parser.parse_args()
    # The main entry point is called directly without using subprocess
    if n_gpus < 4:
        print("Requires at least 4 GPUs to run.")
    elif not SP_AVAILABLE:
        print(
            "PyTorch doesn't have Sequence Parallelism available,"
            " need nightly build."
        )
    #else:
    #mp.spawn(demo_2d, args=(args,), nprocs=args.world_size, join=True)
    demo_2d(args)
