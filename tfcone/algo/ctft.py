import tensorflow as tf
from tensorflow.python.framework import ops
import os
import math
import numpy as np
import tfcone.util.numerical as nm
import tfcone.util.types as t
import sys

_path = os.path.dirname(os.path.abspath(__file__))
_bp_module = tf.load_op_library( _path + '/../../lib/libbackproject.so' )
backproject = _bp_module.backproject
project = _bp_module.project


'''
    Compute the gradient of the backprojection op
    by invoking the forward projector.
'''
@ops.RegisterGradient( "Backproject" )
def _backproject_grad( op, grad ):
    proj = project(
            volume      = grad,
            geom        = op.get_attr( "geom" ),
            vol_shape   = op.get_attr( "vol_shape" ),
            vol_origin  = op.get_attr( "vol_origin" ),
            voxel_dimen = op.get_attr( "voxel_dimen" ),
            proj_shape  = op.get_attr( "proj_shape" )
        )
    return [ proj ]


'''
    Compute the gradient of the forward projection op
    by invoking the backprojector.
'''
@ops.RegisterGradient( "Project" )
def _project_grad( op, grad ):
    vol = backproject(
            proj        = grad,
            geom        = op.get_attr( "geom" ),
            vol_shape   = op.get_attr( "vol_shape" ),
            vol_origin  = op.get_attr( "vol_origin" ),
            voxel_dimen = op.get_attr( "voxel_dimen" ),
            proj_shape  = op.get_attr( "proj_shape" )
        )
    return [ vol ]


'''
    generate 1D-RamLak filter according to Kak & Slaney, chapter 3 equation 61

    TODO:   Does not work for example for pixel_width_mm = 0.5. Then we have
            a negative filter response.. Whats wrong here?

    Note: Conrad implements a slightly different variant, that's why results
    differ in the absolute voxel intensities
'''
def init_ramlak_1D( config ):
    #assert( config.ramlak_width % 2 == 1 )

    hw = config.ramlak_width // 2
    f = [
            -1 / math.pow( i * math.pi * config.pixel_shape.W, 2 ) if i%2 == 1 else 0
            for i in range( -hw, hw )
        ]
    f[hw] = 1/4 * math.pow( config.pixel_shape.W, 2 )

    return f


'''
    Generate 1D parker row-weights

    beta
        projection angle in [0, pi + 2*delta]
    delta
        overscan angle

'''
def init_parker_1D( config, beta, delta ):
    assert( beta + nm.eps >= 0 )

    w = np.ones( ( config.proj_shape.W ), dtype = np.float32 )

    for u in range( 0, config.proj_shape.W ):
        alpha = math.atan( ( u+0.5 - config.proj_shape.W/2 ) *
                config.pixel_shape.W / config.source_det_distance )

        if beta >= 0 and beta < 2 * (delta+alpha):
            # begin of scan
            w[u] = math.pow( math.sin( math.pi/4 * ( beta / (delta+alpha) ) ), 2 )
        elif beta >= math.pi + 2*alpha and beta < math.pi + 2*delta:
            # end of scan
            w[u] = math.pow( math.sin( math.pi/4 * ( ( math.pi + 2*delta - beta
                ) / ( delta - alpha ) ) ), 2 )
        elif beta >= math.pi + 2*delta:
            # out of range
            w[u] = 0.0

    return w


'''
    Generate 3D volume of parker weights

    U
        detector width

    returns
        numpy array of shape [#projections, 1, U]
'''
def init_parker_3D( config, primary_angles_rad ):
    pa = primary_angles_rad

    # normalize angles to [0, 2*pi]
    pa -= pa[0]
    pa = np.where( pa < 0, pa + 2*math.pi, pa )

    # find rotation such that max(angles) is minimal
    tmp = np.reshape( pa, ( pa.size, 1 ) ) - pa
    tmp = np.where( tmp < 0, tmp + 2*math.pi, tmp )
    pa = tmp[:, np.argmin( np.max( tmp, 0 ) )]

    # according to conrad implementation
    delta = math.atan( ( float(config.proj_shape.W * config.pixel_shape.W) / 2 )
            / config.source_det_distance )

    # go over projections
    w = [
            np.reshape(
                init_parker_1D( config, pa[i], delta ),
                ( 1, 1, config.proj_shape.W )
            )
            for i in range( 0, pa.size )
        ]

    return np.concatenate( w )


'''
    Generate 3D volume of cosine weights

    U
        detector width
    V
        detector height

    returns
        numpy array of shape [1, V, U]

'''
def init_cosine_3D( config ):
    cu = config.proj_shape.W/2 * config.pixel_shape.W
    cv = config.proj_shape.H/2 * config.pixel_shape.H
    sd2 = config.source_det_distance**2

    w = np.zeros( ( 1, config.proj_shape.H, config.proj_shape.W ), dtype =
            np.float32 )

    for v in range( 0, config.proj_shape.H ):
        dv = ( (v+0.5) * config.pixel_shape.H - cv )**2
        for u in range( 0, config.proj_shape.W ):
            du = ( (u+0.5) * config.pixel_shape.W - cu )**2
            w[0,v,u] = config.source_det_distance / math.sqrt( sd2 + dv + dv )

    return w


