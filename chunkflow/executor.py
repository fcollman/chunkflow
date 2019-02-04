# -*- coding: utf-8 -*-
"""Executor of inference.
"""

import numpy as np
from chunkflow.offset_array import OffsetArray

from cloudvolume import CloudVolume, Storage
from cloudvolume.lib import Vec, Bbox
import time
import os
import json 
from .validate import validate_by_template_matching
from .igneous.tasks import downsample_and_upload
from .igneous.downsample import downsample_with_averaging


class Executor(object):
    """
    run inference like ChunkFlow.jl
    1. cutout image using cloudvolume
    2. run inference
    3. crop the margin to make the output aligned with cloud storage backend
    4. upload to cloud storage using cloudvolume
    Note that I always use z,y,x in python, but cloudvolume use x,y,z for indexing.
    So I always do a reverse of slices before indexing.
    Parameters:
        is_masked_in_device: the patch could be masked/normalized around the 
            boundary, so we only need to do summation in CPU end.
        image_validate_mip: the mip level of image used for checking whether all of 
            the nonzero voxels were downloaded or not.
    """

    def __init__(self,
                 image_layer_path,
                 output_layer_path,
                 convnet_model_path,
                 convnet_weight_path,
                 image_mask_layer_path,
                 output_mask_layer_path,
                 patch_size,
                 patch_overlap,
                 cropping_margin_size,
                 output_key='affinity',
                 num_output_channels=3,
                 mip=1,
                 output_mask_mip=3,
                 framework='pytorch-multitask',
                 missing_section_ids_file_name=None,
                 image_validate_mip=None):
        self.image_layer_path = image_layer_path
        self.convnet_model_path = convnet_model_path
        self.convnet_weight_path = convnet_weight_path
        self.output_mask_layer_path = output_mask_layer_path
        self.output_layer_path = output_layer_path
        self.patch_size = patch_size
        self.patch_overlap = patch_overlap
        self.cropping_margin_size = cropping_margin_size
        self.output_key = output_key
        self.num_output_channels = num_output_channels
        self.image_mip = mip
        self.output_mip = mip
        self.output_mask_mip = output_mask_mip
        self.framework = framework
        self.missing_section_ids_file_name = missing_section_ids_file_name

        if framework == 'pytorch-multitask':
            # currently only pytorch-multitask support in device masking.
            self.is_masked_in_device = True
        else:
            self.is_masked_in_device = False

        self.image_validate_mip = image_validate_mip
    
    def __call__(self, output_bbox):
        self.output_bbox = output_bbox

        self.log = dict()
        total_start = time.time()

        start = time.time()
        self._read_output_mask()
        elapsed = time.time() - start
        self.log['read_output_mask'] = elapsed
        print("Read output mask takes %3f sec" % (elapsed))
        # if the mask is black, no need to run inference
        if np.all(self.output_mask == 0):
            return

        start = time.time()
        self._read_image()
        elapsed = time.time() - start
        self.log['read_image'] = elapsed
        print("Read image takes %3f sec" % (elapsed))

        start = time.time()
        self._validate_image()
        elapsed = time.time() - start
        self.log['validate_image'] = elapsed
        print("Validate image takes %3f sec" % (elapsed))

        start = time.time()
        self._mask_missing_sections()
        elapsed = time.time() - start
        self.log['mask_missing_sections'] = elapsed
        print("Mask missing sections in image takes %3f sec" % (elapsed))

        start = time.time()
        self._inference()
        elapsed = time.time() - start
        self.log['_inference'] = elapsed
        print("Inference takes %3f min" % (elapsed / 60))

        start = time.time()
        self._crop()
        elapsed = time.time() - start
        self.log['crop_output'] = elapsed
        print("Cropping takes %3f sec" % (elapsed))

        if self.output_mask:
            start = time.time()
            self._mask_output()
            elapsed = time.time() - start
            self.log['mask_output'] = elapsed
            print("Mask output takes %3f sec" % (elapsed))

        start = time.time()
        self._upload_output()
        elapsed = time.time() - start
        self.log['upload_output'] = elapsed
        print("Upload output takes %3f min" % (elapsed / 60))

        start = time.time()
        self._create_output_thumbnail()
        elapsed = time.time() - start
        self.log['create_output_thumbnail'] = elapsed
        print("create output thumbnail takes %3f min" % (elapsed / 60))

        total_time = time.time() - total_start
        self.log['total_time'] = total_time
        print("Whole task takes %3f min" % (total_time / 60))

        log_path = os.path.join(self.output_layer_path, 'log')
        self._upload_log(log_path)

    def _read_output_mask(self):
        if self.output_mask_layer_path is None or not self.output_mask_layer_path:
            print('no mask layer path defined')
            self.output_mask = None
            return
        print("download mask chunk...")
        vol = CloudVolume(
            self.output_mask_layer_path,
            bounded=False,
            fill_missing=False,
            progress=True,
            mip=self.output_mask_mip)
        self.xyfactor = 2**(self.output_mask_mip - self.output_mip)
        # only scale the indices in XY plane
        self.output_mask_slices = tuple(
            slice(a.start // self.xyfactor, a.stop // self.xyfactor)
            for a in self.output_bbox.to_slices()[1:3])
        self.output_mask_slices = (
            self.output_bbox.to_slices()[0], ) + self.output_mask_slices

        # the slices did not contain the channel dimension
        print("mask slices: {}".format(self.output_mask_slices))
        self.output_mask = vol[self.output_mask_slices[::-1]]
        self.output_mask = np.transpose(self.output_mask)
        print("shape of output mask: {}".format(self.output_mask.shape))
        self.output_mask = np.squeeze(self.output_mask, axis=0)

    def _mask_missing_sections(self):
        """
        mask some missing sections if the section id was provided 
        """
        if self.missing_section_ids_file_name:
            zslice = self.image.slices[0]
            start = zslice.start
            stop = zslice.stop

            missing_section_ids = np.loadtxt(
                self.missing_section_ids_file_name, dtype='int64')
            for z in missing_section_ids:
                if z > stop:
                    # the section ID list was supposed to be ordered ascendingly
                    break
                elif z >= start and z <= stop:
                    self.image[z - self.image.global_offset[0], :, :] = 0

    def _mask_output(self):
        if np.all(self.output_mask):
            print("mask elements are all positive, return directly")
            return
        if not np.any(self.output):
            print("output volume is all black, return directly")
            return

        print("perform masking ...")
        # use c++ backend
        # from datatools import mask_affiniy_map
        # mask_affinity_map(self.aff, self.output_mask)

        assert np.any(self.output_mask)
        print("upsampling mask ...")
        # upsampling factor in XY plane
        mask = np.zeros(self.output.shape[1:], dtype=self.output_mask.dtype)
        for offset in np.ndindex((self.xyfactor, self.xyfactor)):
            mask[:, np.s_[offset[0]::self.xyfactor], np.
                 s_[offset[1]::self.xyfactor]] = self.output_mask

        assert mask.shape == self.output.shape[1:]
        assert np.any(self.output_mask)
        np.multiply(self.output[0, :, :, :], mask, out=self.output[0, :, :, :])
        np.multiply(self.output[1, :, :, :], mask, out=self.output[1, :, :, :])
        np.multiply(self.output[2, :, :, :], mask, out=self.output[2, :, :, :])
        assert np.any(self.output)

    def _read_image(self):
        self.image_vol = CloudVolume(
            self.image_layer_path,
            bounded=False,
            fill_missing=False,
            progress=True,
            mip=self.image_mip,
            parallel=False)
        output_slices = self.output_bbox.to_slices()
        self.input_slices = tuple(
            slice(s.start - m, s.stop + m)
            for s, m in zip(output_slices, self.cropping_margin_size))
        # always reverse the indexes since cloudvolume use x,y,z indexing
        self.image = self.image_vol[self.input_slices[::-1]]
        # the cutout is fortran ordered, so need to transpose and make it C order
        self.image = np.transpose(self.image)
        self.image = np.ascontiguousarray(self.image)
        assert self.image.shape[0] == 1
        self.image = np.squeeze(self.image, axis=0)
        global_offset = tuple(s.start for s in self.input_slices)

        self.image = OffsetArray(self.image, global_offset=global_offset)

    def _validate_image(self):
        """
        check that all the image voxels was downloaded without black region  
        We have found some black regions in previous inference run, 
        so hopefully this will solve the problem.
        """
        if self.image_validate_mip is None:
            print('no validate mip parameter defined, skiping validation')
            return

        # only use the region corresponds to higher mip level
        # clamp the surrounding regions in XY plane
        # this assumes that the image dataset was downsampled starting from the
        # beginning offset in the info file
        global_offset = self.image.global_offset

        # factor3 follows xyz order in CloudVolume
        factor3 = np.array([
            2**(self.image_validate_mip - self.image_mip), 2**
            (self.image_validate_mip - self.image_mip), 1
        ],
                           dtype=np.int32)
        clamped_offset = tuple(go + f - (go - vo) % f for go, vo, f in zip(
            global_offset[::-1], self.image_vol.voxel_offset, factor3))
        clamped_stop = tuple(go + s - (go + s - vo) % f
                             for go, s, vo, f in zip(
                                 global_offset[::-1], self.image.shape[::-1],
                                 self.image_vol.voxel_offset, factor3))
        clamped_slices = tuple(
            slice(o, s) for o, s in zip(clamped_offset, clamped_stop))
        clamped_bbox = Bbox.from_slices(clamped_slices)
        clamped_image = self.image.cutout(clamped_slices[::-1])
        # transform to xyz order
        clamped_image = np.transpose(clamped_image)
        # get the corresponding bounding box for validation
        validate_bbox = self.image_vol.bbox_to_mip(
            clamped_bbox, mip=self.image_mip, to_mip=self.image_validate_mip)
        #validate_bbox = clamped_bbox // factor3

        # downsample the image using avaraging
        # keep the z as it is since the mip only applies to xy plane
        # recursivly downsample the image
        # if we do it directly, the downsampled image will not be the same with the recursive one
        # because of the rounding error of integer division
        for _ in range(self.image_validate_mip - self.image_mip):
            clamped_image = downsample_with_averaging(
                clamped_image, np.array([2, 2, 1], dtype=np.int32))

        # validation by template matching
        result = validate_by_template_matching(clamped_image)
        if result is False:
            # save the log to error directory
            log_path = os.path.join(self.output_layer_path, 'error')
            self._upload_log(log_path)

        validate_vol = CloudVolume(
            self.image_layer_path,
            bounded=False,
            fill_missing=False,
            progress=True,
            mip=self.image_validate_mip,
            parallel=False)
        validate_image = validate_vol[validate_bbox.to_slices()]
        assert validate_image.shape[3] == 1
        validate_image = np.squeeze(validate_image, axis=3)

        # use the validate image to check the downloaded image
        assert np.alltrue(validate_image == clamped_image)
    
    def _prepare_inference_engine(self):
        def _log_device():
            import torch 
            self.log['device'] = torch.cuda.get_device_name(0)

        # prepare for inference
        from chunkflow.block_inference_engine import BlockInferenceEngine
        if self.framework == 'pznet':
            from chunkflow.frameworks.pznet_patch_inference_engine import PZNetPatchInferenceEngine
            patch_engine = PZNetPatchInferenceEngine(self.convnet_model_path, self.convnet_weight_path)
        elif self.framework == 'pytorch':
            _log_device()
            from chunkflow.frameworks.pytorch_patch_inference_engine import PytorchPatchInferenceEngine
            patch_engine = PytorchPatchInferenceEngine(
                self.convnet_model_path,
                self.convnet_weight_path,
                patch_size=self.patch_size,
                output_key=self.output_key,
                num_output_channels=self.num_output_channels)
        elif self.framework == 'pytorch-multitask':
            _log_device()
            from chunkflow.frameworks.pytorch_multitask_patch_inference import PytorchMultitaskPatchInferenceEngine
            patch_engine = PytorchMultitaskPatchInferenceEngine(
                self.convnet_model_path,
                self.convnet_weight_path,
                patch_size=self.patch_size,
                output_key=self.output_key,
                patch_overlap=self.patch_overlap,
                num_output_channels=self.num_output_channels)
        elif self.framework == 'identity':
            from chunkflow.frameworks.identity_patch_inference_engine import IdentityPatchInferenceEngine
            patch_engine = IdentityPatchInferenceEngine(num_output_channels=3)
        else:
            raise Exception('invalid inference backend: {}'.format(
                self.framework))

        self.block_inference_engine = BlockInferenceEngine(
            patch_inference_engine=patch_engine,
            patch_size=self.patch_size,
            patch_overlap=self.patch_overlap,
            output_key=self.output_key,
            num_output_channels=self.num_output_channels,
            is_masked_in_device=self.is_masked_in_device)

    def _inference(self):
        # this is for fast tests
        # self.output = np.random.randn(3, *self.image.shape).astype('float32')
        # return

        # inference engine input is a OffsetArray rather than normal numpy array
        # it is actually a numpy array with global offset

        # build the inference engine
        self._prepare_inference_engine()

        input_offset = tuple(s.start for s in self.input_slices)
        input_chunk = OffsetArray(self.image, global_offset=input_offset)
        self.output = self.block_inference_engine(input_chunk)

    def _crop(self):
        self.output = self.output[:, self.cropping_margin_size[0]:self.output.
                                  shape[1] - self.cropping_margin_size[0], self
                                  .cropping_margin_size[1]:self.output.
                                  shape[2] - self.cropping_margin_size[1], self
                                  .cropping_margin_size[2]:self.output.
                                  shape[3] - self.cropping_margin_size[2]]

    def _upload_output(self):
        # this is for fast test
        #self.output = np.transpose(self.output)
        #return

        vol = CloudVolume(
            self.output_layer_path,
            fill_missing=True,
            bounded=True,
            autocrop=True,
            mip=self.image_mip,
            progress=True)
        output_slices = self.output_bbox.to_slices()
        # transpose czyx to xyzc order
        self.output = np.transpose(self.output)
        vol[output_slices[::-1] +
            (slice(0, self.output.shape[-1]), )] = self.output

    def _create_output_thumbnail(self):
        """
        quantize the affinitymap and downsample to higher mip level 
        upload the data for visual inspection.
        """
        thumbnail_path = os.path.join(self.output_layer_path, 'thumbnail')
        thumbnail_vol = CloudVolume(
            thumbnail_path,
            compress='gzip',
            fill_missing=True,
            bounded=True,
            autocrop=True,
            mip=self.image_mip,
            progress=True)
        # the output was already transposed to xyz/fortran order in previous step while uploading the output
        # self.output = np.transpose(self.output)

        # only use the last channel, it is the Z affinity if this is affinitymap
        output = self.output[:, :, :, -1]
        image = (output * 255.0).astype(np.uint8)

        # transform zyx to xyz
        output_bbox = Bbox.from_slices(self.output_bbox.to_slices()[::-1])
        shape = Vec(*(output.shape[:3]))

        downsample_and_upload(
            image,
            output_bbox,
            thumbnail_vol,
            shape,
            mip=self.image_mip,
            axis='z',
            skip_first=True,
            only_last_mip=True)

    def _upload_log(self, log_path):
        """
        upload internal log as a file to the same place of output 
        the file name is the output range 
        """

        with Storage(log_path) as storage:
            storage.put_file(
                file_path=self.output_bbox.to_filename(),
                content=json.dumps(self.log),
                content_type='application/json')