import os
import numpy as np
import time
from numba import njit,types,from_dtype
from numba.experimental import jitclass
from . import models_numba as mnb

# Fit a PSF model to multiple stars in an image

@njit
def quadratic_bisector(x,y):
    """ Calculate the axis of symmetric or bisector of parabola"""
    #https://www.azdhs.gov/documents/preparedness/state-laboratory/lab-licensure-certification/technical-resources/
    #    calibration-training/12-quadratic-least-squares-regression-calib.pdf
    #quadratic regression statistical equation
    n = len(x)
    if n<3:
        return None
    Sxx = np.sum(x**2) - np.sum(x)**2/n
    Sxy = np.sum(x*y) - np.sum(x)*np.sum(y)/n
    Sxx2 = np.sum(x**3) - np.sum(x)*np.sum(x**2)/n
    Sx2y = np.sum(x**2 * y) - np.sum(x**2)*np.sum(y)/n
    Sx2x2 = np.sum(x**4) - np.sum(x**2)**2/n
    #a = ( S(x^2*y)*S(xx)-S(xy)*S(xx^2) ) / ( S(xx)*S(x^2x^2) - S(xx^2)^2 )
    #b = ( S(xy)*S(x^2x^2) - S(x^2y)*S(xx^2) ) / ( S(xx)*S(x^2x^2) - S(xx^2)^2 )
    denom = Sxx*Sx2x2 - Sxx2**2
    if denom==0:
        return np.nan
    a = ( Sx2y*Sxx - Sxy*Sxx2 ) / denom
    b = ( Sxy*Sx2x2 - Sx2y*Sxx2 ) / denom
    if a==0:
        return np.nan
    return -b/(2*a)

#@njit
def starcube(tab,image,npix=51,fillvalue=np.nan):
    """
    Produce a cube of cutouts of stars.

    Parameters
    ----------
    tab : table
       The catalog of stars to use.  This should have "x" and "y" columns and
         preferably also "amp".
    image : CCDData object
       The image to use to generate the stellar images.
    fillvalue : float, optional
       The fill value to use for pixels that are bad are off the image.
            Default is np.nan.

    Returns
    -------
    cube : numpy array
       Two-dimensional cube (Npix,Npix,Nstars) of the star images.

    Example
    -------

    cube = starcube(tab,image)

    """

    # Get the residuals data
    nstars = len(tab)
    nhpix = npix//2
    cube = np.zeros((npix,npix,nstars),float)
    xx,yy = np.meshgrid(np.arange(npix)-nhpix,np.arange(npix)-nhpix)
    rr = np.sqrt(xx**2+yy**2)        
    x = xx[0,:]
    y = yy[:,0]
    for i in range(nstars):
        xcen = tab['x'][i]            
        ycen = tab['y'][i]
        bbox = mnb.starbbox((xcen,ycen),image.shape,nhpix)
        im = image[bbox.slices]
        flux = image.data[bbox.slices]-image.sky[bbox.slices]
        err = image.error[bbox.slices]
        if 'amp' in tab.columns:
            amp = tab['amp'][i]
        elif 'peak' in tab.columns:
            amp = tab['peak'][i]
        else:
            amp = flux[int(np.round(ycen)),int(np.round(xcen))]
        xim,yim = np.meshgrid(im.x,im.y)
        xim = xim.astype(float)-xcen
        yim = yim.astype(float)-ycen
        # We need to interpolate this onto the grid
        f = RectBivariateSpline(yim[:,0],xim[0,:],flux/amp)
        im2 = np.zeros((npix,npix),float)+np.nan
        xcover = (x>=bbox.ixmin-xcen) & (x<=bbox.ixmax-1-xcen)
        xmin,xmax = dln.minmax(np.where(xcover)[0])
        ycover = (y>=bbox.iymin-ycen) & (y<=bbox.iymax-1-ycen)
        ymin,ymax = dln.minmax(np.where(ycover)[0])            
        im2[ymin:ymax+1,xmin:xmax+1] = f(y[ycover],x[xcover],grid=True)
        # Stuff it into 3D array
        cube[:,:,i] = im2
    return cube


#@njit
def mkempirical(cube,order=0,coords=None,shape=None,lookup=False):
    """
    Take a star cube and collapse it to make an empirical PSF using median
    and outlier rejection.

    Parameters
    ----------
    cube : numpy array
      Three-dimensional cube of star images (or residual images) of shape
        (Npix,Npix,Nstars).
    order : int, optional
      The order of the variations. 0-constant, 1-linear terms.  If order=1,
        Then coords and shape must be input.
    coords : tuple, optional
      Two-element tuple of the X/Y coordinates of the stars.  This is needed
        to generate the linear empirical model (order=1).
    shape : tuple, optional
      Two-element tuple giving the shape (Ny,Nx) of the image.  This is
        needed to generate the linear empirical model (order=1).
    lookup : boolean, optional
      Parameter to indicate if this is a lookup table.  If lookup=False, then
      the constant term is constrained to be non-negative. Default is False.

    Returns
    -------
    epsf : numpy array
      The empirical PSF model  If order=0, then this is just a 2D image.  If
        order=1, then it will be a 3D cube (Npix,Npix,4) where the four terms
        are [constant, X-term, Y-term, X*Y-term].  If rect=True, then a list
        of RectBivariateSpline functions are returned.

    Example
    -------

    epsf = mkempirical(cube,order=0)

    or

    epsf = mkempirical(cube,order=1,coords=coords,shape=im.shape)

    """

    ny,nx,nstar = cube.shape
    npix = ny
    nhpix = ny//2
    
    # Do outlier rejection in each pixel
    med = np.nanmedian(cube,axis=2)
    bad = ~np.isfinite(med)
    if np.sum(bad)>0:
        med[bad] = np.nanmedian(med)
    sig = dln.mad(cube,axis=2)
    bad = ~np.isfinite(sig)
    if np.sum(bad)>0:
        sig[bad] = np.nanmedian(sig)        
    # Mask outlier points
    outliers = ((np.abs(cube-med.reshape((med.shape)+(-1,)))>3*sig.reshape((med.shape)+(-1,)))
                & np.isfinite(cube))
    nbadstar = np.sum(outliers,axis=(0,1))
    goodmask = ((np.abs(cube-med.reshape((med.shape)+(-1,)))<3*sig.reshape((med.shape)+(-1,)))
                & np.isfinite(cube))    
    # Now take the mean of the unmasked pixels
    macube = np.ma.array(cube,mask=~goodmask)
    medim = macube.mean(axis=2)
    medim = medim.data

    # Check how well each star fits the median
    goodpix = macube.count(axis=(0,1))
    rms = np.sqrt(np.nansum((cube-medim.reshape((medim.shape)+(-1,)))**2,axis=(0,1))/goodpix)

    xx,yy = np.meshgrid(np.arange(npix)-nhpix,np.arange(npix)-nhpix)
    rr = np.sqrt(xx**2+yy**2)        
    x = xx[0,:]
    y = yy[:,0]
    mask = (rr<=nhpix)
    
    # Constant
    if order==0:
        # Make sure it goes to zero at large radius
        medim *= mask  # mask corners
        # Make sure values are positive
        if lookup==False:
            medim = np.maximum(medim,0.0)
        else:
            fpars = medim
            
    # Linear
    elif order==1:
        if coords is None or shape is None:
            raise ValueError('Need coords and shape with order=1')
        fpars = np.zeros((ny,nx,4),float)
        # scale coordinates to -1 to +1
        xcen,ycen = coords
        relx,rely = mnb.relcoord(xcen,ycen,shape)
        # Loop over pixels and fit line to x/y
        for i in range(ny):
            for j in range(nx):
                data1 = cube[i,j,:]
                if np.sum(np.abs(data1)) != 0:
                    # maybe use a small maxiter
                    pars1,perror1 = utils.poly2dfit(relx,rely,data1)
                    fpars[i,j,:] = pars1
                
    return fpars,nbadstar,rms

