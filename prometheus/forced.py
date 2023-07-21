import os
import errno
import numpy as np
from dlnpyutils import utils as dln, robust, coords
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from astropy.time import Time
from astropy.table import Table,vstack,hstack
import astropy.units as u
from . import groupfit,allfit,models,leastsquares as lsq
from .ccddata import CCDData

# ALLFRAME-like forced photometry

# also see multifit.py

def makemastertab(images,dcr=1.0,mindet=2):
    """
    Make master star list from individual image star catalogs.

    Parameters
    ----------
    images : list
       List of dictionaries containing the WCS and catalog
        information.
    dcr : float, optional
       Cross-matching radius in arcsec.  Default is 1.0 arcsec.
    mindet : int, optional
       Minimum number of detections to be counted in master list.
        Default is 2.

    Returns
    -------
    mastertab : table
       Table of unique sources in the images.

    Example
    -------

    mastertab = mkmastertab(images)

    """

    nimages = len(images)

    objdt = [('objid',int),('ra',float),('dec',float),('amp',float),('flux',float),('nmeas',int)]
    
    # Loop over the images
    for i in range(nimages):
        tab1 = images[i]['table']
        tab1['objid'] = -1
        tab1['ra'].unit = None   # no units
        tab1['dec'].unit = None        
        # First catalog
        if i==0:
            tab1['objid'] = np.arange(len(tab1))+1
            meas = tab1.copy()
            obj = Table(np.zeros(len(meas),dtype=np.dtype(objdt)))
            for c in ['ra','dec']: obj[c] = meas[c]
            obj['nmeas'] = 1
        # 2nd and later catalog, need to crossmatch
        else:
            # Cross-match
            ind1,ind2,dist = coords.xmatch(obj['ra'],obj['dec'],
                                           tab1['ra'],tab1['dec'],dcr,unique=True)
            # Some matches
            if len(ind1)>0:
                tab1['objid'][ind2] = obj['objid'][ind1]
                obj['nmeas'][ind1] += 1
            # Some left, add them to object table
            if len(ind1) < len(tab1):
                leftind = np.arange(len(tab1))
                leftind = np.delete(leftind,ind2)
                tab1['objid'][leftind] = np.arange(len(leftind))+len(obj)+1
                meas = vstack((meas,tab1))
                newobj = Table(np.zeros(len(leftind),dtype=np.dtype(objdt)))
                for c in ['ra','dec']: newobj[c] = tab1[leftind][c]
                newobj['nmeas'] = 1
                obj = vstack((obj,newobj))

    # Get mean ra, dec and flux from the measurements
    measindex = dln.create_index(meas['objid'])
    obj['flux'] = 0.0
    for i in range(len(measindex['value'])):
        objid = measindex['value'][i]
        ind = measindex['index'][measindex['lo'][i]:measindex['hi'][i]+1]
        nind = len(ind)
        obj['ra'][objid-1] = np.mean(meas['ra'][ind])
        obj['dec'][objid-1] = np.mean(meas['dec'][ind])
        obj['amp'][objid-1] = np.mean(meas['psfamp'][ind])
        obj['flux'][objid-1] = np.mean(meas['psfflux'][ind])

    # Impose minimum number of detections
    if mindet is not None:
        gdobj, = np.where(obj['nmeas'] >= mindet)
        if len(gdobj)==0:
            print('No objects passed the minimum number of detections threshold of '+str(mindet))
            return []
        obj = obj[gdobj]

    return obj
            
