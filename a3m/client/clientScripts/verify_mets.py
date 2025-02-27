"""verify_mets.py

Verify METS documents provided to the script. Its first, and primary use so
far is to verify the validity of custom structmaps included with transfers and
supplied on ingest after appraisal.
"""
import os

from lxml import etree

from a3m.archivematicaFunctions import strToUnicode


class VerifyMETSException(Exception):
    """Exception to raise if METS validation fails."""


def call(jobs):
    """Primary entry point for this script."""
    for job in jobs:
        with job.JobContext():
            mets_structmap = os.path.join(
                strToUnicode(job.args[1]), "metadata", "mets_structmap.xml"
            )
            mets_xsd = job.args[2]
            if not os.path.isfile(mets_structmap):
                job.pyprint("Custom structmap not supplied with package")
                return
            if not os.path.isfile(mets_xsd):
                raise VerifyMETSException
            xmlschema = etree.XMLSchema(
                etree.parse(  # nosec B320
                    mets_xsd, etree.XMLParser(resolve_entities=False, no_network=True)
                )
            )
            # Raise an exception if not valid, e.g. etree.DocumentInvalid
            # otherwise, the document validates correctly and returns.
            xmlschema.assertValid(
                etree.parse(  # nosec B320
                    mets_structmap,
                    etree.XMLParser(resolve_entities=False, no_network=True),
                )
            )
            job.pyprint("Custom structmap validated correctly")