@njit
def starbbox(coords,imshape,radius):
    """                                                                                         
    Return the boundary box for a star given radius and image size.                             
                                                                                                
    Parameters                                                                                  
    ----------                                                                                  
    coords: list or tuple                                                                       
       Central coordinates (xcen,ycen) of star (*absolute* values).                             
    imshape: list or tuple                                                                      
       Image shape (ny,nx) values.  Python images are (Y,X).                                    
    radius: float                                                                               
       Radius in pixels.                                                                        
                                                                                                
    Returns                                                                                     
    -------                                                                                     
    bbox : BoundingBox object                                                                   
       Bounding box of the x/y ranges.                                                          
       Upper values are EXCLUSIVE following the python convention.                              
                                                                                                
    """

    # pixels span +/-0.5 pixels in each direction
    # so pixel x=5 spans x=4.5-5.5
    # Use round() to get the value of the pixel that is covers
    # the coordinate
    # to include pixels at the bottom but not the top we have to
    # subtract a tiny bit before we round
    eta = 1e-5

    # Star coordinates
    xcen,ycen = coords
    ny,nx = imshape   # python images are (Y,X)
    x0 = xcen-radius
    x1 = xcen+radius
    y0 = ycen-radius
    y1 = ycen+radius
    xlo = np.maximum(int(np.round(x0-eta)),0)
    xhi = np.minimum(int(np.round(x1-eta))+1,nx)
    ylo = np.maximum(int(np.round(y0-eta)),0)
    yhi = np.minimum(int(np.round(y1-eta))+1,ny)
    # add 1 at the upper end because those values are EXCLUDED
    # by the standard python convention

    # The old way of doing it
    #xlo = np.maximum(int(np.floor(xcen-radius)),0)
    #xhi = np.minimum(int(np.ceil(xcen+radius+1)),nx)
    #ylo = np.maximum(int(np.floor(ycen-radius)),0)
    #yhi = np.minimum(int(np.ceil(ycen+radius+1)),ny)
    return np.array([xlo,xhi,ylo,yhi])

@njit
def sliceinsert(array,lo,insert):
    """ Insert array values."""
    n = insert.size
    for i in range(n):
        j = i + lo
        array[j] = insert[i]
    # array is updated in place  


@njit
def getstar(image,error,xcen,ycen,fitradius):
    """ Return the entire footprint image/error/x/y arrays for one star."""
    # always return the same size
    # a distance of 6.2 pixels spans 6 full pixels but you could have
    # 0.1 left on one side and 0.1 left on the other side
    # that's why we have to add 2 pixels
    npix = int(np.floor(2*fitradius))+2
    bbox = starbbox((xcen,ycen),image.shape,fitradius)
    nx = bbox[1]-bbox[0]
    ny = bbox[3]-bbox[2]
    # extra buffer is ALWAYS at the end of each dimension    
    imdata = np.zeros(npix*npix,float)+np.nan
    errdata = np.zeros(npix*npix,float)+np.nan
    xdata = np.zeros(npix*npix,np.int32)-1
    ydata = np.zeros(npix*npix,np.int32)-1
    count = 0
    for j in range(npix):
        y = j + bbox[2]
        for i in range(npix):
            x = i + bbox[0]            
            xdata[count] = x
            ydata[count] = y
            if x>=bbox[0] and x<=bbox[1]-1 and y>=bbox[2] and y<=bbox[3]-1:
                imdata[count] = image[y,x]
                errdata[count] = error[y,x]
            count += 1
    return imdata,errdata,xdata,ydata,bbox,nx,ny

@njit
def collatestars(image,error,starx,stary,fitradius):
    """ Get the entire footprint image/error/x/y for all of the stars."""
    nstars = len(starx)
    npix = int(np.floor(2*fitradius))+2
    # Get xdata, ydata, error
    imdata = np.zeros((nstars,npix*npix),float)
    errdata = np.zeros((nstars,npix*npix),float)
    xdata = np.zeros((nstars,npix*npix),np.int32)
    ydata = np.zeros((nstars,npix*npix),np.int32)
    bbox = np.zeros((nstars,4),np.int32)
    shape = np.zeros((nstars,2),np.int32)
    for i in range(nstars):
        imdata1,errdata1,xdata1,ydata1,bbox1,nx1,ny1 = getstar(image,error,starx[i],stary[i],fitradius)
        imdata[i,:] = imdata1
        errdata[i,:] = errdata1
        xdata[i,:] = xdata1
        ydata[i,:] = ydata1
        bbox[i,:] = bbox1
        shape[i,0] = ny1
        shape[i,1] = nx1
    return imdata,errdata,xdata,ydata,bbox,shape

@njit
def unpackstar(imdata,errdata,xdata,ydata,bbox,shape,istar):
    """ Return unpacked data for one star."""
    imdata1 = imdata[istar,:].copy()
    errdata1 = errdata[istar,:].copy()
    xdata1 = xdata[istar,:].copy()
    ydata1 = ydata[istar,:].copy()
    bbox1 = bbox[istar,:]
    shape1 = shape[istar,:]
    n = len(imdata1)
    npix = int(np.sqrt(n))
    # Convert to 2D arrays
    imdata1 = imdata1.reshape(npix,npix)
    errdata1 = errdata1.reshape(npix,npix)
    xdata1 = xdata1.reshape(npix,npix)
    ydata1 = ydata1.reshape(npix,npix)
    # Trim values
    if shape1[0] < npix or shape1[1] < npix:
        imdata1 = imdata1[:shape1[0],:shape1[1]]
        errdata1 = errdata1[:shape1[0],:shape1[1]]
        xdata1 = xdata1[:shape1[0],:shape1[1]]
        ydata1 = ydata1[:shape1[0],:shape1[1]]
    return imdata1,errdata1,xdata1,ydata1,bbox1,shape1