def solveone(psf,im,cat,method='qr',bounds=None,fitradius=None,absolute=False):

    method = str(method).lower()
  
    # Image offset for absolute X/Y coordinates
    if absolute:
        imx0 = im.bbox.xrange[0]
        imy0 = im.bbox.yrange[0]

    xc = cat['x']
    yc = cat['y']
    if absolute:  # offset
        xc -= imx0
        yc -= imy0
        if bounds is not None:
            bounds[0][1] -= imx0  # lower
            bounds[0][2] -= imy0
            bounds[1][1] -= imx0  # upper
            bounds[1][2] -= imy0
    if fitradius is None:
        fitradius = np.maximum(psf.fwhm(),1)
    bbox = psf.starbbox((xc,yc),im.shape,fitradius)
    X,Y = psf.bbox2xy(bbox)

    # Get subimage of pixels to fit
    # xc/yc might be offset
    flux = im.data[bbox.slices]
    err = im.error[bbox.slices]
    wt = 1.0/np.maximum(err,1)**2  # weights
    skyim = im.sky[bbox.slices]
    xc -= bbox.ixmin  # offset for the subimage
    yc -= bbox.iymin
    X -= bbox.ixmin
    Y -= bbox.iymin
    if bounds is not None:
        bounds[0][1] -= bbox.ixmin  # lower
        bounds[0][2] -= bbox.iymin
        bounds[1][1] -= bbox.ixmin  # upper
        bounds[1][2] -= bbox.iymin            
    xdata = np.vstack((X.ravel(), Y.ravel()))        
    #sky = np.median(skyim)
    sky = 0.0
    if 'amp' in cat:
        amp = cat['amp']
    else:
        #amp = flux[int(np.round(yc)),int(np.round(xc))]-sky   # python images are (Y,X)
        #amp = np.maximum(amp,1)  # make sure it's not negative
        amp = 1.0
        
    initpar = [amp,xc,yc,sky]
 
    # Use Cholesky, QR or SVD to solve linear system of equations
    m,jac = psf.jac(xdata,*initpar,retmodel=True)
    dy = flux.ravel()-m.ravel()
    # Solve Jacobian
    dbeta = lsq.jac_solve(jac,dy,method=method,weight=wt.ravel())
    dbeta[~np.isfinite(dbeta)] = 0.0  # deal with NaNs
    chisq = np.sum(dy**2 * wt.ravel())/len(dy)

    # Output values
    newamp = np.maximum(amp+dbeta[0], 0)  # amp cannot be negative
    dx = dbeta[1]
    dy = dbeta[2]

    return newamp,dx,dy


def solve(psf,resid,tab,fitradius=None,verbose=False):
    """
    Solve for the flux and find corrections for x and y.

    Parameters
    ----------
    psf : psf model
       The image PSF model.
    resid : image
       Residual image with initial estimate of star models subtracted.
    tab : table
       Table of stars ot fit.
    fitradius : float, optional
       The fitting radius in pixels.  The default is 0.5*psf.fwhm().
    verbose : bool, optional
       Verbose output to the screen.  Default is False.

    Returns
    -------
    out : table
       Table of results

    Example
    -------

    out = solve(psf,resid,meastab)

    """

    if fitradius is None:
        fitradius = 0.5*psf.fwhm()
    
    ntab = len(tab)
    out = tab.copy()
    
    # Loop over the stars
    for i in range(ntab):
        
        # Add the previous best-fit model back in to the image
        if tab['amp'][i] > 0:
            # nocopy=True will change in place
            _ = psf.add(resid.data,tab[i:i+1],nocopy=True)

        # Solve single flux
        newamp,dx,dy = solveone(psf,resid,tab[i:i+1],fitradius=fitradius)

        # the dx/dy numbers are CRAZY LARGE!!!
        
        # Save the results
        out['amp'][i] = newamp
        out['dx'][i] = dx
        out['dy'][i] = dy 
        
        # Immediately subtract the new model
        #  npcopy=True will change in place
        _ = psf.sub(resid.data,out[i:i+1],nocopy=True)

        #print(i,newamp,dx,dy)
        
    return out,resid
        