class ReconstructionConfiguration:

    '''
        proj_shape
            instance of ProjShape
        vol_shape
            shape of volume (instance of 3DShape)
        vol_origin
            volume origin in world coords (instance of 3DCoord)
        voxel_shape
            size of a voxel in mm (instance of 3DShape)
        pixel_shape
            size of a detector pixel in mm (instance of 2DShape)
        source_det_distance
            in mm
        ramlak_width
            in pixel
    '''
    def __init__(
            self,
            proj_shape,
            vol_shape,
            vol_origin,
            voxel_shape,
            pixel_shape,
            source_det_distance,
            ramlak_width
    ):
        assert( type( proj_shape ) is t.ShapeProj )
        assert( type( vol_shape ) is t.Shape3D )
        assert( type( vol_origin ) is t.Coord3D )
        assert( type( voxel_shape ) is t.Shape3D )
        assert( type( pixel_shape ) is t.Shape2D )

        self.proj_shape             = proj_shape
        self.vol_shape              = vol_shape
        self.vol_origin             = vol_origin
        self.voxel_shape            = voxel_shape
        self.pixel_shape            = pixel_shape
        self.source_det_distance    = source_det_distance
        self.ramlak_width           = ramlak_width


class Reconstructor:

    def __init__( self, config, angles, trainable = False, name = None ):
        self.config = config
        self.trainable = trainable
        self.name = name

        with tf.name_scope( self.name, "Reconstruct" ) as scope:
            with tf.variable_scope( self.name, "Reconstruct" ):

                # init cosine weights
                cosine_w_np = init_cosine_3D( config )
                self.cosine_w = tf.Variable(
                        initial_value = cosine_w_np,
                        dtype = tf.float32,
                        name = 'cosine-weights',
                        trainable = False
                )

                # init parker weights
                # NOTE: Current configuration assumes that relative angles
                #       remain valid even if apply is invoked with different
                #       projection matrices!
                self.parker_w_np = init_parker_3D( self.config, angles )
                self.parker_w = tf.Variable(
                        initial_value = self.parker_w_np,
                        dtype = tf.float32,
                        name = 'parker-weights',
                        trainable = self.trainable
                )

                # init ramlak
                ramlak_1D = init_ramlak_1D( config )
                self.pad_size = int( config.ramlak_width/2 )
                ramlak_1D = np.pad( ramlak_1D, ((0, self.pad_size)), 'constant' )
                ramlak_ft = np.fft.fft2( ramlak_1D.reshape( 1, 1, -1 ) )
                self.kernel_ft = tf.Variable(
                        initial_value = ramlak_ft,
                        dtype = tf.complex64,
                        name = 'ramlak-weights',
                        trainable = False
                )

                # initializations for backprojection op
                self.vol_origin_proto = tf.contrib.util.make_tensor_proto(
                        config.vol_origin.toNCHW(), tf.float32 )
                self.voxel_dimen_proto = tf.contrib.util.make_tensor_proto(
                        config.voxel_shape.toNCHW(), tf.float32 )


    '''
        Reset all trainable vars
    '''
    def reset():
        with tf.name_scope( self.name, "Reconstruct" ) as scope:
            with tf.variable_scope( self.name, "Reconstruct" ):
                self.parker_w = tf.Variable(
                        initial_value = self.parker_w_np,
                        dtype = tf.float32,
                        name = 'parker-weights',
                        trainable = self.trainable
                )


    def save_vars():
        return [ self.parker_w ]

    '''
        proj
            the sinogram
        geom
            stack of projection matrices

        returns
            volume tensor
    '''
    def apply( self, proj, geom, fullscan = False ):
        with tf.name_scope( self.name, "Reconstruct", [ proj, geom ] ) as scope:
            with tf.variable_scope( self.name, "Reconstruct", [ proj, geom ] ):

                # COSINE
                proj = tf.multiply( proj, self.cosine_w, name = 'cosine-weighting' )

                # PARKER
                if not fullscan:
                    proj = tf.multiply( proj, self.parker_w, name = 'parker-weighting' )

                # RAMLAK
                s = self.config.proj_shape

                proj = tf.pad( proj, [[0,0], [0,0], [0,self.pad_size]] )
                proj_ft = tf.fft2d( tf.cast( proj, dtype = tf.complex64 ) )
                proj = tf.real( tf.ifft2d( self.kernel_ft*proj_ft ) )[:,:,self.pad_size:]

                proj = tf.reshape( proj, s.toNCHW() )

                # BACKPROJECTION
                geom_proto = tf.contrib.util.make_tensor_proto( geom, tf.float32 )
                vol = backproject(
                        projections = proj,
                        geom        = geom_proto,
                        vol_shape   = self.config.vol_shape.toNCHW(),
                        vol_origin  = self.vol_origin_proto,
                        voxel_dimen = self.voxel_dimen_proto,
                        proj_shape  = self.config.proj_shape.toNCHW(),
                        name        = 'backproject'
                    )

                self.pin = proj

                if fullscan:
                    vol /= 2

                return tf.nn.relu( vol, scope )