@njit
def getfitstar(image,error,xcen,ycen,fitradius):
    """ Get the fitting pixels for a single star."""
    npix = int(np.floor(2*fitradius))+2
    bbox = starbbox((xcen,ycen),image.shape,fitradius)
    flux = image[bbox[2]:bbox[3],bbox[0]:bbox[1]].copy()
    err = error[bbox[2]:bbox[3],bbox[0]:bbox[1]].copy()
    nflux = flux.size
    nx = bbox[1]-bbox[0]
    ny = bbox[3]-bbox[2]
    imdata = np.zeros(npix*npix,float)
    errdata = np.ones(npix*npix,float)
    xdata = np.zeros(npix*npix,np.int32)-1
    ydata = np.zeros(npix*npix,np.int32)-1
    mask = np.zeros(npix*npix,np.int32)
    count = 0
    for j in range(ny):
        y = j + bbox[2]
        for i in range(nx):
            x = i + bbox[0]
            r = np.sqrt((x-xcen)**2 + (y-ycen)**2)
            if r <= fitradius:
                imdata[count] = flux[j,i]
                errdata[count] = error[j,i]
                xdata[count] = x
                ydata[count] = y
                mask[count] = 1
                count += 1
    return imdata,errdata,xdata,ydata,count,mask
        
@njit
def collatefitstars(image,error,starx,stary,fitradius):
    nstars = len(starx)
    npix = int(np.floor(2*fitradius))+2
    # Get xdata, ydata, error
    maxpix = nstars*(npix)**2
    imdata = np.zeros((nstars,npix*npix),float)
    errdata = np.zeros((nstars,npix*npix),float)
    xdata = np.zeros((nstars,npix*npix),np.int32)
    ydata = np.zeros((nstars,npix*npix),np.int32)
    mask = np.zeros((nstars,npix*npix),np.int32)
    ndata = np.zeros(nstars,np.int32)
    bbox = np.zeros((nstars,4),np.int32)
    for i in range(nstars):
        xcen = starx[i]
        ycen = stary[i]
        bb = starbbox((xcen,ycen),image.shape,fitradius)
        imdata1,errdata1,xdata1,ydata1,n1,mask1 = getfitstar(image,error,xcen,ycen,fitradius)
        imdata[i,:] = imdata1
        errdata[i,:] = errdata1
        xdata[i,:] = xdata1
        ydata[i,:] = ydata1
        mask[i,:] = mask1
        ndata[i] = n1
        bbox[i,:] = bb
    return imdata,errdata,xdata,ydata,bbox,ndata,mask

@njit
def unpackfitstar(imdata,errdata,xdata,ydata,bbox,ndata,istar):
    """ Return unpacked fitting data for one star."""
    imdata1 = imdata[istar,:]
    errdata1 = errdata[istar,:]
    xdata1 = xdata[istar,:]
    ydata1 = ydata[istar,:]
    bbox1 = bbox[istar,:]
    n1 = ndata[istar]
    # Trim to the values we want
    imdata1 = imdata1[:n1]
    errdata1 = errdata1[:n1]
    xdata1 = xdata1[:n1]
    ydata1 = ydata1[:n1]
    return imdata1,errdata1,xdata1,ydata1,bbox1,n1

    
kv_ty = (types.int64, types.unicode_type)
spec = [
    ('psftype', types.int32),
    ('params', types.float64[:]),
    ('lookup', types.float64[:,:,:]),
    ('order', types.int32),
    ('fwhm', types.float64),
    ('image', types.float64[:,:]),
    ('error', types.float64[:,:]),
    ('starinit', types.float64[:,:]),
    ('nstars', types.int32),
    ('niter', types.int32),
    ('npsfpix', types.int32),
    ('nx', types.int32),
    ('ny', types.int32),
    ('fitradius', types.float64),
    ('nfitpix', types.int32),
    ('staramp', types.float64[:]),
    ('starxcen', types.float64[:]),
    ('starycen', types.float64[:]),
    ('starchisq', types.float64[:]),
    ('starrms', types.float64[:]),
    ('starnpix', types.int32[:]),
    ('star_imdata', types.float64[:,:]),
    ('star_errdata', types.float64[:,:]),
    ('star_xdata', types.int32[:,:]),
    ('star_ydata', types.int32[:,:]),
    ('star_bbox', types.int32[:,:]),
    ('star_shape', types.int32[:,:]),
    ('starfit_imdata', types.float64[:,:]),
    ('starfit_errdata', types.float64[:,:]),
    ('starfit_xdata', types.int32[:,:]),
    ('starfit_ydata', types.int32[:,:]),
    ('starfit_mask', types.int32[:,:]),
    ('starfit_bbox', types.int32[:,:]),
    ('starfit_ndata', types.int32[:]),
    ('npix', types.int32),
    ('radius', types.int32),
    ('verbose', types.boolean),
    ('niter', types.int32),
    ('_unitfootflux', types.float64),
    ('lookup', types.float64[:,:,:]),
    ('_bounds', types.float64[:,:]),
    ('_steps', types.float64[:]),
    ('coords', types.float64[:]),
    ('imshape', types.int32[:]),
    ('order', types.int32),
]

