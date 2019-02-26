import numpy as np
import unittest

from .validate import validate_by_template_matching


class TestValidateByTemplateMatching(unittest.TestCase):
    def test_validate_by_template_matching(self):
        image = np.random.randint(0, 256, size=(64, 64, 64), dtype=np.uint8)
        assert validate_by_template_matching(image)
        
        # make a black box 
        image[16:-16, 16:-16, 16:-16] = 0
        assert not validate_by_template_matching(image)
    

