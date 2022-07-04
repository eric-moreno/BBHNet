from collections import defaultdict
from pathlib import Path
from typing import List

import bilby
import h5py
import numpy as np
from gwpy.timeseries import TimeSeries

from bbhnet.injection.utils import (
    apply_high_pass_filter,
    calc_snr,
    get_waveform_generator,
)
from bbhnet.io import h5
from bbhnet.io.timeslides import TimeSlide


def generate_gw(
    sample_params, waveform_generator=None, **waveform_generator_params
):
    """Generate raw gravitational-wave signals, pre-interferometer projection.

    Args:
        sample_params: dictionary of GW parameters
        waveform_generator: bilby.gw.WaveformGenerator with appropriate params
        waveform_generator_params: keyword arguments to
        :meth:`bilby.gw.WaveformGenerator`

    Returns:
        An (n_samples, 2, waveform_size) array, containing both polarizations
        for each of the desired number of samples. The first polarization is
        always plus and the second is always cross
    """

    sample_params = [
        dict(zip(sample_params, col)) for col in zip(*sample_params.values())
    ]
    n_samples = len(sample_params)

    waveform_generator = waveform_generator or get_waveform_generator(
        **waveform_generator_params
    )

    sample_rate = waveform_generator.sampling_frequency
    waveform_duration = waveform_generator.duration
    waveform_size = int(sample_rate * waveform_duration)

    num_pols = 2
    signals = np.zeros((n_samples, num_pols, waveform_size))

    filtered_signal = apply_high_pass_filter(
        signals, sample_params, waveform_generator
    )
    return filtered_signal


def project_raw_gw(
    raw_waveforms,
    sample_params,
    waveform_generator,
    ifo,
    get_snr=False,
    noise_psd=None,
):
    """Project a raw gravitational wave onto an interferometer

    Args:
        raw_waveforms: the plus and cross polarizations of a list of GWs
        sample_params: dictionary of GW parameters
        waveform_generator: the waveform generator that made the raw GWs
        ifo: interferometer
        get_snr: return the SNR of each sample
        noise_psd: background noise PSD used to calculate SNR the sample

    Returns:
        An (n_samples, waveform_size) array containing the GW signals as they
        would appear in the given interferometer with the given set of sample
        parameters. If get_snr=True, also returns a list of the SNR associated
        with each signal
    """

    polarizations = {
        "plus": raw_waveforms[:, 0, :],
        "cross": raw_waveforms[:, 1, :],
    }

    sample_params = [
        dict(zip(sample_params, col)) for col in zip(*sample_params.values())
    ]
    n_sample = len(sample_params)

    sample_rate = waveform_generator.sampling_frequency
    waveform_duration = waveform_generator.duration
    waveform_size = int(sample_rate * waveform_duration)

    signals = np.zeros((n_sample, waveform_size))
    snr = np.zeros(n_sample)

    ifo = bilby.gw.detector.get_empty_interferometer(ifo)

    for i, p in enumerate(sample_params):

        # For less ugly function calls later on
        ra = p["ra"]
        dec = p["dec"]
        geocent_time = p["geocent_time"]
        psi = p["psi"]

        # Generate signal in IFO
        signal = np.zeros(waveform_size)
        for mode, polarization in polarizations.items():
            # Get ifo response
            response = ifo.antenna_response(ra, dec, geocent_time, psi, mode)
            signal += response * polarization[i]

        # Total shift = shift to trigger time + geometric shift
        dt = waveform_duration / 2.0
        dt += ifo.time_delay_from_geocenter(ra, dec, geocent_time)
        signal = np.roll(signal, int(np.round(dt * sample_rate)))

        # Calculate SNR
        if noise_psd is not None:
            if get_snr:
                snr[i] = calc_snr(signal, noise_psd, sample_rate)

        signals[i] = signal
    if get_snr:
        return signals, snr
    return signals


