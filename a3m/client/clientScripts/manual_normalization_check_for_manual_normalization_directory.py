# This file is part of Archivematica.
#
# Copyright 2010-2013 Artefactual Systems Inc. <http://artefactual.com>
#
# Archivematica is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Archivematica is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Archivematica.  If not, see <http://www.gnu.org/licenses/>.
import os


def call(jobs):
    for job in jobs:
        with job.JobContext():
            sip_dir = job.args[1]
            manualNormalizationPath = os.path.join(
                sip_dir, "objects", "manualNormalization"
            )
            job.pyprint("Manual normalization path:", manualNormalizationPath)
            if os.path.isdir(manualNormalizationPath):
                mn_preserve_path = os.path.join(manualNormalizationPath, "preservation")
                if os.path.isdir(mn_preserve_path) and os.listdir(mn_preserve_path):
                    job.pyprint("Manually normalized files found")
                    job.set_status(179)
                    continue

            job.set_status(0)
