#!/usr/bin/env python
# coding: utf-8

import logging
from pathlib import Path

import bilby
import h5py
import numpy as np
from bilby.gw.conversion import convert_to_lal_binary_black_hole_parameters
from bilby.gw.source import lal_binary_black_hole

from bbhnet.injection import generate_gw
from bbhnet.logging import configure_logging
from hermes.typeo import typeo


@typeo
def main(
    prior_file: str,
    n_samples: int,
    outdir: Path,
    waveform_duration: float = 8,
    sample_rate: float = 4096,
    deterministic: bool = False,
    force_generation: bool = False,
    verbose: bool = False,
):

    """Simulates a set of raw BBH signals and saves them to an output file.

    Args:
        prior_file: prior file for bilby to sample from
        n_samples: number of signal to inject
        outdir: output directory to which signals will be written
        waveform_duration: length of injected waveforms
        sample_rate: sample rate of the signal in Hz
        deterministic: Bool for whether data should be projected in a
                       detrministic way. Ex: training vs validation sets
        force_generation: if True, generate signals even if path already exists
    Returns:
        path to output file
    """

    # make output dir
    outdir.mkdir(exist_ok=True, parents=True)

    configure_logging(outdir / "generate_waveforms.log", verbose)

    # check if signal file already exists
    signal_file = outdir.joinpath("signals.h5")

    if signal_file.exists() and not force_generation:
        logging.info("Signal file already exists, exiting")
        return signal_file

    # log and print out some simulation parameters
    logging.info("Simulation parameters")
    logging.info("Number of samples     : {}".format(n_samples))
    logging.info("Sample rate [Hz]      : {}".format(sample_rate))
    logging.info("Prior file            : {}".format(prior_file))

    # define a Bilby waveform generator
    waveform_generator = bilby.gw.WaveformGenerator(
        duration=waveform_duration,
        sampling_frequency=sample_rate,
        frequency_domain_source_model=lal_binary_black_hole,
        parameter_conversion=convert_to_lal_binary_black_hole_parameters,
        waveform_arguments={
            "waveform_approximant": "IMRPhenomPv2",
            "reference_frequency": 50,
            "minimum_frequency": 20,
        },
    )

    # sample GW parameters from prior distribution
    priors = bilby.gw.prior.PriorDict(prior_file)
    sample_params = priors.sample(n_samples)

    signals = generate_gw(sample_params, waveform_generator=waveform_generator)

    # Write params and similar to output file

    if np.isnan(signals).any():
        raise ValueError("The signals contain NaN values")

    with h5py.File(signal_file, "w") as f:
        # write signals attributes, snr, and signal parameters
        for k, v in sample_params.items():
            f.create_dataset(k, data=v)

        f.create_dataset("signals", data=signals)

        # write attributes
        f.attrs.update(
            {
                "size": n_samples,
                "sample_rate": sample_rate,
                "waveform_duration": waveform_duration,
            }
        )

    return signal_file


if __name__ == "__main__":
    main()
