#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
from losoto.lib_operations import *

logging.debug('Loading FLAGSTATION module.')

def _run_parser(soltab, parser, step):
    mode = parser.getstr( step, 'mode') # no default
    maxFlaggedFraction = parser.getfloat( step, 'maxFlaggedFraction', 0.5)
    nSigma = parser.getfloat( step, 'nSigma', 5.0)
    telescope = parser.getstr( step, 'telescope', 'lofar')
    refAnt = parser.getstr( step, 'refAnt', '')
    soltabExport = parser.getstr( step, 'soltabExport', '' )
    ncpu = parser.getint( '_global', 'ncpu', 0)

    parser.checkSpelling( step, soltab, ['mode', 'maxFlaggedFraction', 'nSigma', 'telescope', 'refAnt', 'soltabExport'])
    return run( soltab, mode, maxFlaggedFraction, nSigma, telescope, refAnt, soltabExport, ncpu )


def _flag_resid(vals, weights, soltype, nSigma, maxFlaggedFraction, maxStddev, ants, s, outQueue):
    """
    Flags bad residuals relative to mean by setting the corresponding weights to 0.0

    Parameters
    ----------
    vals : array
        Array of values as [time, ant, freq, pol]

    weights : array
        Array of weights as [time, ant, freq, pol]

    soltype : str
        Type of solutions: phase or amplitude

    nSigma : float
        Number of sigma for flagging. vals outside of nSigma*stddev are flagged

    maxFlaggedFraction : float
        Maximum allowable fraction of flagged frequencies. Stations with higher fractions
        will be completely flagged

    maxStddev : float
        Maximum allowable standard deviation

    ants : list
        List of station names

    s : int
        Station index

    Returns
    -------
    indx, weights : int, array
        Station index, modified weights array
    """
    # Skip fully flagged stations
    if np.all(weights == 0.0):
        outQueue.put([s, weights])
        return

    # Iterate over polarizations
    npols = vals.shape[2] # number of polarizations
    for pol in range(npols):
        # Check flags
        weights_orig = weights[:, :, pol]
        if soltype=='phase':
            bad_sols = np.where(np.isnan(vals[:, :, pol]))
        else:
            bad_sols = np.where(np.logical_or(np.isnan(vals[:, :, pol]), vals[:, :, pol] <= 0.0))
        weights_orig[bad_sols] = 0.0
        if np.all(weights_orig == 0.0):
            # Skip fully flagged polarizations
            continue
        flagged = np.where(weights_orig == 0.0)
        unflagged = np.where(weights_orig != 0.0)

        if soltype == 'amplitude':
            # Take the log
            vals[:, :, pol] = np.log10(vals[:, :, pol])

        # Remove mean (to avoid wraps near +/- pi) and set flagged points to 0
        if soltype=='phase':
            mean = np.angle( np.nansum( weights_orig.flatten() * np.exp(1j*vals[:, :, pol].flatten()) ) / ( vals[:, :, pol].flatten().size * sum(weights_orig.flatten()) ) )
        else:
            mean = np.nansum( weights_orig.flatten() * vals[:, :, pol].flatten() ) / ( vals[:, :, pol].flatten().size * sum(weights_orig.flatten()) )
        vals_flagged = vals[:, :, pol]
        if soltype=='phase':
            # Remove the mean to avoid wrapping issues near +/- pi
            vals_flagged = normalize_phase(vals_flagged - mean)
        vals_flagged[flagged] = 0.0

        # Iteratively fit and flag
        nsols_unflagged = len(vals_flagged[unflagged])
        maxiter = 5
        niter = 0
        nflag = 0
        nflag_prev = -1
        weights_copy = weights_orig.copy()
        while nflag != nflag_prev and niter < maxiter:
            stdev_all = np.sqrt(np.average(vals_flagged**2, weights=weights_copy))
            stdev = min(maxStddev, stdev_all)
            bad = np.where(np.abs(vals_flagged) > nSigma*stdev)
            nflag = len(bad[0])
            if nflag == 0 or nflag == nsols_unflagged:
                break
            if niter > 0:
                nflag_prev = nflag
            weights_copy = weights_orig.copy()  # reset flags to original ones
            weights_copy[bad] = 0
            niter += 1

        # Check whether station is bad (high flagged fraction). If
        # so, flag all frequencies and polarizations
        if float(len(bad[0]))/float(nsols_unflagged) > maxFlaggedFraction:
            # Station has high fraction of initially unflagged solutions that are now flagged
            logging.info('Flagged {0} (pol {1}) due to high flagged fraction '
                  '({2:.2f})'.format(ants[s], pol, float(len(bad[0]))/float(nsols_unflagged)))
            weights[:, :, pol] = 0.0
        else:
            # Station is OK, flag bad points only
            nflagged_orig = len(np.where(weights_orig == 0.0)[0])
            nflagged_new = len(np.where(weights_copy == 0.0)[0])
            weights[:, :, pol] = weights_copy
            prcnt = float(nflagged_new - nflagged_orig) / float(np.product(weights_orig.shape)) * 100.0
            logging.info('Flagged {0:.1f}% of solutions for {1} (pol {2})'.format(prcnt, ants[s], pol))

    outQueue.put([s, weights])