def inject_signals_into_timeslide(
    raw_timeslide: TimeSlide,
    out_timeslide: TimeSlide,
    ifos: List[str],
    prior_file: Path,
    spacing: float,
    sample_rate: float,
    file_length: int,
    fmin: float,
    waveform_duration: float = 8,
    reference_frequency: float = 20,
    waveform_approximant: float = "IMRPhenomPv2",
    buffer: float = 0,
    fftlength: float = 2,
):

    """Injects simulated BBH signals into h5 files TimeSlide object that represents
    timeshifted background data. Currently only supports h5 file format.

    Args:
        raw_timeslide: TimeSlide object of raw background data Segments
        out_timeslide: TimeSlide object to store injection Segments
        ifos: list of interferometers corresponding to timeseries
        prior_file: prior file for bilby to sample from
        spacing: seconds between each injection
        sample_rate: sampling rate
        file_length: length in seconds of each h5 file
        fmin: Minimum frequency for highpass filter
        waveform_duration: length of injected waveforms
        reference_frequency: reference frequency for generating waveforms
        waveform_approximant: waveform type to inject
        buffer: buffer between beginning and end of segments and waveform
        fftlength: fftlength to use for calculating psd

    Returns:
        Paths to the injected files and the parameter file
    """

    # define a Bilby waveform generator

    # TODO: should sampling rate be automatically inferred
    # from raw data?
    waveform_generator = get_waveform_generator(
        waveform_approximant=waveform_approximant,
        reference_frequency=reference_frequency,
        minimum_frequency=fmin,
        sampling_frequency=sample_rate,
        duration=waveform_duration,
    )

    # initiate prior
    priors = bilby.gw.prior.BBHPriorDict(prior_file)

    # dict to store all parameters
    # of injections
    parameters = defaultdict(list)

    for segment in raw_timeslide.segments:

        # extract start and stop of segment
        start = segment.t0
        stop = segment.tf

        # determine signal times
        # based on length of segment and spacing;
        # The signal time represents the first sample
        # in the signals generated by project_raw_gw.
        # not to be confused with the t0, which should
        # be the middle sample

        signal_times = np.arange(start + buffer, stop - buffer, spacing)
        n_samples = len(signal_times)

        # sample prior for this segment
        segment_parameters = priors.sample(n_samples)

        # append to master parameters dict
        for key, value in segment_parameters.items():
            parameters[key].extend(value)

        # the center of the sample
        # is geocent time
        segment_parameters["geocent_time"] = signal_times + (
            waveform_duration / 2
        )

        # generate raw waveforms
        raw_signals = generate_gw(
            segment_parameters, waveform_generator=waveform_generator
        )

        # dictionary to store
        # gwpy timeseries of background
        raw_ts = {}

        # load segment;
        # expects that ifo is the name
        # of the dataset
        data = segment.load(*ifos)

        # times array is returned last
        times = data[-1]

        for i, ifo in enumerate(ifos):
            raw_ts[ifo] = TimeSeries(data[i], times=times)

            # calculate psd for this segment
            psd = raw_ts[ifo].psd(fftlength)

            # project raw waveforms
            signals, snr = project_raw_gw(
                raw_signals,
                segment_parameters,
                waveform_generator,
                ifo,
                get_snr=True,
                noise_psd=psd,
            )

            # loop over signals, injecting them into the
            # raw strain

            for signal_start, signal in zip(signal_times, signals):
                signal_stop = signal_start + len(signal) * (1 / sample_rate)
                signal_times = np.arange(
                    signal_start, signal_stop, 1 / sample_rate
                )

                # create gwpy timeseries for signal
                signal = TimeSeries(signal, times=signal_times)

                # inject into raw background
                raw_ts[ifo] = raw_ts[ifo].inject(signal)

        # now write this segment to out TimeSlide
        # in files of length file_length
        for t0 in np.arange(start, stop, file_length):
            inj_datasets = {}

            tf = min(t0 + file_length, stop)
            times = np.arange(t0, tf, 1 / sample_rate)

            for ifo in ifos:
                inj_datasets[ifo] = raw_ts[ifo].crop(t0, tf)

            h5.write_timeseries(
                out_timeslide.path, prefix="inj", t=times, **inj_datasets
            )

    # concat parameters for all segments and save
    with h5py.File(out_timeslide.path / "params.h5", "w") as f:
        for k, v in parameters.items():
            f.create_dataset(k, data=v)

    return out_timeslide
