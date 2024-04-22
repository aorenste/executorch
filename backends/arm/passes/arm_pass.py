# Copyright (c) Meta Platforms, Inc. and affiliates.
# Copyright 2024 Arm Limited and/or its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from executorch.exir.pass_base import ExportPass
from torch.export import ExportedProgram


class ArmPass(ExportPass):
    def __init__(self, exported_program: ExportedProgram):
        super().__init__()
        self._exported_program = exported_program

    @property
    def exported_program(self) -> ExportedProgram:
        return self._exported_program