def _flag_bandpass(freqs, amps, weights, telescope, nSigma, maxFlaggedFraction, maxStddev,
                     plot, ants, s, outQueue):
    """
    Flags bad amplitude solutions relative to median bandpass (in log space) by setting
    the corresponding weights to 0.0

    Note: A median over the time axis is done before flagging, so the flags are not time-
    dependent

    Parameters
    ----------
    freqs : array
        Array of frequencies

    amps : array
        Array of amplitudes as [time, ant, freq, pol]

    weights : array
        Array of weights as [time, ant, freq, pol]

    telescope : str, optional
        Specifies the telescope for the bandpass model

    nSigma : float
        Number of sigma for flagging. Amplitudes outside of nSigma*stddev are flagged

    maxFlaggedFraction : float
        Maximum allowable fraction of flagged frequencies. Stations with higher fractions
        will be completely flagged

    maxStddev : float
        Maximum allowable standard deviation

    plot : bool
        If True, the bandpass with flags and best-fit line is plotted for each station

    ants : list
        List of station names

    s : int
        Station index

    Returns
    -------
    indx, weights : int, array
        Station index, modified weights array
    """
    def _B(x, k, i, t, extrap, invert):
        if k == 0:
            if extrap:
                if invert:
                    return -1.0
                else:
                    return 1.0
            else:
                return 1.0 if t[i] <= x < t[i+1] else 0.0
        if t[i+k] == t[i]:
           c1 = 0.0
        else:
           c1 = (x - t[i])/(t[i+k] - t[i]) * _B(x, k-1, i, t, extrap, invert)
        if t[i+k+1] == t[i+1]:
           c2 = 0.0
        else:
           c2 = (t[i+k+1] - x)/(t[i+k+1] - t[i+1]) * _B(x, k-1, i+1, t, extrap, invert)
        return c1 + c2


    def _bspline(x, t, c, k):
        n = len(t) - k - 1
        assert (n >= k+1) and (len(c) >= n)
        invert = False
        extrap = [False] * n
        if x >= t[n]:
            extrap[-1] = True
        elif x < t[k]:
            extrap[0] = True
            invert = False
        return sum(c[i] * _B(x, k, i, t, e, invert) for i, e in zip(range(n), extrap))


    def _bandpass_LBA(freq, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13):
        """
        Defines the functional form of the LBA bandpass in terms of splines of degree 3

        The spline fit was done using LSQUnivariateSpline() on the median bandpass between
        30 MHz and 78 MHz. The knots were set by hand to acheive a good fit with a
        minimum number of parameters.

        Parameters
        ----------
        freq : array
            Array of frequencies

        c1-c13 : float
            Spline coefficients

        Returns
        -------
        bandpass : list
            List of bandpass values as function of frequency
        """
        knots = np.array([30003357.0, 30003357.0, 30003357.0, 30003357.0, 40000000.0,
                          50000000.0, 55000000.0, 56000000.0, 60000000.0, 62000000.0,
                          63000000.0, 64000000.0, 70000000.0, 77610779.0, 77610779.0,
                          77610779.0, 77610779.0])
        coeffs = np.array([c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13])
        return [_bspline(f, knots, coeffs, 3) for f in freq]


    def _bandpass_HBA_low(freq, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10):
        """
        Defines the functional form of the HBA-low bandpass in terms of splines of degree
        3

        The spline fit was done using LSQUnivariateSpline() on the median bandpass between
        120 MHz and 188 MHz. The knots were set by hand to acheive a good fit with a
        minimum number of parameters.

        Parameters
        ----------
        freq : array
            Array of frequencies

        c1-c10 : float
            Spline coefficients

        Returns
        -------
        bandpass : list
            List of bandpass values as function of frequency
        """
        knots = np.array([1.15e+08, 1.15e+08, 1.15e+08, 1.15e+08,
                          1.30e+08, 1.38e+08, 1.48e+08, 1.60e+08,
                          1.68e+08, 1.78e+08, 1.90e+08, 1.90e+08,
                          1.9e+08, 1.9e+08])
        coeffs = np.array([c1, c2, c3, c4, c5, c6, c7, c8, c9, c10])
        return [_bspline(f, knots, coeffs, 3) for f in freq]


    def _fit_bandpass(freq, logamp, sigma, band, do_fit=True):
        """
        Fits amplitudes with one of the bandpass functions

        The initial coefficients were determined from a LSQUnivariateSpline() fit on the
        median bandpass of the appropriate band. The allowable fitting ranges were set by
        hand through testing on a number of observations (to allow the bandpass function
        to adjust for the differences between stations but not to fit to RFI, etc.).

        Parameters
        ----------
        freq : array
            Array of frequencies

        amps : array
            Array of log10(amplitudes)

        sigma : array
            Array of sigma (1/weights**2)

        band : str
            Band name ('hba_low', etc.)

        do_fit : bool, optional
            If True, the fitting is done. If False, the unmodified model bandpass is
            returned

        Returns
        -------
        fit_parms, bandpass : list, list
            List of best-fit parameters, List of bandpass values as function of frequency
        """
        from scipy.optimize import curve_fit

        if band.lower() == 'hba_low':
            bandpass_function = _bandpass_HBA_low
            init_coeffs = np.array([-0.01460369, 0.05062699, 0.02827004, 0.03738518,
                                    -0.05729109, 0.02303295, -0.03550487, -0.0803113,
                                    -0.2394929, -0.358301])
            bounds_deltas_lower = [0.06, 0.05, 0.04, 0.04, 0.04, 0.04, 0.1, 0.1, 0.2, 0.5]
            bounds_deltas_upper = [0.06, 0.1, 0.1, 0.1, 0.04, 0.04, 0.04, 0.04, 0.05, 0.06]
        elif band.lower() == 'lba':
            bandpass_function = _bandpass_LBA
            init_coeffs = np.array([-0.22654016, -0.1950495, -0.07763014, 0.10002095,
                                    0.32797671, 0.46900048, 0.47155583, 0.31945897,
                                    0.29072278, 0.08064795, -0.15761538, -0.36020451,
                                    -0.51163338])
            bounds_deltas_lower = [0.25, 0.2, 0.05, 0.05, 0.05, 0.05, 0.08, 0.05, 0.08, 0.15,
                                   0.15, 0.15, 0.15]
            bounds_deltas_upper = [0.25, 0.2, 0.05, 0.05, 0.05, 0.05, 0.08, 0.05, 0.08, 0.15,
                                   0.15, 0.15, 0.15]
        else:
            print('The "{}" band is not supported'.format(band))
            sys.exit(1)

        if do_fit:
            lower = [c - b for c, b in zip(init_coeffs, bounds_deltas_lower)]
            upper = [c + b for c, b in zip(init_coeffs, bounds_deltas_upper)]
            param_bounds = (lower, upper)
            try:
                popt, pcov = curve_fit(bandpass_function, freq, logamp, sigma=sigma,
                                       bounds=param_bounds, method='dogbox')
                return popt, bandpass_function(freq, *tuple(popt))
            except RuntimeError:
                logging.error('Fitting failed.' )
                return None, bandpass_function(freq, *tuple(init_coeffs))
        else:
            return None, bandpass_function(freq, *tuple(init_coeffs))

    # Check that telescope is supported
    if telescope.lower() == 'lofar':
        # Determine which band we're in
        if np.median(freqs) < 180e6 and np.median(freqs) > 110e6:
            band = 'hba_low'
            median_min = 50.0
            median_max = 200.0
        elif np.median(freqs) < 90e6:
            band = 'lba'
            median_min = 50.0
            median_max = 200.0
        else:
            print('The median frequency of {} Hz is outside of any supported LOFAR band '
                  '(LBA and HBA-low)'.format(np.median(freqs)))
            sys.exit(1)
    else:
       logging.error("Only telescope = 'lofar' is currently supported for bandpass mode.")
       outQueue.put([s, weights])
       return 1

    # Skip fully flagged stations
    if np.all(weights == 0.0):
        outQueue.put([s, weights])
        return

    # Build arrays for fitting
    flagged = np.where(np.logical_or(weights == 0.0, np.isnan(amps)))
    amps_flagged = amps.copy()
    amps_flagged[flagged] = np.nan
    sigma = weights.copy()
    sigma[flagged] = 1.0
    sigma = np.sqrt(1.0 / sigma)
    sigma[flagged] = 1e8

    # Iterate over polarizations
    npols = amps.shape[2] # number of polarizations
    for pol in range(npols):
        # Skip fully flagged polarizations
        if np.all(weights[:, :, pol] == 0.0):
            continue

        # Take median over time and divide out the median offset
        with np.warnings.catch_warnings():
            # Filter NaN warnings -- we deal with NaNs below
            np.warnings.filterwarnings('ignore', r'All-NaN (slice|axis) encountered')
            amps_div = np.nanmedian(amps_flagged[:, :, pol], axis=0)
            median_val = np.nanmedian(amps_div)
        amps_div /= median_val
        sigma_div = np.median(sigma[:, :, pol], axis=0)
        sigma_orig = sigma_div.copy()
        unflagged = np.where(~np.isnan(amps_div))
        nsols_unflagged = len(unflagged[0])
        median_flagged = np.where(np.isnan(amps_div))
        amps_div[median_flagged] = 1.0
        sigma_div[median_flagged] = 1e8
        median_flagged = np.where(amps_div <= 0.0)
        amps_div[median_flagged] = 1.0
        sigma_div[median_flagged] = 1e8

        # Before doing the fitting, renormalize and flag any solutions that deviate from
        # the model bandpass by a large factor to avoid biasing the first fit
        _, bp_sp = _fit_bandpass(freqs, np.log10(amps_div), sigma_div, band, do_fit=False)
        normval = np.median(np.log10(amps_div) - bp_sp) # value to normalize model to data
        amps_div /= 10**normval
        bad = np.where(np.abs(np.array(bp_sp) - np.log10(amps_div)) > 0.2)
        sigma_div[bad] = 1e8

        # Iteratively fit and flag
        maxiter = 5
        niter = 0
        nflag = 0
        nflag_prev = -1
        while nflag != nflag_prev and niter < maxiter:
            p, bp_sp = _fit_bandpass(freqs, np.log10(amps_div), sigma_div, band)
            stdev_all = np.sqrt(np.average((bp_sp-np.log10(amps_div))**2, weights=(1/sigma_div)**2))
            stdev = min(maxStddev, stdev_all)
            bad = np.where(np.abs(bp_sp - np.log10(amps_div)) > nSigma*stdev)
            nflag = len(bad[0])
            if nflag == 0 or nflag == nsols_unflagged:
                break
            if niter > 0:
                nflag_prev = nflag
            sigma_div = sigma_orig.copy()  # reset flags to original ones
            sigma_div[bad] = 1e8
            niter += 1

        if plot:
            import matplotlib.pyplot as plt
            plt.plot(freqs, bp_sp, 'g-', lw=3)
            plt.plot(freqs, np.log10(amps_div), 'o', c='g')
            plt.plot(freqs[bad], np.log10(amps_div)[bad], 'o', c='r')
            plt.show()

        # Check whether entire station is bad (high stdev or high flagged fraction). If
        # so, flag all frequencies and polarizations
        if stdev_all > nSigma*maxStddev:
            # Station has high stddev relative to median bandpass
            logging.info('Flagged {0} (pol {1}) due to high stddev '
                  '({2})'.format(ants[s], pol, stdev_all))
            weights[:, :, pol] = 0.0
        elif float(len(bad[0]))/float(nsols_unflagged) > maxFlaggedFraction:
            # Station has high fraction of initially unflagged solutions that are now flagged
            logging.info('Flagged {0} (pol {1}) due to high flagged fraction '
                  '({2:.2f})'.format(ants[s], pol, float(len(bad[0]))/float(nsols_unflagged)))
            weights[:, :, pol] = 0.0
        else:
            flagged = np.where(sigma_div > 1e3)
            nflagged_orig = len(np.where(weights[:, :, pol] == 0.0)[0])
            weights[:, flagged[0], pol] = 0.0
            nflagged_new = len(np.where(weights[:, :, pol] == 0.0)[0])
            median_val = np.nanmedian(amps[np.where(weights[:, :, pol] > 0.0)])
            if median_val < median_min or median_val > median_max:
                # Station has extreme median value
                logging.info('Flagged {0} (pol {1}) due to extreme median value '
                      '({2})'.format(ants[s], pol, median_val))
                weights[:, :, pol] = 0.0
            else:
                # Station is OK, flag bad points only
                prcnt = float(nflagged_new - nflagged_orig) / float(np.product(weights.shape[:-1])) * 100.0
                logging.info('Flagged {0:.1f}% of solutions for {1} (pol {2})'.format(prcnt, ants[s], pol))

    outQueue.put([s, weights])


