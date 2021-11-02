__all__ = ["models","getpsf","synth","groupfit","leastsquares","allfit",
           "ccddata","detection","aperture","sky","prometheus","utils"]
__version__ = '1.0.2'

from .ccddata import CCDData

read = CCDData.read