@jitclass(spec)
class PSFFitter(object):

    def __init__(self,psftype,psfparams,image,error,starx,stary,starflux,fitradius,verbose=False):
        self.verbose = verbose
        self.psftype = psftype
        self.params = psfparams
        #self.lookup = psf.lookup
        #self.order = psf.order
        #self.fwhm = psf.fwhm()
        self.image = image.astype(np.float64)
        self.error = error.astype(np.float64)
        self.imshape = np.zeros(2,np.int32)
        self.imshape[:] = image.shape
        nstars = len(starx)
        self.starinit = np.zeros((nstars,3),float)
        self.starinit[:,0] = starx
        self.starinit[:,1] = stary
        self.starinit[:,2] = starflux
        self.nstars = nstars
        self.niter = 0
        #self.npsfpix = psf.npix
        ny,nx = image.shape
        self.nx = nx
        self.ny = ny
        #if np.isfinite(fitradius)==False:
        #    fitradius = self.fwhm*1.5
        self.fitradius = fitradius
        self.nfitpix = int(np.floor(2*fitradius))+2  # max pixels 
        self.staramp = np.zeros(self.nstars,float)
        self.staramp[:] = starflux/(2*fitradius+1)**2
        # current best-fit values
        self.starxcen = np.zeros(self.nstars,float)
        self.starxcen[:] = self.starinit[:,0].copy()
        self.starycen = np.zeros(self.nstars,float)
        self.starycen[:] = self.starinit[:,1].copy()
        self.starchisq = np.zeros(self.nstars,float)
        self.starrms = np.zeros(self.nstars,float)
        self.starnpix = np.zeros(self.nstars,np.int32)

        # Get information for all the stars
        imdata,errdata,xdata,ydata,bbox,shape = collatestars(image,error,self.starxcen,
                                                             self.starycen,fitradius)
        self.star_imdata = imdata
        self.star_errdata = errdata
        self.star_xdata = xdata
        self.star_ydata = ydata
        self.star_bbox = bbox
        self.star_shape = shape
        
        # Get fitting information for all the stars
        fimdata,ferrdata,fxdata,fydata,fbbox,fndata,fmask = collatefitstars(image,error,self.starxcen,
                                                                            self.starycen,fitradius)
        self.starfit_imdata = fimdata
        self.starfit_errdata = ferrdata
        self.starfit_xdata = fxdata
        self.starfit_ydata = fydata
        self.starfit_mask = fmask
        self.starfit_bbox = fbbox
        self.starfit_ndata = fndata

    def unpackstar(self,istar):
        return unpackstar(self.star_imdata,self.star_errdata,self.star_xdata,
                          self.star_ydata,self.star_bbox,self.star_shape,istar)

    def unpackfitstar(self,istar):
        return unpackfitstar(self.starfit_imdata,self.starfit_errdata,self.starfit_xdata,
                             self.starfit_ydata,self.starfit_bbox,self.starfit_ndata,istar)

    def psf(self,x,y,pars,psfparams,lookup,deriv=False):
        """ Get a PSF model for a single star."""
        return mnb.psf(x,y,pars,self.psftype,psfparams,lookup,
                       self.imshape,deriv=deriv,verbose=self.verbose)

    def model(self,x,args,refit=True,verbose=False):
        """ model function."""
        # input the model parameters
        
        if self.verbose:
            print('model: '+str(self.niter)+' ',args)
        nparsarr = np.array([6,7,8,8,7])
        npars = nparsarr[self.psftype-1]
        # Loop over the stars and generate the model image
        allmodel = np.zeros(self.star_imdata.shape,float)
        maxpix = self.star_imdata.shape[1]
        for i in range(self.nstars):
            im1,err1,xdata1,ydata1,bbox1,n1 = self.unpackfitstar(i)
            allpars = np.zeros(npars,float)
            allpars[0] = self.staramp[i]
            allpars[1] = self.starxcen[i]
            allpars[2] = self.starycen[i]
            allpars[3:] = args
            print(i,allpars)
            g,_ = mnb.amodel2d(xdata1,ydata1,self.psftype,allpars,0)
            allmodel[i,:len(g)] = g
        return allmodel

    def chisq(self,pars):
        """ Create the model and return chisq for this set of PSF parameters."""
        m = self.model(np.arange(10),pars)
        chisq = np.sum((self.starfit_imdata-m)**2/self.starfit_errdata**2)        
        return chisq
        
    
            #image = self.imdata[i]
            #amp = self.staramp[i]
            #xcenorig = self.starxcenorig[i]   
            #ycenorig = self.starycenorig[i]
            #xcen = self.starxcen[i]   
            #ycen = self.starycen[i]            
            #bbox = self.bboxdata[i]
            #x = self.xlist[i]
            #y = self.ylist[i]
            #pixstart = self.pixstart[i]
            #npix = self.npix[i]
            #flux = self.imflatten[pixstart:pixstart+npix]
            #err = self.errflatten[pixstart:pixstart+npix]

            #x0orig = xcenorig - bbox.ixmin
            #y0orig = ycenorig - bbox.iymin
            #x0 = xcen - bbox.ixmin
            #y0 = ycen - bbox.iymin            
            
            # # Fit amp/xcen/ycen if niter=1
            # if refit:
            #     #if (self.niter<=1): # or self.niter%3==0):
            #     if self.niter>-1:
            #         # force the positions to stay within +/-2 pixels of the original values
            #         bounds = (np.array([0,np.maximum(x0orig-2,0),np.maximum(y0orig-2,0),-np.inf]),
            #                   np.array([np.inf,np.minimum(x0orig+2,bbox.shape[1]-1),np.minimum(y0orig+2,bbox.shape[0]-1),np.inf]))
            #         # the image still has sky in it, use sky (nosky=False)
            #         if np.isfinite(psf.fwhm())==False:
            #             print('nan fwhm')
            #             #import pdb; pdb.set_trace()
            #         pars,perror,model = psf.fit(image,[amp,x0,y0],nosky=False,retpararray=True,niter=5,bounds=bounds)
            #         xcen += (pars[1]-x0)
            #         ycen += (pars[2]-y0)
            #         amp = pars[0]                    
            #         self.staramp[i] = amp
            #         self.starxcen[i] = xcen
            #         self.starycen[i] = ycen
            #         model = psf(x,y,pars=[amp,xcen,ycen])
            #         if verbose:
            #             print('Star '+str(i)+' Refitting all parameters')
            #             print(str([amp,xcen,ycen]))

            #         #pars2,model2,mpars2 = psf.fit(image,[amp,x0,y0],nosky=False,niter=5,allpars=True)
            #         #import pdb; pdb.set_trace()
                        
    #             # Only fit amp if niter>1
    #             #   do it empirically
    #             else:
    #                 #im1 = psf(pars=[1.0,xcen,ycen],bbox=bbox)
    #                 #wt = 1/image.error**2
    #                 #amp = np.median(image.data[mask]/im1[mask])                
    #                 model1 = psf(x,y,pars=[1.0,xcen,ycen])
    #                 wt = 1/err**2
    #                 amp = np.median(flux/model1)
    #                 #amp = np.median(wt*flux/model1)/np.median(wt)

    #                 self.staramp[i] = amp
    #                 model = model1*amp
    #                 #self.starxcen[i] = pars2[1]+xy[0][0]
    #                 #self.starycen[i] = pars2[2]+xy[1][0]       
    #                 #print(count,self.starxcen[i],self.starycen[i])
    #                 # updating the X/Y values after the first iteration
    #                 #  causes problems.  bounces around too much

    #                 if verbose:
    #                     print('Star '+str(i)+' Refitting amp empirically')
    #                     print(str(amp))
                        
    #                 #if i==1: print(amp)
    #                 #if self.niter==2:
    #                 #    import pdb; pdb.set_trace()

    #         # No refit of stellar parameters
    #         else:
    #             model = psf(x,y,pars=[amp,xcen,ycen])

    #         #if self.niter>1:
    #         #    import pdb; pdb.set_trace()
                
    #         # Relculate reduced chi squared
    #         chisq = np.sum((flux-model.ravel())**2/err**2)/npix
    #         self.starchisq[i] = chisq
    #         # chi value, RMS of the residuals as a fraction of the amp
    #         rms = np.sqrt(np.mean(((flux-model.ravel())/self.staramp[i])**2))
    #         self.starrms[i] = rms
            
    #         #model = psf(x,y,pars=[amp,xcen,ycen])
    #         # Zero-out anything beyond the fitting radius
    #         #im[mask] = 0.0
    #         #npix = im.size
    #         #npix = len(x)
    #         allim[pixcnt:pixcnt+npix] = model.flatten()
    #         pixcnt += npix

    #         #import pdb; pdb.set_trace()
            
    #     self.niter += 1
            
    #    return allim


    def fitstars(self,x,psfparams):
        """ Fit amp/xcen/ycen for all the stars using the current PSF."""

        # Loop over the stars and generate the model image
        lookup = np.zeros((1,1,1),float)
        allmodel = np.zeros(self.star_imdata.shape,float)
        maxpix = self.star_imdata.shape[1]
        npix = int(np.sqrt(maxpix))
        for i in range(self.nstars):
            im1,err1,xdata1,ydata1,bbox1,n1 = self.unpackfitstar(i)
            pars = np.zeros(3,float)
            pars[0] = self.staramp[i]
            pars[1] = self.starxcen[i]
            pars[2] = self.starycen[i]
            pout = mnb.psffit(im1,err1,xdata1,ydata1,pars,self.psftype,psfparams,
                              self.lookup,self.imshape,self.verbose)
            bestpar,perror,cov,flux,fluxerr,chisq = pout
            rchisq = chisq/len(xdata1)
            model,der = mnb.psf(xdata1,ydata1,pars,self.psftype,psfparams,
                                self.lookup,self.imshape)
            rms = np.sqrt(np.mean(((im1-model)/bestpar[0])**2))
            print(i,bestpar,rchisq,rms)
            
            # Update stellar parameters
            self.staramp[i] = bestpar[0]
            self.starxcen[i] = bestpar[1]
            self.starycen[i] = bestpar[2]
            self.starchisq[i] = rchisq
            self.starrms[i] = rms
            
    
    def jac(self,x,args,refit=True):
        """ jacobian."""
        # input the model parameters        
        if self.verbose:
            print('model: '+str(self.niter)+' ',args)
        # Loop over the stars and generate the model image
        nparsarr = np.array([6,7,8,8,7])
        npars = nparsarr[self.psftype-1]
        allmodel = np.zeros(self.star_imdata.shape,float)
        maxpix = self.star_imdata.shape[1]
        allderiv = np.zeros((self.nstars,maxpix,npars),float)
        for i in range(self.nstars):
            im1,err1,xdata1,ydata1,bbox1,n1 = self.unpackfitstar(i)
            allpars = np.zeros(npars,float)
            allpars[0] = self.staramp[i]
            allpars[1] = self.starxcen[i]
            allpars[2] = self.starycen[i]
            allpars[3:] = args
            g,d = mnb.amodel2d(xdata1,ydata1,self.psftype,allpars,npars)
            print(len(g))
            print(d.shape)
            allmodel[i,:len(g)] = g
            allderiv[i,:len(g),:] = d
        # Trim off the stellar parameter (amp/xc/yc) derivatives
        allderiv = allderiv[:,:,3:]
        return allmodel,allderiv



        # if self.verbose:
        #     print('jac: '+str(self.niter)+' ',args)
        
        # psf = self.psf.copy()
        # psf._params = list(args)
    
        # # Loop over the stars and generate the derivatives
        # #-------------------------------------------------

        # # Initalize output arrays
        # allderiv = np.zeros((self.ntotpix,len(psf.params)),float)
        # if retmodel:
        #     allim = np.zeros(self.ntotpix,float)
        # pixcnt = 0

        # # Need to run model() to calculate amp/xcen/ycen for first couple iterations
        # #if self.niter<=1 and refit:
        # #    dum = self.model(x,*args,refit=refit)
        # dum = self.model(x,*args,refit=True) #,verbose=True)            
            
        # for i in range(self.nstars):
        #     amp = self.staramp[i]
        #     xcen = self.starxcen[i]            
        #     ycen = self.starycen[i]
        #     bbox = self.bboxdata[i]
        #     x = self.xlist[i]
        #     y = self.ylist[i]
        #     pixstart = self.pixstart[i]
        #     npix = self.npix[i]
        #     flux = self.imflatten[pixstart:pixstart+npix]
        #     err = self.errflatten[pixstart:pixstart+npix]
        #     xdata = np.vstack((x,y))
            
        #     # Get the model and derivative
        #     allpars = np.concatenate((np.array([amp,xcen,ycen]),np.array(args)))
        #     m,deriv = psf.jac(xdata,*allpars,allpars=True,retmodel=True)
        #     #if retmodel:
        #     #    m,deriv = psf.jac(xdata,*allpars,allpars=True,retmodel=True)
        #     #else:
        #     #    deriv = psf.jac(xdata,*allpars,allpars=True)                
        #     deriv = np.delete(deriv,[0,1,2],axis=1)  # remove stellar ht/xc/yc columns

        #     # Solve for the best amp, and then scale the derivatives (all scale with amp)
        #     #if self.niter>1 and refit:
        #     #    newamp = amp*np.median(flux/m)
        #     #    self.staramp[i] = newamp
        #     #    m *= (newamp/amp)
        #     #    deriv *= (newamp/amp)

        #     #if i==1: print(amp,newamp)
        #     #import pdb; pdb.set_trace()

        #     npix,dum = deriv.shape
        #     allderiv[pixcnt:pixcnt+npix,:] = deriv
        #     if retmodel:
        #         allim[pixcnt:pixcnt+npix] = m
        #     pixcnt += npix
            
        # return allim,allderiv

    def linesearch(self,bestpar,dbeta):
        # Perform line search along search gradient

        # Inside model() the parameters are limited to the PSF bounds()
        f0 = self.chisq(bestpar)
        f1 = self.chisq(bestpar+0.5*dbeta)
        f2 = self.chisq(bestpar+dbeta)
        print(f0,f1,f2)
        alpha = quadratic_bisector(np.array([0.0,0.5,1.0]),np.array([f0,f1,f2]))
        alpha = np.minimum(np.maximum(alpha,0.0),1.0)  # 0<alpha<1
        if np.isfinite(alpha)==False:
            alpha = 1.0

        pars_new = bestpar + alpha * dbeta
        new_dbeta = alpha * dbeta
        return alpha,new_dbeta
    
    # def mklookup(self,order=0):
    #     """ Make an empirical look-up table for the residuals."""

    #     # Make the empirical EPSF
    #     cube = self.psf.resid(self.tab,self.image,fillvalue=np.nan)
    #     coords = (self.tab['x'].data,self.tab['y'].data)
    #     epsf,nbadstar,rms = mkempirical(cube,order=order,coords=coords,shape=self.image.shape,lookup=True)
    #     lookup = models.PSFEmpirical(epsf,imshape=self.image.shape,order=order,lookup=True)

    #     # DAOPHOT does some extra analysis to make sure the flux
    #     # in the residual component is okay

    #     # -make sure
    #     #  -to take the total flux into account (not varying across image)
    #     #  -make sure the amp=1 at center
    #     #  -make sure all PSF values are >=0
                             
    #     # Add the lookup table to the PSF model
    #     self.psf.lookup = lookup

    #     #import pdb; pdb.set_trace()
        
        
    # def starmodel(self,star=None,pars=None):
    #     """ Generate 2D star model images that can be compared to the original cutouts.
    #          if star=None, then it will return all of them as a list."""

    #     psf = self.psf.copy()
    #     if pars is not None:
    #         psf._params = pars
        
    #     model = []
    #     if star is None:
    #         star = np.arange(self.nstars)
    #     else:
    #         star = [star]

    #     for i in star:
    #         image = self.imdata[i]
    #         amp = self.staramp[i]
    #         xcen = self.starxcen[i]   
    #         ycen = self.starycen[i]
    #         bbox = self.bboxdata[i]
    #         model1 = psf(pars=[amp,xcen,ycen],bbox=bbox)
    #         model.append(model1)
    #     return model



