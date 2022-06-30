import logging
import os
import time
from typing import Callable, Iterable, Optional, Tuple

import numpy as np
import torch


def train_for_one_epoch(
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    criterion: torch.nn.Module,
    train_dataset: Iterable[Tuple[np.ndarray, np.ndarray]],
    valid_dataset: Iterable[Tuple[np.ndarray, np.ndarray]] = None,
    profiler: Optional[torch.profiler.profile] = None,
    scaler: Optional[torch.cuda.amp.GradScaler] = None,
):
    """Run a single epoch of training"""

    train_loss = 0
    samples_seen = 0
    start_time = time.time()
    model.train()

    for samples, targets in train_dataset:
        optimizer.zero_grad(set_to_none=True)  # reset gradient

        # do forward step in mixed precision
        # hard code false for now
        with torch.autocast("cuda", enabled=scaler is not None):
            predictions = torch.flatten(model(samples))
            targets = torch.flatten(targets)
            loss = criterion(predictions, targets)

        train_loss += loss.item()
        samples_seen += len(samples)

        if scaler is not None:
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
        else:
            loss.backward()
            optimizer.step()

        if profiler is not None:
            profiler.step()

    if profiler is not None:
        profiler.stop()

    end_time = time.time()
    duration = end_time - start_time
    throughput = samples_seen / duration
    train_loss /= samples_seen

    logging.info(
        "Duration {:0.2f}s, Throughput {:0.1f} samples/s".format(
            duration, throughput
        )
    )
    msg = f"Train Loss: {train_loss:.4e}"

    # Evaluate performance on validation set if given
    if valid_dataset is not None:
        valid_loss = 0
        samples_seen = 0

        model.eval()

        # reason mixed precision is not used here?
        # since no gradient calculation that requires
        # higher precision?
        with torch.no_grad():
            for samples, targets in valid_dataset:

                predictions = torch.flatten(model(samples))
                targets = torch.flatten(targets)
                loss = criterion(predictions, targets)

                valid_loss += loss.item()
                samples_seen += len(samples)

        valid_loss /= samples_seen
        msg += f", Valid Loss: {valid_loss:.4e}"
    else:
        valid_loss = None

    logging.info(msg)
    return train_loss, valid_loss, duration, throughput