def forced(files,mastertab=None,fitpm=False,reftime=None,refwcs=None,verbose=True):
    """
    ALLFRAME-like forced photometry.

    Parameters
    ----------
    files : list
       List of image filenames.
    mastertab : table
       Master table of objects.
    fitpm : boolean, optional
       Fit proper motions as well as central positions.
         Default is False.
    reftime : Time object, optional
       The reference time to use.  By default, the mean
         JD of all images is used.

    Returns
    -------
    obj : table
       Table of unique objects and their mean coordinates and
         proper motions.
    meas : table
       Table of individiual measurements.

    Example
    -------

    obj,meas = forced(files)

    """

    # fit proper motions as well
    # don't load all the data at once, only what you need
    #   maybe use memory maps

    # The default behavior of fits.open() is to use memmap=True
    # and only load the data into physical memory when it is accessed
    # e.g. hdu[0].data.
    # after closing the hdu you still need to delete the data i.e.
    # del hdu[0].data, because there's still a memory map open.

    # I created a function in utils.refresh_mmap() that you can give
    # an open HDUList() and it will refresh the mmap and free up the
    # virtual memory.
    
    nfiles = len(files)
    print('Running forced photometry on {:d} images'.format(nfiles))


    # Do NOT load all of the images at once, only the headers and WCS objects
    # load the PSF and object catalog from the _prometheus.fits file
    # run prometheus if the _prometheus.fits file does not exist
    
    # Load images headers and WCS
    print('Loading image headers, WCS, and catalogs')
    images = []
    for i in range(nfiles):
        print('Image {:d}  {:s}'.format(i+1,files[i]))
        if os.path.exists(files[i])==False:
            print(files[i],' not found')
            continue
        prfile = files[i].replace('.fits','_prometheus.fits')            
        if os.path.exists(prfile)==False:
            print(prfile,' not found')
            continue
        # Load header
        head1 = fits.getheader(files[i],0)
        wcs1 = WCS(head1)
        # Load soure table and PSF model from prometheus output file
        tab1 = Table.read(prfile,1)
        for c in tab1.colnames: tab1[c].name = c.lower()
        psf1 = models.read(prfile,4)
        images.append({'file':files[i],'header':head1,'wcs':wcs1,
                       'table':tab1,'psf':psf1})
        #im1 = ccddata.CCDData.read(files[i])
    nimages = len(images)
    if nimages==0:
        print('No images to process')
        return
    
    # Load the master star table if necessary
    if mastertab is not None:
        if isinstance(mastertab,str):
            if os.path.exists(mastertab)==False:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), mastertab)
            mastertab_filename = mastertab
            mastertab = Table.read(mastertab_filename)
    # No master star table input, make one from the
    #  individual catalogs
    else:
        print('Creating master list')
        mastertab = makemastertab(images)
        
    # Make sure we have the necessary columns
    for c in mastertab.colnames: mastertab[c].name = c.lower()
    if 'ra' not in mastertab.colnames or 'dec' not in mastertab.colnames:
        raise ValueError('ra and dec columns must exist in mastertab')
    
    # Initialize the array of positions and proper motions
    nobj = len(mastertab)
    print('Master list has {:d} stars'.format(nobj))
    objtab = mastertab.copy()
    objtab['objid'] = 0
    objtab['objid'] = np.arange(nobj)+1
    objtab['cenra'] = objtab['ra'].copy()
    objtab['cendec'] = objtab['dec'].copy()
    objtab['cenpmra'] = 0.0
    objtab['cenpmdec'] = 0.0
    objtab['nmeas'] = 0
    objtab['converged'] = False
    
    # Initial master list coordinates
    coo0 = SkyCoord(ra=objtab['cenra']*u.deg,dec=objtab['cendec']*u.deg,frame='icrs')
    
    # Get information about all of the images
    dt = [('file',str,200),('residfile',str,200),('dateobs',str,26),('jd',float),
          ('cenra',float),('cendec',float),('nx',int),('ny',int),('vra',float,4),
          ('vdec',float,4),('exptime',float),('startmeas',int),('nmeas',int)]
    iminfo = Table(np.zeros(nimages,dtype=np.dtype(dt)))
    meascount = 0
    for i in range(nimages):
        iminfo['file'][i] = images[i]['file']
        iminfo['residfile'][i] = images[i]['file'].replace('.fits','_resid.npy')
        iminfo['dateobs'][i] = images[i]['header']['DATE-OBS']
        iminfo['jd'][i] = Time(iminfo['dateobs'][i]).jd
        nx = images[i]['header']['NAXIS1']
        ny = images[i]['header']['NAXIS2']
        iminfo['nx'][i] = nx
        iminfo['ny'][i] = ny
        cencoo = images[i]['wcs'].pixel_to_world(nx//2,ny//2)
        iminfo['cenra'][i] = cencoo.ra.deg
        iminfo['cendec'][i] = cencoo.dec.deg
        vra,vdec = images[i]['wcs'].wcs_pix2world([0,nx-1,nx-1,0],[0,0,ny-1,ny-1],0)
        iminfo['vra'][i] = vra
        iminfo['vdec'][i] = vdec
        isin = coo0.contained_by(images[i]['wcs'])
        iminfo['startmeas'][i] = meascount
        iminfo['nmeas'][i] = np.sum(isin)
        meascount += iminfo['nmeas'][i]
        print('Image {:d} - {:d} stars overlap'.format(i+1,np.sum(isin)))
        
    # Get the reference epoch, mean epoch
    refepoch = Time(np.mean(iminfo['jd']),format='jd')
    

    # Initialize the measurement table
    dt = [('objid',int),('objindex',int),('imindex',int),('jd',float),
          ('ra',float),('dec',float),('x',float),('y',float),('amp',float),
          ('flux',float),('fluxerr',float),('dflux',float),('dfluxerr',float),
          ('dx',float),('dxerr',float),('dy',float),('dyerr',float),('dra',float),
          ('ddec',float),('sky',float),('converged',bool)]
    nmeas = np.sum(iminfo['nmeas'])
    meastab = np.zeros(nmeas,dtype=np.dtype(dt))
    meascount = 0
    for i in range(nimages):
        contained = coo0.contained_by(images[i]['wcs'])
        isin, = np.where(contained)
        nisin = len(isin)
        objtab['nmeas'][isin] += 1 
        if nisin > 0:
            meastab['objid'][meascount:meascount+nisin] = objtab['objid'][isin]
            meastab['objindex'][meascount:meascount+nisin] = isin
            meastab['imindex'][meascount:meascount+nisin] = i
            meastab['jd'][meascount:meascount+nisin] = iminfo['jd'][i]
            meascount += nisin

    # Some stars have zero measurements
    zeromeas, = np.where(objtab['nmeas']==0)
    if len(zeromeas)>0:
        print('{:d} objects have zero measurements'.format(len(zeromeas)))
            
    # Create object index into measurement table
    oindex = dln.create_index(meastab['objindex'])
    objindex = nobj*[[]]  # initialize index with empty list for each object
    for i in range(len(oindex['value'])):
        ind = oindex['index'][oindex['lo'][i]:oindex['hi'][i]+1]
        objindex[oindex['value'][i]] = ind    # value is the index in objtab
    
    # Iterate until convergence has been reached
    count = 0
    flag = True
    while (flag):

        print('----- Iteration {:d} -----'.format(count+1))

        if count % 5 == 0:
            print('Recomputing and subtracting the sky')
        
        # Loop over the images:
        for i in range(nimages):
            wcs = images[i]['wcs']
            psf = images[i]['psf']
            residfile = iminfo['residfile'][i]
            imtime = Time(iminfo['jd'][i],format='jd')
            
            # Get the objects and measurements that overlap this image
            contained = coo0.contained_by(images[i]['wcs'])
            objtab1 = objtab[contained]
            msbeg = iminfo['startmeas'][i]
            msend = msbeg + iminfo['nmeas'][i]        
            meastab1 = meastab[msbeg:msend]
            print('Image {:d}  {:d} stars'.format(i+1,iminfo['nmeas'][i]))
            
            # Calculate x/y position for each object in this image
            # using the current best overall on-the-sky position
            # and proper motion
            # Need to convert celestial values to x/y position in
            # the image using the WCS.
            coo1 = SkyCoord(ra=objtab1['cenra']*u.deg,dec=objtab1['cendec']*u.deg,
                           pm_ra_cosdec=objtab1['cenpmra']*u.mas/u.year,
                           pm_dec=objtab1['cenpmdec']*u.mas/u.year,
                           obstime=refepoch,frame='icrs')
            
            # Use apply_space_motion() method to get coordinates for the
            #  epoch of this image
            newcoo1 = coo1.apply_space_motion(imtime)
            meastab1['ra'] = newcoo1.ra.deg
            meastab1['dec'] = newcoo1.dec.deg            
            
            # Now convert to image X/Y coordinates
            x,y = wcs.world_to_pixel(newcoo1)
            meastab1['x'] = x
            meastab1['y'] = y            

            # Initialize or load the residual image
            if count==0:
                im = CCDData.read(iminfo['file'][i])
                resid = im.copy()
            else:
                #resid_data = np.load(residfile)
                #resid.data = resid_data
                reid = dln.unpickle(residfile)
                
            # Subtract sky
            if count % 5 == 0:
                if hasattr(resid,'_sky'):
                    resid._sky = None  # force it to be recomputed
                resid.data -= resid.sky

            # Fit the fluxes while holding the positions fixed            
            out,resid = solve(psf,resid,meastab1,verbose=verbose)

            # Convert dx/dy to dra/ddec
            coo2 = wcs.pixel_to_world(meastab1['x']+out['dx'],meastab1['y']+out['dy'])
            dra = coo2.ra.deg - meastab1['ra']
            ddec = coo2.dec.deg - meastab1['dec']
            out['dra'] = dra    # in degrees
            out['ddec'] = ddec  # in degrees
            
            # Save the residual file
            dln.pickle(residfile,resid)
            
            # SOMETHING IS WRONG, I THINK THE PSF MIGHT NOT BE GOOD

            #from dlnpyutils import plotting as pl 
            #import pdb; pdb.set_trace()
            
            # Stuff the information back in
            meastab['amp'][msbeg:msend] = out['amp']
            meastab['ra'][msbeg:msend] = out['ra']
            meastab['dec'][msbeg:msend] = out['dec']
            meastab['dx'][msbeg:msend] = out['dx']
            meastab['dy'][msbeg:msend] = out['dy']
            meastab['dra'][msbeg:msend] = out['dra']
            meastab['ddec'][msbeg:msend] = out['ddec']            


            
            # allframe operates on the residual map, with the best-fit model subtracted
            
            # allframe derived flux and centroid corrections for each object
            # the flux corrections are applied immediately while the centroid
            # corrections are saved.

            # there are no groups in allframe
            # the least-squares design matrix is completely diagonalized: the
            # incremental brightness and position corrections are derived for
            # each star separately!  this may add a few more iterations for the
            # badly blended stars, but it does *not* affect the accuracy of the
            # final results.

            # Once a star has converged, it's best-fit model is subtracted from
            # the residual map and its parameters are fixed.
            
            # when no further infinitesimal change to a star's parameters
            # produces any reduction in the robust estimate of the mean-square
            # brightness residual inside that star's fitting region, the
            # maximum-likelihood solution has been achieved.
            
            # Calculate dx/dy residuals for each source
            # convert from pixel to work offset using image wcs

            # need to save the residual image and uncertainties
            # that's all we need to solve the least-squares problem.

            
        # Calculate new coordinates and proper motions based on the x/y residuals
        # the accumulated centroid corrections are projected through the individual
        # frames' geometric transformations to the coordinate system of the master list
        # and averaged.  These net corrections are applied to the stars' positions as
        # retained in the master list.

        # Can also make modest corrections to the input geometric transformation
        # equations by evaluating and removing systematic trends in the centroid
        # corrections derived for stars in each input image.
        
        # Loop over object and calculate coordinate and proper motion corrections
        for i in range(nobj):
            cosdec = np.cos(np.deg2rad(objtab['cendec'][i]))
            measind = objindex[i]
            meas1 = meastab[measind]
            jd0 = np.min(meas1['jd'])
            jd = meas1['jd']-jd0
            ra = meas1['ra']+meas1['dra']     # both in degrees
            dec = meas1['dec']+meas1['ddec']
            # Perform linear fit
            # SHOULD BE WEIGHTED!!!
            racoef = robust.linefit(jd,ra)
            deccoef = robust.linefit(jd,dec)
            # Get coordinate at the reference epoch
            refra = np.polyval(racoef,refepoch.jd-jd0)
            refdec = np.polyval(deccoef,refepoch.jd-jd0)
            # Calculate new proper motion
            pmra = racoef[0] * (3600*1e3)*365.2425      # convert slope from deg/day to mas/yr
            pmdec = deccoef[0] * (3600*1e3)*365.2425
            # update object cenra, cendec, cenpmra, cenpmdec
            objtab['cenra'][i] += refra
            objtab['cendec'][i] += refdec
            objtab['cenpmra'][i] += pmra * cosdec  # multiply by cos(dec)
            objtab['cenpmdec'][i] += pmdec 
            

        # Check for convergence
        count += 1
            
        import pdb; pdb.set_trace()

        
    # If there's filter information, then get average photometry
    # in each band for the objects

        
    return objtab,meastab