#@njit
def fitpsf(psftype,psfparams,image,error,tab,fitradius,method='qr',maxiter=10,
           minpercdiff=1.0,verbose=False):
    """
    Fit PSF model to stars in an image.

    Parameters
    ----------
    psftype : PSF object
       PSF object with initial parameters to use.
    psfparams : numpy array
       PSF object with initial parameters to use.
    image : numpy array
       Image to use to fit PSF model to stars.
    error : numpy array
       Uncertainties in image.
    tab : numpy structured array
       Table with initial amp/x/y values for the stars to use to fit the PSF.
    fitradius : float, table
       The fitting radius.  If none is input then the initial PSF FWHM will be used.
    method : str, optional
       Method to use for solving the non-linear least squares problem: "qr",
       "svd", "cholesky", and "curve_fit".  Default is "qr".
    maxiter : int, optional
       Maximum number of iterations to allow.  Only for methods "qr", "svd", and "cholesky".
       Default is 10.
    minpercdiff : float, optional
       Minimum percent change in the parameters to allow until the solution is
       considered converged and the iteration loop is stopped.  Only for methods
       "qr" and "svd".  Default is 1.0.
    verbose : boolean, optional
       Verbose output.

    Returns
    -------
    pars : numpy array
       Array of best-fit model parameters
    perror : numpy array
       Uncertainties in "pars".
    pcov : numpy array
       Covariances of the best-ft parameters.
    psftab : table
       Table of best-fitting amp/xcen/ycen values for the PSF stars.
    rchisq : float
       Reduced chi-squared of the best-fit.
    pf : PSFFitter object
       PSFFitter object.
    
    Example
    -------

    pars,perror,pcov,psftab,rchisq,pf = fitpsf(psftype,psfparams,image,error,tab,fitradius)

    """

    t0 = time.time()
    #print = utils.getprintfunc() # Get print function to be used locally, allows for easy logging   

    # Initialize the output catalog best-fitting values for the PSF stars
    dt = np.dtype([('id',int),('amp',float),('x',float),('y',float),('npix',int),('rms',float),
                   ('chisq',float),('ixmin',int),('ixmax',int),('iymin',int),('iymax',int)])
    psftab = np.zeros(len(tab),dtype=dt)

    # Fitting the PSF to the stars
    #-----------------------------

    # # Empirical PSF - done differently
    # if psf.psftype==6:
    #     cube1 = starcube(tab,image,npix=psf.npix,fillvalue=np.nan)
    #     coords = (tab['x'].data,tab['y'].data)
    #     epsf1,nbadstar1,rms1 = mkempirical(cube1,order=psf.order,coords=coords,shape=psf._shape)
    #     initpsf = mnb.PSFEmpirical(epsf1,imshape=image.shape,order=psf.order)
    #     pf = PSFFitter(initpsf,image,tab,fitradius=fitradius,verbose=False)
    #     # Fit the amp, xcen, ycen properly
    #     xdata = np.arange(pf.ntotpix)
    #     out = pf.model(xdata,[])
    #     # Put information into the psftab table
    #     psftab['amp'] = pf.staramp
    #     psftab['x'] = pf.starxcen
    #     psftab['y'] = pf.starycen
    #     psftab['chisq'] = pf.starchisq
    #     psftab['rms'] = pf.starrms
    #     psftab['npix'] = pf.starnpix    
    #     for i in range(len(tab)):
    #         bbox = pf.bboxdata[i]
    #         psftab['ixmin'][i] = bbox.ixmin
    #         psftab['ixmax'][i] = bbox.ixmax
    #         psftab['iymin'][i] = bbox.iymin
    #         psftab['iymax'][i] = bbox.iymax        
    #     psftab = Table(psftab)
    #     # Remake the empirical EPSF    
    #     cube = starcube(psftab,image,npix=psf.npix,fillvalue=np.nan)
    #     epsf,nbadstar,rms = mkempirical(cube,order=psf.order,coords=coords,shape=psf._shape)
    #     newpsf = mnb.PSFEmpirical(epsf,imshape=image.shape,order=psf.order)
    #     if verbose:
    #         print('Median RMS: '+str(np.median(pf.starrms)))
    #         print('dt = %.2f sec' % (time.time()-t0))
    #     return newpsf, None, None, psftab, pf

    starx = tab['x']
    stary = tab['y']
    starflux = tab['flux']
    pf = PSFFitter(psftype,psfparams,image,error,starx,stary,starflux,fitradius,verbose=False)
    initpar = psfparams.copy()
    #method = str(method).lower()
    xdata = np.arange(10)  # dummy

    # Iterate
    niter = 0
    percdiff = 1e10
    bestpar = initpar.copy()
    
    dchisq = -1
    oldchisq = 1e30
    bounds = mnb.model2d_bounds(psftype)
    bounds = bounds[3:,:]
    while (niter<maxiter and percdiff>minpercdiff and dchisq<0):
        # Fit the stellar parameters first
        pf.fitstars(xdata,bestpar)
        # Get the Jacobian and model
        m,jac = pf.jac(xdata,bestpar)
        chisq = np.sum((pf.starfit_imdata-m)**2/pf.starfit_errdata**2)
        dy = (pf.starfit_imdata-m).ravel()
        # Weights
        wt = 1/pf.starfit_errdata**2
        wt = wt.ravel()
        jac = jac.reshape((jac.shape[0]*jac.shape[1],jac.shape[2]))
        m = m.ravel()
        # Solve Jacobian
        dbeta = mnb.qr_jac_solve(jac,dy,weight=wt)

        # Perform line search
        alpha,new_dbeta = pf.linesearch(bestpar,dbeta)
            
        if verbose:
            print('  pars = '+str(bestpar))
            print('  dbeta = '+str(dbeta))

        # Update parameters
        oldpar = bestpar.copy()
        # limit the steps to the maximum step sizes and boundaries
        maxsteps = mnb.model2d_maxsteps(psftype,bestpar)
        maxsteps = maxsteps[3:]
        bestpar = mnb.newlsqpars(bestpar,new_dbeta,bounds,maxsteps)
        
        # Update the parameters 
        diff = np.abs(bestpar-oldpar)
        denom = np.abs(oldpar.copy())
        bad, = np.where(denom==0)
        if len(bad)>0:
            denom[bad] = 1.0  # deal with zeros
        percdiff = np.max(diff/denom*100)
        dchisq = chisq-oldchisq
        percdiffchisq = dchisq/oldchisq*100
        oldchisq = chisq
        niter += 1
            
        if verbose:
            print('  '+str(niter+1)+' ',bestpar,' '+str(percdiff)+' '+str(chisq))

    # Fit the stellar parameters first
    pf.fitstars(xdata,bestpar)
    # Get the Jacobian and model
    m,jac = pf.jac(xdata,bestpar)
    chisq = np.sum((pf.starfit_imdata-m)**2/pf.starfit_errdata**2)
    dy = (pf.starfit_imdata-m).ravel()
    # Weights
    wt = 1/pf.starfit_errdata**2
    wt = wt.ravel()
    jac = jac.reshape((jac.shape[0]*jac.shape[1],jac.shape[2]))
    rchisq = chisq/np.sum(pf.starfit_ndata)
    
    # Estimate uncertainties
    # Calculate covariance matrix
    pcov = mnb.jac_covariance(jac,dy,wt=wt)
    perror = np.sqrt(np.diag(pcov))

    pars = bestpar
    if verbose:
        print('Best-fitting parameters: '+str(pars))
        print('Errors: '+str(perror))
        print('Median RMS: '+str(np.median(pf.starrms)))

    # Output best-fitting values for the PSF stars as well
    dt = np.dtype([('id',int),('amp',float),('x',float),('y',float),('npix',int),('rms',float),
                   ('chisq',float),('ixmin',int),('ixmax',int),('iymin',int),('iymax',int)])
    psftab = np.zeros(len(tab),dtype=dt)
    psftab['id'] = np.arange(len(tab))+1
    psftab['amp'] = pf.staramp
    psftab['x'] = pf.starxcen
    psftab['y'] = pf.starycen
    psftab['chisq'] = pf.starchisq
    psftab['rms'] = pf.starrms
    psftab['npix'] = pf.starfit_ndata
    for i in range(len(tab)):
        bbox = pf.star_bbox[i,:]
        psftab['ixmin'][i] = bbox[0]
        psftab['ixmax'][i] = bbox[1]
        psftab['iymin'][i] = bbox[2]
        psftab['iymax'][i] = bbox[3]
    
    if verbose:
        print('dt = %.2f sec' % (time.time()-t0))
    
    return pars, perror, pcov, psftab, rchisq, pf


