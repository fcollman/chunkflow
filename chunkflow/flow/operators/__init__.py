# -*- coding: utf-8 -*-
"""Operators to generate or handle tasks.
"""
from .cloud_watch import CloudWatchOperator
from .create_chunk import CreateChunkOperator
from .crop_margin import CropMarginOperator
from .cutout import CutoutOperator
from .downsample_upload import DownsampleUploadOperator
from .inference import InferenceOperator
from .mask import MaskOperator
from .mesh import MeshOperator
from .neuroglancer import NeuroglancerOperator
from .normalize_section_contrast import NormalizeSectionContrastOperator
from .normalize_section_shang import NormalizeSectionShangOperator
from .custom_operator import CustomOperator
from .read_file import ReadFileOperator
from .read_h5 import ReadH5Operator
from .save import SaveOperator
from .save_images import SaveImagesOperator
from .view import ViewOperator
from .write_h5 import WriteH5Operator