def train(
    architecture: Callable,
    outdir: str,
    # data params
    train_dataset: Iterable[Tuple[np.ndarray, np.ndarray]],
    valid_dataset: Iterable[Tuple[np.ndarray, np.ndarray]] = None,
    preprocessor: Optional[torch.nn.Module] = None,
    # optimization params
    max_epochs: int = 40,
    init_weights: Optional[str] = None,
    lr: float = 1e-3,
    weight_decay: float = 0.0,
    patience: Optional[int] = None,
    factor: float = 0.1,
    early_stop: int = 20,
    # misc params
    device: Optional[str] = None,
    use_amp: bool = False,
    profile: bool = False,
) -> float:
    """Train BBHnet model on in-memory data
    Args:
        architecture:
            A callable which takes as its only input the number
            of ifos, and returns an initialized torch
            Module
        outdir:
            Location to save training artifacts like optimized
            weights, preprocessing objects, and visualizations
        train_dataset:
            An Iterable of (X, y) pairs where X is a batch of training
            data and y is the corresponding targets
        valid_dataset:
            An Iterable of (X, y) pairs where X is a batch of training
            data and y is the corresponding targets
        max_epochs:
            Maximum number of epochs over which to train.
        init_weights:
            Path to weights with which to initialize network. If
            left as `None`, network will be randomly initialized.
            If `init_weights` is a directory, it will be assumed
            that this directory contains a file called `weights.pt`.
        lr:
            Learning rate to use during training.
        weight_decay:
            Amount of regularization to apply during training.
        patience:
            Number of epochs without improvement in validation
            loss before learning rate is reduced. If left as
            `None`, learning rate won't be scheduled. Ignored
            if `valid_data is None`
        factor:
            Factor by which to reduce the learning rate after
            `patience` epochs without improvement in validation
            loss. Ignored if `valid_data is None` or
            `patience is None`.
        early_stop:
            Number of epochs without improvement in validation
            loss before training terminates altogether. Ignored
            if `valid_data is None`.
        device:
            Indicating which device (i.e. cpu or gpu) to run on. Use
            `"cuda"` to use the default GPU available, or `"cuda:{i}`"`,
            where `i` is a valid GPU index on your machine, to specify
            a specific GPU (alternatively, consider setting the environment
            variable `CUDA_VISIBLE_DEVICES=${i}` and using just `"cuda"`
            here).
        profile:
            Whether to generate a tensorboard profile of the
            training step on the first epoch. This will make
            this first epoch slower.
    """

    device = device or "cpu"
    os.makedirs(outdir, exist_ok=True)

    # Creating model, loss function, optimizer and lr scheduler
    logging.info("Building and initializing model")

    # hard coded since we haven't generalized to multiple ifos
    # pull request to generalize dataloader is a WIP
    num_ifos = 2

    model = architecture(num_ifos)
    model.to(device)

    # now move the train dataset and the validation dataset
    # (if the latter exists) to the target device
    train_dataset.to(device)
    if valid_dataset is not None:
        valid_dataset.to(device)

    # if we passed a module for preprocessing,
    # include it in the model so that the weights
    # get exported along with everything else
    if preprocessor is not None:
        preprocessor.to(device)
        model = torch.nn.Sequential(preprocessor, model)

    if init_weights is not None:
        # allow us to easily point to the best weights
        # from another run of this same function
        if os.path.isdir(init_weights):
            init_weights = os.path.join(init_weights, "weights.pt")

        logging.debug(
            f"Initializing model weights from checkpoint '{init_weights}'"
        )
        model.load_state_dict(torch.load(init_weights))

    logging.info(model)
    logging.info("Initializing loss and optimizer")

    # TODO: Allow different loss functions or
    # optimizers to be passed?

    criterion = torch.nn.functional.binary_cross_entropy_with_logits
    optimizer = torch.optim.Adam(
        model.parameters(), lr=lr, weight_decay=weight_decay
    )

    if patience is not None:
        lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer,
            patience=patience,
            factor=factor,
            threshold=0.0001,
            min_lr=lr * factor**2,
            verbose=True,
        )

    # start training
    torch.backends.cudnn.benchmark = True

    # start training
    scaler = None
    if use_amp and device.startswith("cuda"):
        scaler = torch.cuda.amp.GradScaler()
    elif use_amp:
        logging.warning("'use_amp' flag set but no cuda device, ignoring")

    best_valid_loss = np.inf
    since_last_improvement = 0
    history = {"train_loss": [], "valid_loss": []}

    logging.info("Beginning training loop")
    for epoch in range(max_epochs):
        if epoch == 0 and profile:
            profiler = torch.profiler.profile(
                schedule=torch.profiler.schedule(wait=0, warmup=1, active=10),
                on_trace_ready=torch.profiler.tensorboard_trace_handler(
                    os.path.join(outdir, "profile")
                ),
            )
            profiler.start()
        else:
            profiler = None

        logging.info(f"=== Epoch {epoch + 1}/{max_epochs} ===")
        train_loss, valid_loss, duration, throughput = train_for_one_epoch(
            model,
            optimizer,
            criterion,
            train_dataset,
            valid_dataset,
            profiler,
            scaler,
        )

        history["train_loss"].append(train_loss)

        # do some house cleaning with our
        # validation loss if we have one
        if valid_loss is not None:
            history["valid_loss"].append(valid_loss)

            # update our learning rate scheduler if we
            # indicated a schedule with `patience`
            if patience is not None:
                lr_scheduler.step(valid_loss)

            # save this version of the model weights if
            # we achieved a new best loss, otherwise check
            # to see if we need to early stop based on
            # plateauing validation loss
            if valid_loss < best_valid_loss:
                logging.debug(
                    "Achieved new lowest validation loss, "
                    "saving model weights"
                )
                best_valid_loss = valid_loss

                weights_path = os.path.join(outdir, "weights.pt")
                torch.save(model.state_dict(), weights_path)
                since_last_improvement = 0
            else:
                since_last_improvement += 1
                if since_last_improvement >= early_stop:
                    logging.info(
                        "No improvement in validation loss in {} "
                        "epochs, halting training early".format(early_stop)
                    )
                    break

    # return the training results
    return history