def run( soltab, mode, maxFlaggedFraction=0.5, nSigma=5.0, telescope='lofar', refAnt='', soltabExport='', ncpu=0 ):
    """
    This operation for LoSoTo implements a station-flagging procedure. Flags are time-independent.
    WEIGHT: compliant

    Parameters
    ----------
    mode: str
        Fitting algorithm: bandpass or resid. Bandpass mode clips amplitudes relative to a model bandpass (only LOFAR is currently supported). Resid mode clips residual phases or log(amplitudes).

    maxFlaggedFraction : float, optional
        This sets the maximum allowable fraction of flagged solutions above which the entire station is flagged.

    nSigma : float, optional
        This sets the number of standard deviations considered when outlier clipping is done

    telescope : str, optional
        Specifies the telescope if mode = 'bandpass'.

    refAnt : str, optional
        If mode=phaseresid, this sets the reference antenna, by default None.

    soltabExport : str, optional
        Soltab to export station flags to. Note: exported flags are not time- or frequency-dependent.

    ncpu : int, optional
        Number of cpu to use, by default all available.
    """

    logging.info("Flagging on soltab: "+soltab.name)

    # input check
    if refAnt == '':
        refAnt = None
    if soltabExport == '':
        soltabExport = None

    if mode == None or mode.lower() not in ['bandpass', 'resid']:
        logging.error('Mode must be one of bandpass or resid')
        return 1

    # Axis order must be [time, ant, freq, pol], so reorder if necessary
    axis_names = soltab.getAxesNames()
    if ('freq' not in axis_names or 'pol' not in axis_names or
        'time' not in axis_names or 'ant' not in axis_names):
       logging.error("Currently, flagstation requires the following axes: "
                     "freq, pol, time, and ant.")
       return 1
    freq_ind = axis_names.index('freq')
    pol_ind = axis_names.index('pol')
    time_ind = axis_names.index('time')
    ant_ind = axis_names.index('ant')
    if 'dir' in axis_names:
        dir_ind = axis_names.index('dir')
        vals_arraytmp = soltab.val[:].transpose([time_ind, ant_ind, freq_ind, pol_ind, dir_ind])
        weights_arraytmp = soltab.weight[:].transpose([time_ind, ant_ind, freq_ind, pol_ind, dir_ind])
    else:
        vals_arraytmp = soltab.val[:].transpose([time_ind, ant_ind, freq_ind, pol_ind])
        weights_arraytmp = soltab.weight[:].transpose([time_ind, ant_ind, freq_ind, pol_ind])

    # Check for NaN solutions and flag
    flagged = np.where(np.isnan(vals_arraytmp))
    weights_arraytmp[flagged] = 0.0

    if mode == 'bandpass':
        solType = soltab.getType()
        if solType != 'amplitude':
           logging.error("Soltab must be of type amplitude for bandpass mode.")
           return 1

        # Fill the queue
        mpm = multiprocManager(ncpu, _flag_bandpass)
        for s in range(len(soltab.ant)):
            mpm.put([soltab.freq[:], vals_arraytmp[:, s, :, :], weights_arraytmp[:, s, :, :],
                     telescope, nSigma, maxFlaggedFraction, 0.01, False, soltab.ant[:], s])
        mpm.wait()

        # Write new weights
        for (s, w) in mpm.get():
            weights_arraytmp[:, s, :, :] = w
        weights_array = weights_arraytmp.transpose([time_ind, ant_ind, freq_ind, pol_ind])
        soltab.setValues(weights_array, weight=True)
        soltab.addHistory('FLAGSTATION (mode=bandpass, telescope={0}, maxFlaggedFraction={1}, '
                          'nSigma={2}'.format(telescope, maxFlaggedFraction, nSigma))
    else:
        solType = soltab.getType()
        if solType not in ['phase', 'amplitude']:
           logging.error("Soltab must be of type phase or amplitude for resid mode.")
           return 1
        if solType == 'phase':
            maxStddev = 0.1 # in radians
        else:
            maxStddev = 0.02 # in log10(amp)

        # Fill the queue
        if 'dir' in axis_names:
            for d, dirname in enumerate(soltab.dir):
                mpm = multiprocManager(ncpu, _flag_resid)
                for s in range(len(soltab.ant)):
                    mpm.put([vals_arraytmp[:, s, :, :, d], weights_arraytmp[:, s, :, :, d], solType, nSigma, maxFlaggedFraction, maxStddev, soltab.ant[:], s])
                mpm.wait()
                for (s, w) in mpm.get():
                    weights_arraytmp[:, s, :, :, d] = w
        else:
            mpm = multiprocManager(ncpu, _flag_resid)
            for s in range(len(soltab.ant)):
                mpm.put([vals_arraytmp[:, s, :, :], weights_arraytmp[:, s, :, :], solType, nSigma, maxFlaggedFraction, maxStddev, soltab.ant[:], s])
            mpm.wait()
            for (s, w) in mpm.get():
                weights_arraytmp[:, s, :, :] = w

        # Write new weights
        if 'dir' in axis_names:
            weights_array = weights_arraytmp.transpose([time_ind, ant_ind, freq_ind, pol_ind, dir_ind])
        else:
            weights_array = weights_arraytmp.transpose([time_ind, ant_ind, freq_ind, pol_ind])
        soltab.setValues(weights_array, weight=True)
        soltab.addHistory('FLAGSTATION (mode=resid, maxFlaggedFraction={0}, '
                          'nSigma={1}'.format(maxFlaggedFraction, nSigma))

    if soltabExport is not None:
        # Transfer station flags to soltabExport
        solset = soltab.getSolset()
        soltabexp = solset.getSoltab(soltabExport)
        axis_namesexp = soltabexp.getAxesNames()

        for stat in soltabexp.ant:
            if stat in soltab.ant:
                s = soltab.ant[:].tolist().index(stat)
                if 'pol' in axis_namesexp:
                    for pol in soltabexp.pol:
                        if pol in soltab.pol:
                            soltabexp.setSelection(ant=stat, pol=pol)
                            p = soltab.pol[:].tolist().index(pol)
                            if np.all(weights_arraytmp[:, s, :, p] == 0):
                                soltabexp.setValues(np.zeros(soltabexp.weight.shape), weight=True)
                else:
                    soltabexp.setSelection(ant=stat)
                    if np.all(weights_arraytmp[:, s, :, :] == 0):
                        soltabexp.setValues(np.zeros(soltabexp.weight.shape), weight=True)
        soltabexp.addHistory('WEIGHT imported by FLAGSTATION from '+soltab.name+'.')

    return 0
