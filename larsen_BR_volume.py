import numpy as np

def volume_source(A_best, A_low=None, A_high=None, type='bedrock'):
    """ Returns volume estimates based off landslide source areas using bedrock failure
        values from Larsen, I.J., Montgomery, D.R., Korup, O., 2010, Landslide erosion
        controlled by hillslope material: Nature Geoscience, v. 3, doi: 10.1038/NGEO776
        The standard deviation of log(Area) is estimated approximately by
        assuming the range of log10(A_low) to log10(A_high) spans the 95% range of
        +/- 2 standard deviations, so std is estimated as the mean of
        log(A_best)-log(A_low) and log(A_high)-log(A_best) divided by 2.
        Note that the std values from Larsen reported here are called
        standard deviation in their main text but standard error in the
        supplement so their exact meaning is unclear. We assume they are the
        standard deviation not the standard error because otherwise we find
        that the ranges given after error propagation are unrealistically
        large.

    Args:
    A_best (float): Best estimate of source area
    A_low (float): Low estimate of source area (optional). If A_low and A_high
        are not provided, the uncertainties are estimated under the assumption
        that std(log(A_best)) is zero.
    A_high (float): High estimate of source area (optional)
    type (str): 'bedrock' or 'soil', 'bedrock' is default

    Returns:
        best, low, and high volume estimates (+/- 1 standard deviation computed
            in log units)
    """
    if type == 'soil':
        # Soil scar numbers from Larsen et al, Table S1
        logalpha = -0.649  # Log base 10
        stdlogalpha = 0.021
        gamma = 1.262
        stdgamma = 0.009
    elif type == 'bedrock':
        # Bedrock scar numbers from Larsen et al, Table S1
        logalpha = -0.63  # Log base 10
        stdlogalpha = 0.06
        gamma = 1.41
        stdgamma = 0.02
    else:
        raise Exception('type must be bedrock or soil')

    # Estimate log Volume
    logV = logalpha + gamma * np.log10(A_best)
    V = 10.**logV  # Linear volume

    if A_high is not None and A_low is not None:
        if A_high < A_best:
            raise Exception('A_high must be larger than A_best')
        if A_low > A_best:
            raise Exception('A_low must be smaller than A_best')
        # Estimate std of logArea
        d1 = np.log10(A_best)-np.log10(A_low)
        d2 = np.log10(A_high)-np.log10(A_best)
        stdlogA = np.mean([d1, d2])/2.

        # Estimate std log Volume
        K = stdlogalpha**2 + (np.log10(A_best)**2 * stdgamma**2) + (gamma**2 * stdlogA**2)
        stdlogV = np.sqrt(K)
        logVlow = logV - stdlogV
        logVhigh = logV + stdlogV
        Vlow = 10.**logVlow
        Vhigh = 10.**logVhigh
    else: # Estimate under assumption that stdlogA = 0
        K = stdlogalpha**2 + (np.log10(A_best)**2 * stdgamma**2)
        stdlogV = np.sqrt(K)
        logVlow = logV - stdlogV
        logVhigh = logV + stdlogV
        Vlow = 10.**logVlow
        Vhigh = 10.**logVhigh

    return V, Vlow, Vhigh