#@njit
def getpsf(psf,image,tab,fitradius=None,lookup=False,lorder=0,method='qr',subnei=False,
           alltab=None,maxiter=10,minpercdiff=1.0,reject=False,maxrejiter=3,verbose=False):
    """
    Fit PSF model to stars in an image with outlier rejection of badly-fit stars.

    Parameters
    ----------
    psf : PSF object
       PSF object with initial parameters to use.
    image : CCDData object
       Image to use to fit PSF model to stars.
    tab : table
       Catalog with initial amp/x/y values for the stars to use to fit the PSF.
    fitradius : float, table
       The fitting radius.  If none is input then the initial PSF FWHM will be used.
    lookup : boolean, optional
       Use an empirical lookup table.  Default is False.
    lorder : int, optional
       The order of the spatial variations (0=constant, 1=linear).  Default is 0.
    method : str, optional
       Method to use for solving the non-linear least squares problem: "qr",
       "svd", "cholesky", and "curve_fit".  Default is "qr".
    subnei : boolean, optional
       Subtract stars neighboring the PSF stars.  Default is False.
    alltab : table, optional
       Catalog of all objects in the image.  This is needed for bad PSF star
       rejection.
    maxiter : int, optional
       Maximum number of iterations to allow.  Only for methods "qr", "svd", and "cholesky".
       Default is 10.
    minpercdiff : float, optional
       Minimum percent change in the parameters to allow until the solution is
       considered converged and the iteration loop is stopped.  Only for methods
       "qr" and "svd".  Default is 1.0.
    reject : boolean, optional
       Reject PSF stars with high RMS values.  Default is False.
    maxrejiter : int, boolean
       Maximum number of PSF star rejection iterations.  Default is 3.
    verbose : boolean, optional
       Verbose output.

    Returns
    -------
    newpsf : PSF object
       New PSF object with the best-fit model parameters.
    pars : numpy array
       Array of best-fit model parameters
    perror : numpy array
       Uncertainties in "pars".
    psftab : table
       Table of best-fitting amp/xcen/ycen values for the PSF stars.

    Example
    -------

    newpsf,pars,perror,psftab = getpsf(psf,image,tab)

    """

    t0 = time.time()
    print = utils.getprintfunc() # Get print function to be used locally, allows for easy logging   

    psftype,psfpars,_,_ = mnb.unpackpsf(psf)
    
    # Fitting radius
    if fitradius is None:
        tpars = np.zeros(len(psfpars)+3,float)
        tpars[0] = 1.0
        tpars[3:] = psfpars
        if psftype == 3:  # Penny
            fitradius = mnb.penny2d_fwhm(tpars)*1.5
        else:
            fitradius = mnb.model2d_fwhm(psftype,tpars)
        
    # subnei but no alltab input
    if subnei and alltab is None:
        raise ValueError('alltab is needed for PSF neighbor star subtraction')
        
    if 'id' not in tab.dtype.names:
        tab['id'] = np.arange(len(tab))+1
    psftab = tab.copy()

    # Initializing output PSF star catalog
    dt = np.dtype([('id',int),('amp',float),('x',float),('y',float),('npix',int),
                   ('rms',float),('chisq',float),('ixmin',int),('ixmax',int),
                   ('iymin',int),('iymax',int),('reject',int)])
    outtab = np.zeros(len(tab),dtype=dt)
    outtab = Table(outtab)
    for n in ['id','x','y']:
        outtab[n] = tab[n]
    
    # Remove stars that are too close to the edge
    ny,nx = image.shape
    bd = ((psftab['x']<fitradius) | (psftab['x']>(nx-1-fitradius)) |
          (psftab['y']<fitradius) | (psftab['y']>(ny-1-fitradius)))
    nbd = np.sum(bd)
    if nbd > 0:
        if verbose:
            print('Removing '+str(nbd)+' stars near the edge')
        psftab = psftab[~bd]

    # Generate an empirical image of the stars
    # and fit a model to it to get initial estimates
    if psftype != 6:
        cube = starcube(psftab,image,npix=psf.npix,fillvalue=np.nan)
        epsf,nbadstar,rms = mkempirical(cube,order=0)
        #epsfim = CCDData(epsf,error=epsf.copy()*0+1,mask=~np.isfinite(epsf))
        epsfim = epsf.copy()
        epsferr = np.ones(epsf.shape,float)
        ny,nx = epsf.shape
        xx,yy = np.meshgrid(np.arange(nx),np.arange(ny))
        out = model2dfit(epsfim,epsferr,xx,yy,psftype,1.0,nx//2,ny//2,verbose=False)
        pars,perror,cov,flux,fluxerr,chisq = out
        mparams = pars[3:]  # model parameters
        #pars,perror,mparams = mnb.model2d_fit(epsfim,pars=[1.0,psf.npix/2,psf.npix//2])
        initpar = mparams.copy()
        curpsf = mnb.packpsf(psftype,mparams,0,0)
        #curpsf = psf.copy()
        #curpsf.params = initpar
        if verbose:
            print('Initial estimate from empirical PSF fit = '+str(mparams))
    else:
        curpsf = psf.copy()
        _,initpar,_,_ = mnb.unpackpsf(psf)
        #initpar = psf.params.copy()

    # Outlier rejection iterations
    nrejiter = 0
    flag = 0
    nrejstar = 100
    fitrad = fitradius
    useimage = image.copy()
    while (flag==0):
        if verbose:
            print('--- Iteration '+str(nrejiter+1)+' ---')                

        # Update the fitting radius
        if nrejiter>0:
            fitrad = curpsf.fwhm()
        if verbose:
            print('  Fitting radius = %5.3f' % (fitrad))
                    
        # Reject outliers
        if reject and nrejiter>0:
            medrms = np.median(ptab['rms'])
            sigrms = dln.mad(ptab['rms'].data)
            gd, = np.where(ptab['rms'] < medrms+3*sigrms)
            nrejstar = len(psftab)-len(gd)
            if verbose:
                print('  RMS = %6.4f +/- %6.4f' % (medrms,sigrms))
                print('  Threshold RMS = '+str(medrms+3*sigrms))
                print('  Rejecting '+str(nrejstar)+' stars')
            if nrejstar>0:
                psftab = psftab[gd]

        # Subtract neighbors
        if nrejiter>0 and subnei:
            if verbose:
                print('Subtracting neighbors')
                # Find the neighbors in alltab
                # Fit the neighbors and PSF stars
                # Subtract neighbors from the image
                useimage = image.copy()  # start with original image
                useimage = subtractnei(useimage,alltab,tab,curpsf)
                
        # Fitting the PSF to the stars
        #-----------------------------
        newpsf,pars,perror,ptab,pf = fitpsf(curpsf,useimage,psftab,fitradius=fitrad,method=method,
                                            maxiter=maxiter,minpercdiff=minpercdiff,verbose=verbose)
        
        # Add information into the output catalog
        ind1,ind2 = dln.match(outtab['id'],ptab['id'])
        outtab['reject'] = 1
        for n in ptab.columns:
            outtab[n][ind1] = ptab[n][ind2]
        outtab['reject'][ind1] = 0

        # Compare PSF parameters
        if type(newpsf)!=mnb.PSFEmpirical:
            pardiff = newpsf.params-curpsf.params
        else:
            pardiff = newpsf._data-curpsf._data
        sumpardiff = np.sum(np.abs(pardiff))
        curpsf = newpsf.copy()
        
        # Stopping criteria
        if reject is False or sumpardiff<0.05 or nrejiter>=maxrejiter or nrejstar==0: flag=1
        if subnei is True and nrejiter==0: flag=0   # iterate at least once with neighbor subtraction
        
        nrejiter += 1
        
    # Generate an empirical look-up table of corrections
    if lookup:
        if verbose:
            print('Making empirical lookup table with order='+str(lorder))

        pf.mklookup(lorder)
        # Fit the stars again and get new RMS values
        xdata = np.arange(pf.ntotpix)
        out = pf.model(xdata,*pf.psf.params)
        newpsf = pf.psf.copy()
        # Update information in the output catalog
        ind1,ind2 = dln.match(outtab['id'],ptab['id'])
        outtab['reject'] = 1
        outtab['reject'][ind1] = 0
        outtab['amp'][ind1] = pf.staramp[ind2]
        outtab['x'][ind1] = pf.starxcen[ind2]
        outtab['y'][ind1] = pf.starycen[ind2]
        outtab['rms'][ind1] = pf.starrms[ind2]
        outtab['chisq'][ind1] = pf.starchisq[ind2]                
        if verbose:
            print('Median RMS: '+str(np.median(pf.starrms)))            
            
    if verbose:
        print('dt = %.2f sec' % (time.time()-t0))
    
    return newpsf, pars, perror, outtab
        
        
