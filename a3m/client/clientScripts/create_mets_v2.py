#
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Archivematica.    If not, see <http://www.gnu.org/licenses/>.
import collections
import copy
import html
import logging
import os
import pprint
import re
import sys
import traceback
from glob import glob
from itertools import chain
from uuid import uuid4

import lxml.etree as etree
from bagit import Bag
from bagit import BagError
from django.utils import timezone

from .archivematicaCreateMETSMetadataCSV import parseMetadata
from .archivematicaCreateMETSRights import archivematicaGetRights
from .sanitize_names import sanitize_name
from a3m import namespaces as ns
from a3m.archivematicaFunctions import escape
from a3m.archivematicaFunctions import normalizeNonDcElementName
from a3m.archivematicaFunctions import strToUnicode
from a3m.main.models import Agent
from a3m.main.models import Derivation
from a3m.main.models import Directory
from a3m.main.models import DublinCore
from a3m.main.models import Event
from a3m.main.models import File
from a3m.main.models import FileID
from a3m.main.models import FPCommandOutput
from a3m.main.models import SIP


class ErrorAccumulator:
    def __init__(self):
        self.error_count = 0


class MetsState:
    def __init__(
        self, globalAmdSecCounter=0, globalTechMDCounter=0, globalDigiprovMDCounter=0
    ):
        self.globalFileGrps = {}
        self.globalFileGrpsUses = [
            "original",
            "submissionDocumentation",
            "preservation",
            "service",
            "access",
            "license",
            "text/ocr",
            "metadata",
            "derivative",
        ]
        for use in self.globalFileGrpsUses:
            grp = etree.Element(ns.metsBNS + "fileGrp")
            grp.set("USE", use)
            self.globalFileGrps[use] = grp

        # counters
        self.amdSecs = []
        self.dmdSecs = []
        self.globalDmdSecCounter = 0
        self.globalAmdSecCounter = globalAmdSecCounter
        self.globalTechMDCounter = globalTechMDCounter
        self.globalDigiprovMDCounter = globalDigiprovMDCounter
        self.globalRightsMDCounter = 0
        self.fileNameToFileID = {}  # Used for mapping structMaps included with transfer
        self.globalStructMapCounter = 0

        # GROUPID="G1" -> GROUPID="Group-%object's UUID%"
        # group of the object and it's related access, license

        self.CSV_METADATA = {}
        self.error_accumulator = ErrorAccumulator()


logger = logging.getLogger(__name__)

FSItem = collections.namedtuple("FSItem", "type path is_empty")
FakeDirMdl = collections.namedtuple("FakeDirMdl", "uuid")


def newChild(parent, tag, text=None, tailText=None, sets=None):
    # TODO convert sets to a dict, and use **dict
    sets = sets or []
    child = etree.SubElement(parent, tag)
    child.text = strToUnicode(text)
    if tailText:
        child.tail = strToUnicode(tailText)
    for set_ in sets:
        key, value = set_
        child.set(key, value)
    return child


SIPMetadataAppliesToType = "3e48343d-e2d2-4956-aaa3-b54d26eb9761"
TransferMetadataAppliesToType = "45696327-44c5-4e78-849b-e027a189bf4d"
FileMetadataAppliesToType = "7f04d9d4-92c2-44a5-93dc-b7bfdf0c1f17"


def getDublinCore(unit, id_):
    db_field_mapping = collections.OrderedDict(
        [
            ("title", "title"),
            ("creator", "creator"),
            ("subject", "subject"),
            ("description", "description"),
            ("publisher", "publisher"),
            ("contributor", "contributor"),
            ("date", "date"),
            ("type", "type"),
            ("format", "format"),
            ("identifier", "identifier"),
            ("source", "source"),
            ("relation", "relation"),
            ("language", "language"),
            ("coverage", "coverage"),
            ("rights", "rights"),
        ]
    )

    try:
        dc = DublinCore.objects.get(
            metadataappliestotype_id=unit, metadataappliestoidentifier=id_
        )
    except DublinCore.DoesNotExist:
        return

    ret = etree.Element(
        ns.dctermsBNS + "dublincore", nsmap={"dcterms": ns.dctermsNS, "dc": ns.dcNS}
    )
    ret.set(
        ns.xsiBNS + "schemaLocation",
        ns.dctermsNS
        + " https://dublincore.org/schemas/xmls/qdc/2008/02/11/dcterms.xsd",
    )

    for dbname, term in db_field_mapping.items():
        txt = getattr(dc, dbname)
        elem_ns = ""
        # See http://dublincore.org/documents/2012/06/14/dcmi-terms/?v=elements for which elements are which namespace
        if term in (
            "contributor",
            "coverage",
            "creator",
            "date",
            "description",
            "format",
            "identifier",
            "language",
            "publisher",
            "relation",
            "rights",
            "source",
            "subject",
            "title",
            "type",
        ):
            elem_ns = ns.dcBNS
        elif term in ("isPartOf",):
            elem_ns = ns.dctermsBNS
        if txt:
            newChild(ret, elem_ns + term, text=txt)
    return ret


def _get_mdl_identifiers(mdl):
    """Get identifiers of a model as a type-value 2-tuple."""
    if isinstance(mdl, FakeDirMdl):
        return []
    return [(idfr.type, idfr.value) for idfr in mdl.identifiers.all()]


def _add_identifier(object_elem, identifier, bns=ns.premisBNS):
    """Add an identifier to a <premis:object>."""
    idfr_type, idfr_val = identifier
    objectIdentifier = etree.SubElement(object_elem, bns + "objectIdentifier")
    etree.SubElement(objectIdentifier, bns + "objectIdentifierType").text = idfr_type
    etree.SubElement(objectIdentifier, bns + "objectIdentifierValue").text = idfr_val
    return object_elem


def getDirDmdSec(dir_mdl, relativeDirectoryPath):
    """Return an lxml ``Element`` representing a <mets:dmdSec> for a directory.
    It describes the directory as a PREMIS:OBJECT of type
    premis:intellectualEntity and lists the directory's original name (i.e.,
    relative path within the transfer) as well as the UUID assigned to it during
    transfer (or arrange), and any other identifiers, as
    premis:objectIdentifiers. Cf. https://projects.artefactual.com/issues/11192.
    """
    dir_uuid = dir_mdl.uuid
    ret = etree.Element(ns.metsBNS + "dmdSec")
    mdWrap = etree.SubElement(ret, ns.metsBNS + "mdWrap")
    mdWrap.set("MDTYPE", "PREMIS:OBJECT")
    xmlData = etree.SubElement(mdWrap, ns.metsBNS + "xmlData")
    object_elem = etree.SubElement(
        xmlData, ns.premisBNS + "object", nsmap={"premis": ns.premisNS}
    )
    object_elem.set(ns.xsiBNS + "type", "premis:intellectualEntity")
    object_elem.set(
        ns.xsiBNS + "schemaLocation",
        ns.premisNS + " http://www.loc.gov/standards/premis/v3/premis.xsd",
    )
    object_elem.set("version", "3.0")
    # Add the directory's UUID and any other identifiers as
    # <premis:objectIdentifier> children of <premis:object>.
    for identifier in chain((("UUID", dir_uuid),), _get_mdl_identifiers(dir_mdl)):
        object_elem = _add_identifier(object_elem, identifier, bns=ns.premisBNS)
    try:
        original_name = escape(dir_mdl.originallocation)
    except AttributeError:  # SIP model won't have originallocation
        original_name = escape(relativeDirectoryPath)
    etree.SubElement(object_elem, ns.premisBNS + "originalName").text = original_name
    return ret


def createDMDIDsFromCSVMetadata(job, path, state):
    """
    Creates dmdSecs with metadata associated with path from the metadata.csv

    :param path: Path relative to the SIP to find CSV metadata on
    :return: Space-separated list of DMDIDs or empty string
    """
    metadata = state.CSV_METADATA.get(path, {})
    dmdsecs = createDmdSecsFromCSVParsedMetadata(job, metadata, state)
    return " ".join([d.get("ID") for d in dmdsecs])


def createDmdSecsFromCSVParsedMetadata(job, metadata, state):
    """
    Create dmdSec(s) from the provided metadata.

    :param metadata: OrderedDict with the metadata keys and a list of values
    :return: List of dmdSec Elements created
    """
    dc = None
    other = None
    ret = []

    # Archivematica does not support refined Dublin Core, e.g.
    # multitiered terms in the format dc.description.abstract
    # If these terms are encountered, an element with only the
    # last portion of the name will be added.
    # e.g., dc.description.abstract is mapped to <dc:abstract>
    refinement_regex = re.compile(r"\w+\.(.+)")

    for key, value in metadata.items():
        if key.startswith("dc.") or key.startswith("dcterms."):
            if dc is None:
                state.globalDmdSecCounter += 1
                ID = "dmdSec_" + state.globalDmdSecCounter.__str__()
                dmdSec = etree.Element(ns.metsBNS + "dmdSec", ID=ID)
                state.dmdSecs.append(dmdSec)
                ret.append(dmdSec)
                mdWrap = etree.SubElement(dmdSec, ns.metsBNS + "mdWrap")
                mdWrap.set("MDTYPE", "DC")
                xmlData = etree.SubElement(mdWrap, ns.metsBNS + "xmlData")
                dc = etree.Element(
                    ns.dctermsBNS + "dublincore",
                    nsmap={"dcterms": ns.dctermsNS, "dc": ns.dcNS},
                )
                dc.set(
                    ns.xsiBNS + "schemaLocation",
                    ns.dctermsNS
                    + " https://dublincore.org/schemas/xmls/qdc/2008/02/11/dcterms.xsd",
                )
                xmlData.append(dc)
            elem_namespace = ""
            if key.startswith("dc."):
                key = key.replace("dc.", "", 1)
                elem_namespace = ns.dcBNS
            elif key.startswith("dcterms."):
                key = key.replace("dcterms.", "", 1)
                elem_namespace = ns.dctermsBNS
            match = re.match(refinement_regex, key)
            if match:
                (key,) = match.groups()
            for v in value:
                etree.SubElement(dc, elem_namespace + key).text = v
        else:  # not a dublin core item
            if other is None:
                state.globalDmdSecCounter += 1
                ID = "dmdSec_" + state.globalDmdSecCounter.__str__()
                dmdSec = etree.Element(ns.metsBNS + "dmdSec", ID=ID)
                state.dmdSecs.append(dmdSec)
                ret.append(dmdSec)
                mdWrap = etree.SubElement(dmdSec, ns.metsBNS + "mdWrap")
                mdWrap.set("MDTYPE", "OTHER")
                mdWrap.set("OTHERMDTYPE", "CUSTOM")
                other = etree.SubElement(mdWrap, ns.metsBNS + "xmlData")
            for v in value:
                etree.SubElement(other, normalizeNonDcElementName(key)).text = v
    return ret


def createDublincoreDMDSecFromDBData(
    job, unit_type, unit_uuid, baseDirectoryPath, state
):
    """
    Creates dmdSec containing DublinCore metadata for unit_uuid.

    If DC metadata exists in the DB, use that.
    If not, check the transfer metadata directory for a dublincore.xml file, and use that.

    :param str unit_type: Pk from MetadataAppliesToType
    :param str unit_uuid: SIP UUID
    :param str baseDirectoryPath: SIP path to check for transfer metadata
    :return: Tuple of (dmdSec Element, DMDID), or None
    """
    dc = getDublinCore(unit_type, unit_uuid)
    if dc is None:
        transfers = os.path.join(baseDirectoryPath, "objects/metadata/transfers/")
        if not os.path.isdir(transfers):
            return None
        for transfer in os.listdir(transfers):
            dcXMLFile = os.path.join(transfers, transfer, "dublincore.xml")
            if os.path.isfile(dcXMLFile):
                try:
                    parser = etree.XMLParser(
                        remove_blank_text=True, resolve_entities=False, no_network=True
                    )
                    dtree = etree.parse(dcXMLFile, parser)  # nosec B320
                    dc = dtree.getroot()
                    break
                except Exception as inst:
                    job.pyprint("error parsing file:", dcXMLFile, file=sys.stderr)
                    job.pyprint(type(inst), file=sys.stderr)  # the exception instance
                    job.pyprint(inst.args, file=sys.stderr)
                    job.print_output(traceback.format_exc())
                    state.error_accumulator.error_count += 1
        else:  # break not called, no DC successfully parsed
            return None
    state.globalDmdSecCounter += 1
    dmdSec = etree.Element(ns.metsBNS + "dmdSec")
    ID = "dmdSec_" + state.globalDmdSecCounter.__str__()
    dmdSec.set("ID", ID)
    mdWrap = etree.SubElement(dmdSec, ns.metsBNS + "mdWrap")
    mdWrap.set("MDTYPE", "DC")
    xmlData = etree.SubElement(mdWrap, ns.metsBNS + "xmlData")
    xmlData.append(dc)
    return (dmdSec, ID)


def createTechMD(fileUUID, state):
    """
    Create a techMD containing a PREMIS:OBJECT for the file with fileUUID.

    :param str fileUUID: UUID of the File to create an object for
    :return: mets:techMD containing a premis:object
    """
    ret = etree.Element(ns.metsBNS + "techMD")
    techMD = ret

    state.globalTechMDCounter += 1
    techMD.set("ID", "techMD_" + str(state.globalTechMDCounter))

    mdWrap = etree.SubElement(techMD, ns.metsBNS + "mdWrap")
    mdWrap.set("MDTYPE", "PREMIS:OBJECT")
    xmlData = etree.SubElement(mdWrap, ns.metsBNS + "xmlData")

    premis_object = create_premis_object(fileUUID)
    xmlData.append(premis_object)
    return ret


def create_premis_object(fileUUID):
    """
    Create a PREMIS:OBJECT for fileUUID.

    Access the models for File, FileID, FPCommandOutput, Derivation

    :param str fileUUID: UUID of the File to create an object for
    :return: premis:object Element, suitable for inserting into mets:xmlData
    """
    f = File.objects.get(uuid=fileUUID)
    # PREMIS:OBJECT
    object_elem = etree.Element(ns.premisBNS + "object", nsmap={"premis": ns.premisNS})
    object_elem.set(ns.xsiBNS + "type", "premis:file")
    object_elem.set(
        ns.xsiBNS + "schemaLocation",
        ns.premisNS + " http://www.loc.gov/standards/premis/v3/premis.xsd",
    )
    object_elem.set("version", "3.0")

    # Add the UUID and any additional file identifiers, e.g., PIDs or
    # PURLs/URIs, to the XML.
    for identifier in chain((("UUID", fileUUID),), _get_mdl_identifiers(f)):
        object_elem = _add_identifier(object_elem, identifier)

    objectCharacteristics = etree.SubElement(
        object_elem, ns.premisBNS + "objectCharacteristics"
    )
    etree.SubElement(
        objectCharacteristics, ns.premisBNS + "compositionLevel"
    ).text = "0"

    fixity = etree.SubElement(objectCharacteristics, ns.premisBNS + "fixity")
    etree.SubElement(
        fixity, ns.premisBNS + "messageDigestAlgorithm"
    ).text = f.checksumtype
    etree.SubElement(fixity, ns.premisBNS + "messageDigest").text = f.checksum

    etree.SubElement(objectCharacteristics, ns.premisBNS + "size").text = str(f.size)

    for elem in create_premis_object_formats(fileUUID):
        objectCharacteristics.append(elem)

    creatingApplication = etree.Element(ns.premisBNS + "creatingApplication")
    etree.SubElement(
        creatingApplication, ns.premisBNS + "dateCreatedByApplication"
    ).text = f.modificationtime.strftime("%Y-%m-%d")
    objectCharacteristics.append(creatingApplication)

    for elem in create_premis_object_characteristics_extensions(fileUUID):
        objectCharacteristics.append(elem)

    etree.SubElement(object_elem, ns.premisBNS + "originalName").text = escape(
        f.originallocation
    )

    for elem in create_premis_object_derivations(fileUUID):
        object_elem.append(elem)

    return object_elem


def create_premis_object_formats(fileUUID):
    files = FileID.objects.filter(file_id=fileUUID)
    elements = []
    if not files.exists():
        fmt = etree.Element(ns.premisBNS + "format")
        formatDesignation = etree.SubElement(fmt, ns.premisBNS + "formatDesignation")
        etree.SubElement(
            formatDesignation, ns.premisBNS + "formatName"
        ).text = "Unknown"
        elements.append(fmt)
    for row in files.values_list(
        "format_name", "format_version", "format_registry_name", "format_registry_key"
    ):
        fmt = etree.Element(ns.premisBNS + "format")

        formatDesignation = etree.SubElement(fmt, ns.premisBNS + "formatDesignation")
        etree.SubElement(formatDesignation, ns.premisBNS + "formatName").text = row[0]
        etree.SubElement(formatDesignation, ns.premisBNS + "formatVersion").text = row[
            1
        ]

        formatRegistry = etree.SubElement(fmt, ns.premisBNS + "formatRegistry")
        etree.SubElement(
            formatRegistry, ns.premisBNS + "formatRegistryName"
        ).text = row[2]
        etree.SubElement(formatRegistry, ns.premisBNS + "formatRegistryKey").text = row[
            3
        ]
        elements.append(fmt)

    return elements


def create_premis_object_characteristics_extensions(fileUUID):
    elements = []
    objectCharacteristicsExtension = etree.Element(
        ns.premisBNS + "objectCharacteristicsExtension"
    )
    parser = etree.XMLParser(remove_blank_text=True)
    documents = FPCommandOutput.objects.filter(
        file_id=fileUUID,
        rule__purpose__in=["characterization", "default_characterization"],
    ).values_list("content")
    for (document,) in documents:
        # This needs to be converted into an str because lxml doesn't accept
        # XML documents in unicode strings if the document contains an
        # encoding declaration.
        output = etree.XML(document.encode("utf-8"), parser)
        objectCharacteristicsExtension.append(output)
    if len(objectCharacteristicsExtension):
        elements.append(objectCharacteristicsExtension)

    return elements


def create_premis_object_derivations(fileUUID):
    elements = []
    # Derivations
    derivations = Derivation.objects.filter(
        source_file_id=fileUUID, event__isnull=False
    )
    for derivation in derivations:
        relationship = etree.Element(ns.premisBNS + "relationship")
        etree.SubElement(
            relationship, ns.premisBNS + "relationshipType"
        ).text = "derivation"
        etree.SubElement(
            relationship, ns.premisBNS + "relationshipSubType"
        ).text = "is source of"

        relatedObjectIdentifier = etree.SubElement(
            relationship, ns.premisBNS + "relatedObjectIdentifier"
        )
        etree.SubElement(
            relatedObjectIdentifier, ns.premisBNS + "relatedObjectIdentifierType"
        ).text = "UUID"
        etree.SubElement(
            relatedObjectIdentifier, ns.premisBNS + "relatedObjectIdentifierValue"
        ).text = derivation.derived_file_id

        relatedEventIdentifier = etree.SubElement(
            relationship, ns.premisBNS + "relatedEventIdentifier"
        )
        etree.SubElement(
            relatedEventIdentifier, ns.premisBNS + "relatedEventIdentifierType"
        ).text = "UUID"
        etree.SubElement(
            relatedEventIdentifier, ns.premisBNS + "relatedEventIdentifierValue"
        ).text = str(derivation.event_id)

        elements.append(relationship)

    derivations = Derivation.objects.filter(
        derived_file_id=fileUUID, event__isnull=False
    )
    for derivation in derivations:
        relationship = etree.Element(ns.premisBNS + "relationship")
        etree.SubElement(
            relationship, ns.premisBNS + "relationshipType"
        ).text = "derivation"
        etree.SubElement(
            relationship, ns.premisBNS + "relationshipSubType"
        ).text = "has source"

        relatedObjectIdentifier = etree.SubElement(
            relationship, ns.premisBNS + "relatedObjectIdentifier"
        )
        etree.SubElement(
            relatedObjectIdentifier, ns.premisBNS + "relatedObjectIdentifierType"
        ).text = "UUID"
        etree.SubElement(
            relatedObjectIdentifier, ns.premisBNS + "relatedObjectIdentifierValue"
        ).text = derivation.source_file_id

        relatedEventIdentifier = etree.SubElement(
            relationship, ns.premisBNS + "relatedEventIdentifier"
        )
        etree.SubElement(
            relatedEventIdentifier, ns.premisBNS + "relatedEventIdentifierType"
        ).text = "UUID"
        etree.SubElement(
            relatedEventIdentifier, ns.premisBNS + "relatedEventIdentifierValue"
        ).text = str(derivation.event_id)

        elements.append(relationship)

    return elements


def createDigiprovMD(fileUUID, state):
    """
    Create digiprovMD for PREMIS Events and linking Agents.
    """
    ret = []

    events = Event.objects.filter(file_uuid_id=fileUUID)
    for event_record in events:
        state.globalDigiprovMDCounter += 1
        digiprovMD = etree.Element(
            ns.metsBNS + "digiprovMD",
            ID="digiprovMD_" + str(state.globalDigiprovMDCounter),
        )
        ret.append(digiprovMD)

        mdWrap = etree.SubElement(
            digiprovMD, ns.metsBNS + "mdWrap", MDTYPE="PREMIS:EVENT"
        )
        xmlData = etree.SubElement(mdWrap, ns.metsBNS + "xmlData")
        xmlData.append(createEvent(event_record))

    agents = Agent.objects.filter(event__file_uuid_id=fileUUID).distinct()
    for agent in agents:
        state.globalDigiprovMDCounter += 1
        digiprovMD = etree.Element(
            ns.metsBNS + "digiprovMD",
            ID="digiprovMD_" + str(state.globalDigiprovMDCounter),
        )
        ret.append(digiprovMD)

        mdWrap = etree.SubElement(
            digiprovMD, ns.metsBNS + "mdWrap", MDTYPE="PREMIS:AGENT"
        )
        xmlData = etree.SubElement(mdWrap, ns.metsBNS + "xmlData")
        xmlData.append(createAgent(agent))

    return ret


def createEvent(event_record):
    """Returns a PREMIS Event."""
    event = etree.Element(ns.premisBNS + "event", nsmap={"premis": ns.premisNS})
    event.set(
        ns.xsiBNS + "schemaLocation",
        ns.premisNS + " http://www.loc.gov/standards/premis/v3/premis.xsd",
    )
    event.set("version", "3.0")

    eventIdentifier = etree.SubElement(event, ns.premisBNS + "eventIdentifier")
    etree.SubElement(
        eventIdentifier, ns.premisBNS + "eventIdentifierType"
    ).text = "UUID"
    etree.SubElement(eventIdentifier, ns.premisBNS + "eventIdentifierValue").text = str(
        event_record.event_id
    )

    etree.SubElement(event, ns.premisBNS + "eventType").text = event_record.event_type
    etree.SubElement(
        event, ns.premisBNS + "eventDateTime"
    ).text = event_record.event_datetime.isoformat()

    eventDetailInformation = etree.SubElement(
        event, ns.premisBNS + "eventDetailInformation"
    )
    etree.SubElement(
        eventDetailInformation, ns.premisBNS + "eventDetail"
    ).text = escape(event_record.event_detail)

    eventOutcomeInformation = etree.SubElement(
        event, ns.premisBNS + "eventOutcomeInformation"
    )
    etree.SubElement(
        eventOutcomeInformation, ns.premisBNS + "eventOutcome"
    ).text = event_record.event_outcome
    eventOutcomeDetail = etree.SubElement(
        eventOutcomeInformation, ns.premisBNS + "eventOutcomeDetail"
    )
    etree.SubElement(
        eventOutcomeDetail, ns.premisBNS + "eventOutcomeDetailNote"
    ).text = escape(event_record.event_outcome_detail)

    # linkingAgentIdentifier
    for agent in event_record.agents.all():
        linkingAgentIdentifier = etree.SubElement(
            event, ns.premisBNS + "linkingAgentIdentifier"
        )
        etree.SubElement(
            linkingAgentIdentifier, ns.premisBNS + "linkingAgentIdentifierType"
        ).text = agent.identifiertype
        etree.SubElement(
            linkingAgentIdentifier, ns.premisBNS + "linkingAgentIdentifierValue"
        ).text = agent.identifiervalue
    return event


def createAgent(agent_record):
    """Creates a PREMIS Agent as a SubElement of digiprovMD."""
    agent = etree.Element(ns.premisBNS + "agent", nsmap={"premis": ns.premisNS})
    agent.set(
        ns.xsiBNS + "schemaLocation",
        ns.premisNS + " http://www.loc.gov/standards/premis/v3/premis.xsd",
    )
    agent.set("version", "3.0")

    agentIdentifier = etree.SubElement(agent, ns.premisBNS + "agentIdentifier")
    etree.SubElement(
        agentIdentifier, ns.premisBNS + "agentIdentifierType"
    ).text = agent_record.identifiertype
    etree.SubElement(
        agentIdentifier, ns.premisBNS + "agentIdentifierValue"
    ).text = agent_record.identifiervalue
    etree.SubElement(agent, ns.premisBNS + "agentName").text = agent_record.name
    etree.SubElement(agent, ns.premisBNS + "agentType").text = agent_record.agenttype
    return agent


def getAMDSec(
    job,
    fileUUID,
    use,
    sip_uuid,
    transferUUID,
    itemdirectoryPath,
    baseDirectoryPath,
    state,
):
    """
    Creates an amdSec.

    techMD contains a PREMIS:OBJECT, see createTechMD
    rightsMD contain PREMIS:RIGHTS, see archivematicaGetRights
    digiprovMD contain PREMIS:EVENT and PREMIS:AGENT, see createDigiprovMD and createDigiprovMDAgents

    :param fileUUID: UUID of the file
    :param use: If "original", look for rights metadata.
    :param sip_uuid: UUID of the SIP this file is in, to check for original file rights metadata.
    :param transferUUID: UUID of the Transfer this file was in, to check for original file rights metadata.
    :param itemdirectoryPath:
    """
    state.globalAmdSecCounter += 1
    AMDID = "amdSec_%s" % (state.globalAmdSecCounter.__str__())
    AMD = etree.Element(ns.metsBNS + "amdSec", ID=AMDID)
    ret = (AMD, AMDID)

    # tech MD
    AMD.append(createTechMD(fileUUID, state))

    if use == "original":
        metadataAppliesToList = [
            (fileUUID, FileMetadataAppliesToType),
            (sip_uuid, SIPMetadataAppliesToType),
            (transferUUID, TransferMetadataAppliesToType),
        ]
        for a in archivematicaGetRights(job, metadataAppliesToList, fileUUID, state):
            state.globalRightsMDCounter += 1
            rightsMD = etree.SubElement(AMD, ns.metsBNS + "rightsMD")
            rightsMD.set("ID", "rightsMD_" + state.globalRightsMDCounter.__str__())
            mdWrap = newChild(rightsMD, ns.metsBNS + "mdWrap")
            mdWrap.set("MDTYPE", "PREMIS:RIGHTS")
            xmlData = newChild(mdWrap, ns.metsBNS + "xmlData")
            xmlData.append(a)

    for a in createDigiprovMD(fileUUID, state):
        AMD.append(a)

    return ret


def _fixup_path_input_by_user(job, path):
    """Fix-up paths submitted by a user, e.g. in custom structmap examples so
    that they don't have to anticipate the Archivematica normalization process.
    """
    return os.path.join("", *[sanitize_name(name) for name in path.split(os.path.sep)])


def include_custom_structmap(
    job, baseDirectoryPath, state, custom_structmap="mets_structmap.xml"
):
    """Enable users in submitting a structmap with a transfer and have that
    included in the eventual AIP METS.
    """
    ret = []
    transferMetadata = os.path.join(
        baseDirectoryPath, os.path.join("objects", "metadata", "transfers")
    )
    if not os.path.isdir(transferMetadata):
        return ret
    baseLocations = os.listdir(transferMetadata)
    baseLocations.append(baseDirectoryPath)
    for dir_ in baseLocations:
        dirPath = os.path.join(transferMetadata, dir_)
        structMapXmlPath = os.path.join(dirPath, custom_structmap)
        if not os.path.isdir(dirPath):
            continue
        if os.path.isfile(structMapXmlPath):
            tree = etree.parse(  # nosec B320
                structMapXmlPath,
                etree.XMLParser(resolve_entities=False, no_network=True),
            )
            root = tree.getroot()
            structMap = root.find(ns.metsBNS + "structMap")
            id_ = structMap.get("ID")
            if not id_:
                state.globalStructMapCounter += 1
                structMap.set("ID", f"structMap_{state.globalStructMapCounter}")
            ret.append(structMap)
            # CONTENTIDS will map to fptrs and area elements where present.
            fptrs = root.xpath("//mets:fptr", namespaces={"mets": ns.metsNS})
            area_elements = root.xpath("//mets:area", namespaces={"mets": ns.metsNS})
            contentids = structMap.xpath(
                "//*[@CONTENTIDS]", namespaces={"mets:": ns.metsNS}
            )
            if not contentids:
                state.error_accumulator.error_count += 1
                logger.error(
                    "No CONTENTIDS found in custom structMap. AIP METS cannot be generated"
                )
                return []
            if len(contentids) < (len(fptrs) + len(area_elements)):
                logger.error(
                    "Mismatch of CONTENTID elements to elements we wish to replace. AIP METS cannot be generated"
                )
                state.error_accumulator.error_count += 1
                return []
            for item in contentids:
                file_path = item.get("CONTENTIDS")
                if not file_path:
                    logger.error(
                        "Empty file path in custom structMap. AIP METS cannot be generated"
                    )
                    state.error_accumulator.error_count += 1
                    return []
                normalized_path = _fixup_path_input_by_user(job, file_path)
                if normalized_path in state.fileNameToFileID:
                    item.set("FILEID", state.fileNameToFileID[normalized_path])
                else:
                    state.error_accumulator.error_count += 1
                    logger.error(
                        "No fileUUID for '%s'; original in custom structMap: %s",
                        normalized_path,
                        file_path,
                    )
                    return []
    if ret:
        job.pyprint("Custom structmap will be included in AIP METS")
    return ret


# DMDID="dmdSec_01" for an object goes in here
# <file ID="file1-UUID" GROUPID="G1" DMDID="dmdSec_02" ADMID="amdSec_01">


def createFileSec(
    job,
    directoryPath,
    parentDiv,
    baseDirectoryPath,
    baseDirectoryName,
    fileGroupIdentifier,
    fileGroupType,
    directories,
    state,
    includeAmdSec=True,
):

    """Creates fileSec and structMap entries for files on disk recursively.

    :param directoryPath: Path to recursively traverse and create METS entries for
    :param parentDiv: structMap div to attach created children to
    :param baseDirectoryPath: SIP path
    :param baseDirectoryName: Name of the %var% for the SIP path
    :param fileGroupIdentifier: SIP UUID
    :param fileGroupType: Name of the foreign key field linking to SIP UUID in files.
    :param includeAmdSec: If True, creates amdSecs for the files
    """
    filesInThisDirectory = []
    try:
        directoryContents = sorted(os.listdir(directoryPath))
    except os.error:
        # Directory doesn't exist
        job.pyprint(directoryPath, "doesn't exist", file=sys.stderr)
        return

    # Create the <mets:div> element for the directory that this file is in.
    # If this directory has been assigned a UUID during transfer, retrieve that
    # UUID based on the directory's relative path and document it in its own
    # <mets:dmdSec> element.
    directoryName = os.path.basename(directoryPath)
    relativeDirectoryPath = "%SIPDirectory%" + os.path.join(
        directoryPath.replace(baseDirectoryPath, "", 1), ""
    )
    dir_mdl = directories.get(
        relativeDirectoryPath, directories.get(relativeDirectoryPath.rstrip("/"))
    )
    dir_dmd_id = None
    if dir_mdl:
        dirDmdSec = getDirDmdSec(dir_mdl, relativeDirectoryPath)
        state.globalDmdSecCounter += 1
        state.dmdSecs.append(dirDmdSec)
        dir_dmd_id = "dmdSec_" + state.globalDmdSecCounter.__str__()
        dirDmdSec.set("ID", dir_dmd_id)
    structMapDiv = etree.SubElement(
        parentDiv, ns.metsBNS + "div", TYPE="Directory", LABEL=directoryName
    )

    DMDIDS = createDMDIDsFromCSVMetadata(
        job, directoryPath.replace(baseDirectoryPath, "", 1), state
    )
    if DMDIDS or dir_dmd_id:
        if DMDIDS and dir_dmd_id:
            structMapDiv.set("DMDID", dir_dmd_id + " " + DMDIDS)
        elif DMDIDS:
            structMapDiv.set("DMDID", DMDIDS)
        else:
            structMapDiv.set("DMDID", dir_dmd_id)

    for item in directoryContents:
        itemdirectoryPath = os.path.join(directoryPath, item)
        if os.path.isdir(itemdirectoryPath):
            createFileSec(
                job,
                itemdirectoryPath,
                structMapDiv,
                baseDirectoryPath,
                baseDirectoryName,
                fileGroupIdentifier,
                fileGroupType,
                directories,
                state,
                includeAmdSec=includeAmdSec,
            )

        elif os.path.isfile(itemdirectoryPath):
            # Setup variables for creating file metadata
            DMDIDS = ""
            directoryPathSTR = itemdirectoryPath.replace(
                baseDirectoryPath, baseDirectoryName, 1
            )

            kwargs = {
                "removedtime__isnull": True,
                fileGroupType: fileGroupIdentifier,
                "currentlocation": directoryPathSTR,
            }
            try:
                f = File.objects.get(**kwargs)
            except File.DoesNotExist:
                job.pyprint(
                    'No uuid for file: "', directoryPathSTR, '"', file=sys.stderr
                )
                state.error_accumulator.error_count += 1
                continue

            use = f.filegrpuse
            label = f.label

            directoryPathSTR = itemdirectoryPath.replace(baseDirectoryPath, "", 1)

            # Create <div TYPE="Item"> and child <fptr>
            # <fptr FILEID="file-<UUID>" LABEL="filename.ext">
            fileId = f"file-{f.uuid}"
            label = item if not label else label
            fileDiv = etree.SubElement(
                structMapDiv, ns.metsBNS + "div", LABEL=label, TYPE="Item"
            )
            etree.SubElement(fileDiv, ns.metsBNS + "fptr", FILEID=fileId)
            # Pair items listed in custom structmaps. Strip leading path
            # separator if it exists.
            state.fileNameToFileID[directoryPathSTR] = fileId

            # Determine fileGrp @GROUPID based on the file's fileGrpUse and transfer type
            GROUPID = ""
            if f.filegrpuuid:
                # GROUPID was determined elsewhere
                GROUPID = "Group-%s" % (f.filegrpuuid)

            elif use in ("original", "submissionDocumentation", "metadata"):
                # These files are in a group defined by themselves
                GROUPID = "Group-%s" % (f.uuid)
                # Check for CSV-based Dublincore dmdSec
                if use == "original":
                    DMDIDS = createDMDIDsFromCSVMetadata(
                        job,
                        f.originallocation.replace("%transferDirectory%", "", 1),
                        state,
                    )
                    if DMDIDS:
                        fileDiv.set("DMDID", DMDIDS)

            elif use in ("preservation", "text/ocr", "derivative"):
                # Derived files should be in the original file's group
                try:
                    d = Derivation.objects.get(derived_file_id=f.uuid)
                except Derivation.DoesNotExist:
                    job.pyprint(
                        "Fatal error: unable to locate a Derivation object"
                        " where the derived file is {}".format(f.uuid)
                    )
                    raise
                GROUPID = "Group-" + d.source_file_id

            elif use == "service":
                # Service files are in the original file's group
                fileFileIDPath = itemdirectoryPath.replace(
                    baseDirectoryPath + "objects/service/",
                    baseDirectoryName + "objects/",
                )
                objectNameExtensionIndex = fileFileIDPath.rfind(".")
                fileFileIDPath = fileFileIDPath[: objectNameExtensionIndex + 1]

                kwargs = {
                    "removedtime__isnull": True,
                    fileGroupType: fileGroupIdentifier,
                    "filegrpuse": "original",
                    "currentlocation__startswith": fileFileIDPath,
                }
                original_file = File.objects.get(**kwargs)
                GROUPID = "Group-" + original_file.uuid

            if GROUPID == "":
                state.error_accumulator.error_count += 1
                job.pyprint(
                    'No groupID for file: "', directoryPathSTR, '"', file=sys.stderr
                )

            if use not in state.globalFileGrps:
                job.pyprint('Invalid use: "%s"' % (use), file=sys.stderr)
                state.error_accumulator.error_count += 1
            else:
                file_elem = etree.SubElement(
                    state.globalFileGrps[use],
                    ns.metsBNS + "file",
                    ID=fileId,
                    GROUPID=GROUPID,
                )
                if use == "original":
                    filesInThisDirectory.append(file_elem)
                # <Flocat xlink:href="objects/file1-UUID" locType="other" otherLocType="system"/>
                newChild(
                    file_elem,
                    ns.metsBNS + "FLocat",
                    sets=[
                        (ns.xlinkBNS + "href", directoryPathSTR),
                        ("LOCTYPE", "OTHER"),
                        ("OTHERLOCTYPE", "SYSTEM"),
                    ],
                )
                if includeAmdSec:
                    AMD, ADMID = getAMDSec(
                        job,
                        f.uuid,
                        use,
                        fileGroupIdentifier,
                        f.transfer_id,
                        itemdirectoryPath,
                        baseDirectoryPath,
                        state,
                    )
                    state.amdSecs.append(AMD)
                    file_elem.set("ADMID", ADMID)

    return structMapDiv


def build_arranged_structmap(job, original_structmap, sip_uuid):
    """
    Given a structMap, builds a new copy of the structMap with file and directory labels assigned according to their intellectual arrangement.
    Logical arrangement is determined using the levels of description which were assigned to them during SIP arrange.

    :param etree.Element original_structmap: the structMap on which the arranged structMap should be based.
    :param str sip_uuid: the SIP's UUID
    """
    tag_dict = dict()  # Removed SIPArrange lookup
    if not tag_dict:
        return

    structmap = copy.deepcopy(original_structmap)
    structmap.attrib["TYPE"] = "logical"
    structmap.attrib["LABEL"] = "Hierarchical"
    structmap.attrib["ID"] = f"structMap_{uuid4()}"
    root_div = structmap.find("./mets:div", namespaces=ns.NSMAP)
    del root_div.attrib["TYPE"]
    objects = root_div.find('./mets:div[@LABEL="objects"]', namespaces=ns.NSMAP)

    # The contents of submissionDocumentation and metadata do
    # not have intellectual arrangement, so don't need to be
    # represented in this structMap.
    for label in ("submissionDocumentation", "metadata"):
        div = objects.find(f'.mets:div[@LABEL="{label}"]', namespaces=ns.NSMAP)
        if div is not None:
            objects.remove(div)

    # Handle objects level of description separately, since tag paths are relative to objects
    tag = tag_dict.get(".")
    if tag:
        job.pyprint("Adding TYPE=%s for logical structMap element objects" % tag)
        objects.attrib["TYPE"] = tag
    else:
        del objects.attrib["TYPE"]

    for element in objects.iterdescendants():
        if element.tag != ns.metsBNS + "div":
            continue

        # Build the full path relative to objects dir
        path = [element.attrib["LABEL"]]
        parent = element.getparent()
        while parent != objects:
            path.insert(0, parent.attrib["LABEL"])
            parent = parent.getparent()
        relative_location = os.path.join(*path)

        # Certain items won't have a level of description;
        # they should be retained in the tree, but have
        # no TYPE attribute.
        tag = tag_dict.get(relative_location)
        if tag:
            job.pyprint(
                "Adding TYPE=%s for logical structMap element %s"
                % (tag, relative_location)
            )
            element.attrib["TYPE"] = tag
        else:
            del element.attrib["TYPE"]

    return structmap


def find_source_metadata(path):
    """
    Returns lists of all metadata to be referenced in the final document.
    This includes transfer metadata (embedded), and any XML metadata contained
    in the `metadata/sourceMD` directory from the original transfer (mdRef).

    The first returned list is the set of transfer metadata; the second is
    all other metadata to reference.
    """
    transfer = []
    source = []
    for dirpath, subdirs, filenames in os.walk(path):
        if "transfer_metadata.xml" in filenames:
            transfer.append(os.path.join(dirpath, "transfer_metadata.xml"))

        if "sourceMD" in subdirs:
            pattern = os.path.join(dirpath, "sourceMD", "*.xml")
            source.extend(glob(pattern))

    return transfer, source


def find_bag_metadata(job, bag_logs_path):
    try:
        return Bag(bag_logs_path).info
    except BagError:
        job.pyprint(
            f"Unable to locate or parse bag metadata at: {bag_logs_path}",
            file=sys.stderr,
        )
        return {}


def create_object_metadata(job, struct_map, baseDirectoryPath, state):
    transfer_metadata_path = os.path.join(
        baseDirectoryPath, "objects/metadata/transfers"
    )
    transfer, source = find_source_metadata(transfer_metadata_path)

    paths = glob(
        os.path.join(baseDirectoryPath, "logs", "transfers", "**", "logs", "BagIt")
    )
    bag_info = [find_bag_metadata(job, path) for path in paths]

    if not transfer and not source and not bag_info:
        return

    state.globalAmdSecCounter += 1
    label = f"amdSec_{state.globalAmdSecCounter}"
    struct_map.set("ADMID", label)

    source_md_counter = 1

    el = etree.Element(ns.metsBNS + "amdSec", {"ID": label})

    for filename in transfer:
        sourcemd = etree.SubElement(
            el, ns.metsBNS + "sourceMD", {"ID": f"sourceMD_{source_md_counter}"}
        )
        mdwrap = etree.SubElement(sourcemd, ns.metsBNS + "mdWrap", {"MDTYPE": "OTHER"})
        xmldata = etree.SubElement(mdwrap, ns.metsBNS + "xmlData")
        source_md_counter += 1
        parser = etree.XMLParser(remove_blank_text=True)
        md = etree.parse(  # nosec B320
            filename, parser, etree.XMLParser(resolve_entities=False, no_network=True)
        )
        xmldata.append(md.getroot())

    for filename in source:
        sourcemd = etree.SubElement(
            el, ns.metsBNS + "sourceMD", {"ID": f"sourceMD_{source_md_counter}"}
        )
        source_md_counter += 1
        attributes = {
            ns.xlinkBNS + "href": os.path.relpath(filename, baseDirectoryPath),
            "MDTYPE": "OTHER",
            "LOCTYPE": "OTHER",
            "OTHERLOCTYPE": "SYSTEM",
        }
        etree.SubElement(sourcemd, ns.metsBNS + "mdRef", attributes)

    for bagdata in bag_info:
        # If there are no tags, skip creating an element
        if not bagdata:
            continue

        sourcemd = etree.SubElement(
            el, ns.metsBNS + "sourceMD", {"ID": f"sourceMD_{source_md_counter}"}
        )
        source_md_counter += 1
        mdwrap = etree.SubElement(
            sourcemd, ns.metsBNS + "mdWrap", {"MDTYPE": "OTHER", "OTHERMDTYPE": "BagIt"}
        )
        xmldata = etree.SubElement(mdwrap, ns.metsBNS + "xmlData")
        bag_metadata = etree.SubElement(xmldata, "transfer_metadata")
        for key, value in bagdata.items():
            if not isinstance(value, list):
                value = [value]
            for v in value:
                try:
                    bag_tag = etree.SubElement(bag_metadata, key)
                except ValueError:
                    job.pyprint(
                        "Skipping bag key {}; not a valid" " XML tag name".format(key),
                        file=sys.stderr,
                    )
                    continue
                bag_tag.text = v

    return el


def write_mets(tree, filename):
    """
    Write tree to filename, and a validate METS form.

    :param ElementTree tree: METS ElementTree
    :param str filename: Filename to write the METS to
    """
    tree.write(filename, pretty_print=True, xml_declaration=True, encoding="utf-8")

    validate_filename = filename + ".validatorTester.html"
    fileContents = """<html>
<body>
  <form method="post" action="http://pim.fcla.edu/validate/results">
    <label for="document">Enter XML Document:</label>
    <br/>
    <textarea id="directinput" rows="12" cols="76" name="document">%s</textarea>
    <br/>
    <br/>
    <input type="submit" value="Validate" />
    <br/>
  </form>
</body>
</html>""" % (
        html.escape(
            etree.tostring(
                tree, pretty_print=True, xml_declaration=True, encoding="utf-8"
            ).decode("utf8"),
        )
    )
    with open(validate_filename, "w") as f:
        f.write(fileContents)


def get_paths_as_fsitems(baseDirectoryPath, objectsDirectoryPath):
    """Get all paths in the SIP as ``FSItem`` instances before deleting any
    empty directories. These filesystem items are crucially ordered so that
    directories always precede the paths of the items they contain.
    :param string baseDirectoryPath: path to the AIP with a trailing slash
    :param string objectsDirectoryPath: path to the AIP's object directory
    :returns: list of ``FSItem`` instances representing paths
    """
    all_fsitems = []
    for root, dirs, files in os.walk(objectsDirectoryPath):
        root = root.replace(baseDirectoryPath, "", 1)
        if files or dirs:
            all_fsitems.append(FSItem("dir", root, is_empty=False))
        else:
            all_fsitems.append(FSItem("dir", root, is_empty=True))
        for file_ in files:
            all_fsitems.append(
                FSItem("file", os.path.join(root, file_), is_empty=False)
            )
    return all_fsitems


def get_normative_structmap(
    baseDirectoryPath, objectsDirectoryPath, directories, state
):
    """Get a normative structMap representing the paths within a SIP.
    :param string baseDirectoryPath: path to the AIP with a trailing slash
    :param string objectsDirectoryPath: path to the AIP's object directory
    :param dict directories: maps directory model instance ``currentlocation``
    :returns: etree Element representing structMap XML
    """
    normativeStructMap = etree.Element(
        ns.metsBNS + "structMap",
        TYPE="logical",
        ID=f"structMap_{state.globalStructMapCounter}",
        LABEL="Normative Directory Structure",
    )
    normativeStructMapDiv = etree.SubElement(
        normativeStructMap,
        ns.metsBNS + "div",
        TYPE="Directory",
        LABEL=os.path.basename(baseDirectoryPath.rstrip("/")),
    )
    all_fsitems = get_paths_as_fsitems(baseDirectoryPath, objectsDirectoryPath)
    add_normative_structmap_div(all_fsitems, normativeStructMapDiv, directories, state)
    return normativeStructMap


def add_normative_structmap_div(
    all_fsitems, root_el, directories, state, path_to_el=None
):
    """Document all of the file/dir paths in ``all_fsitems`` in the
    lxml._Element instance ``root_el``. This constructs the <mets:div> element
    tree under the TYPE "logical" structMap with LABEL "Normative Directory
    Structure". Said structural map documents all files and directories,
    including empty directories, which latter are not documented in the
    physical structMap.
    :param list all_fsitems: contains ``FSItem`` instances crucially ordered so
        that parent directories always precede their children.
    :param lxml._Element root_el: root element for documenting the directory
        structure.
    :param dict directories: maps directory model instance ``currentlocation``
        values to directories for any and all directory model instances
        associated to the current SIP.
    :param dict path_to_el: maps paths from ``all_fsitems`` to the lxml elements
        that document them.
    :returns: None.
    """
    path_to_el = path_to_el or {"": root_el}
    for fsitem in all_fsitems:
        parent_path = os.path.dirname(fsitem.path)
        basename = os.path.basename(fsitem.path)
        try:
            parent_el = path_to_el[parent_path]
        except KeyError:
            logger.debug(
                "Unable to find parent path {} of item {} in path_to_el\n{}".format(
                    parent_path, fsitem.path, pprint.pformat(path_to_el)
                )
            )
            raise
        el = etree.SubElement(
            parent_el,
            ns.metsBNS + "div",
            TYPE={"dir": "Directory"}.get(fsitem.type, "Item"),
            LABEL=basename,
        )
        if fsitem.is_empty:  # Create dmdSec for empty dirs
            if fsitem.path.startswith("objects/metadata/transfers/"):
                continue
            fsitem_path = "%SIPDirectory%" + fsitem.path
            dir_mdl = directories.get(
                fsitem_path,
                directories.get(fsitem_path.rstrip("/"), FakeDirMdl(uuid=str(uuid4()))),
            )
            dirDmdSec = getDirDmdSec(dir_mdl, fsitem_path)
            state.globalDmdSecCounter += 1
            state.dmdSecs.append(dirDmdSec)
            dir_dmd_id = "dmdSec_" + str(state.globalDmdSecCounter)
            dirDmdSec.set("ID", dir_dmd_id)
            el.set("DMDID", dir_dmd_id)
        path_to_el[fsitem.path] = el


def call(jobs):
    from optparse import OptionParser

    parser = OptionParser()
    parser.add_option(
        "-s",
        "--baseDirectoryPath",
        action="store",
        dest="baseDirectoryPath",
        default="",
    )
    # transferDirectory/
    parser.add_option(
        "-b",
        "--baseDirectoryPathString",
        action="store",
        dest="baseDirectoryPathString",
        default="SIPDirectory",
    )
    # transferUUID/sipUUID
    parser.add_option(
        "-f",
        "--fileGroupIdentifier",
        action="store",
        dest="fileGroupIdentifier",
        default="",
    )
    parser.add_option(
        "-t", "--fileGroupType", action="store", dest="fileGroupType", default="sipUUID"
    )
    parser.add_option("-x", "--xmlFile", action="store", dest="xmlFile", default="")
    parser.add_option(
        "-a", "--amdSec", action="store_true", dest="amdSec", default=False
    )
    parser.add_option(
        "-n",
        "--createNormativeStructmap",
        action="store_true",
        dest="createNormativeStructmap",
        default=False,
    )

    for job in jobs:
        with job.JobContext(logger=logger):
            try:
                opts, _ = parser.parse_args(job.args[1:])
                state = MetsState()
                baseDirectoryPath = opts.baseDirectoryPath
                XMLFile = opts.xmlFile
                baseDirectoryPathString = "%%%s%%" % (opts.baseDirectoryPathString)
                fileGroupIdentifier = opts.fileGroupIdentifier
                fileGroupType = opts.fileGroupType
                includeAmdSec = opts.amdSec
                createNormativeStructmap = opts.createNormativeStructmap

                state.CSV_METADATA = parseMetadata(job, baseDirectoryPath, state)

                baseDirectoryPath = os.path.join(baseDirectoryPath, "")
                objectsDirectoryPath = os.path.join(baseDirectoryPath, "objects")

                # Fetch any ``Directory`` objects in the database that are contained within
                # this SIP and return them as a dict from relative paths to UUIDs. (See
                # createSIPfromTransferObjects.py for the association of ``Directory``
                # objects to a ``SIP``.
                directories = {
                    d.currentlocation.rstrip("/"): d
                    for d in Directory.objects.filter(sip_id=fileGroupIdentifier).all()
                }

                state.globalStructMapCounter += 1
                structMap = etree.Element(
                    ns.metsBNS + "structMap",
                    TYPE="physical",
                    ID=f"structMap_{state.globalStructMapCounter}",
                    LABEL="Archivematica default",
                )
                sip_dir_name = os.path.basename(baseDirectoryPath.rstrip("/"))
                structMapDiv = etree.SubElement(
                    structMap, ns.metsBNS + "div", TYPE="Directory", LABEL=sip_dir_name
                )

                if createNormativeStructmap:
                    # Create the normative structmap.
                    state.globalStructMapCounter += 1
                    normativeStructMap = get_normative_structmap(
                        baseDirectoryPath, objectsDirectoryPath, directories, state
                    )
                else:
                    job.pyprint("Skipping creation of normative structmap")
                    normativeStructMap = None

                # Delete empty directories, see #8427
                for root, _, _ in os.walk(baseDirectoryPath, topdown=False):
                    try:
                        os.rmdir(root)
                        job.pyprint("Deleted empty directory", root)
                    except OSError:
                        pass

                # Get the <dmdSec> for the entire AIP; it is associated to the root
                # <mets:div> in the physical structMap.
                sip_mdl = SIP.objects.filter(uuid=fileGroupIdentifier).first()
                if sip_mdl:
                    aipDmdSec = getDirDmdSec(sip_mdl, sip_dir_name)
                    state.globalDmdSecCounter += 1
                    state.dmdSecs.append(aipDmdSec)
                    aip_dmd_id = "dmdSec_" + str(state.globalDmdSecCounter)
                    aipDmdSec.set("ID", aip_dmd_id)
                    structMapDiv.set("DMDID", aip_dmd_id)

                structMapDivObjects = createFileSec(
                    job,
                    objectsDirectoryPath,
                    structMapDiv,
                    baseDirectoryPath,
                    baseDirectoryPathString,
                    fileGroupIdentifier,
                    fileGroupType,
                    directories,
                    state,
                    includeAmdSec=includeAmdSec,
                )

                el = create_object_metadata(
                    job, structMapDivObjects, baseDirectoryPath, state
                )
                if el:
                    state.amdSecs.append(el)

                # In an AIC, the metadata dir is not inside the objects dir
                metadataDirectoryPath = os.path.join(baseDirectoryPath, "metadata")
                createFileSec(
                    job,
                    metadataDirectoryPath,
                    structMapDiv,
                    baseDirectoryPath,
                    baseDirectoryPathString,
                    fileGroupIdentifier,
                    fileGroupType,
                    directories,
                    state,
                    includeAmdSec=includeAmdSec,
                )

                fileSec = etree.Element(ns.metsBNS + "fileSec")
                for (
                    group
                ) in state.globalFileGrpsUses:  # state.globalFileGrps.itervalues():
                    grp = state.globalFileGrps[group]
                    if len(grp) > 0:
                        fileSec.append(grp)

                rootNSMap = {"mets": ns.metsNS, "xsi": ns.xsiNS, "xlink": ns.xlinkNS}
                root = etree.Element(
                    ns.metsBNS + "mets",
                    nsmap=rootNSMap,
                    attrib={
                        "{"
                        + ns.xsiNS
                        + "}schemaLocation": "http://www.loc.gov/METS/ http://www.loc.gov/standards/mets/version1121/mets.xsd"
                    },
                )
                etree.SubElement(root, ns.metsBNS + "metsHdr").set(
                    "CREATEDATE", timezone.now().strftime("%Y-%m-%dT%H:%M:%S")
                )

                dc = createDublincoreDMDSecFromDBData(
                    job,
                    SIPMetadataAppliesToType,
                    fileGroupIdentifier,
                    baseDirectoryPath,
                    state,
                )
                if dc is not None:
                    (dmdSec, ID) = dc
                    structMapDivObjects.set("DMDID", ID)
                    root.append(dmdSec)

                for dmdSec in state.dmdSecs:
                    root.append(dmdSec)

                for amdSec in state.amdSecs:
                    root.append(amdSec)

                root.append(fileSec)
                root.append(structMap)
                if normativeStructMap is not None:
                    root.append(normativeStructMap)

                for custom_structmap in include_custom_structmap(
                    job, baseDirectoryPath, state
                ):
                    root.append(custom_structmap)

                arranged_structmap = build_arranged_structmap(
                    job, structMap, fileGroupIdentifier
                )
                if arranged_structmap is not None:
                    root.append(arranged_structmap)

                printSectionCounters = True
                if printSectionCounters:
                    job.pyprint("DmdSecs:", state.globalDmdSecCounter)
                    job.pyprint("AmdSecs:", state.globalAmdSecCounter)
                    job.pyprint("TechMDs:", state.globalTechMDCounter)
                    job.pyprint("RightsMDs:", state.globalRightsMDCounter)
                    job.pyprint("DigiprovMDs:", state.globalDigiprovMDCounter)

                tree = etree.ElementTree(root)
                write_mets(tree, XMLFile)

                job.set_status(state.error_accumulator.error_count)
            except Exception as err:
                job.print_error(repr(err))
                job.print_error(traceback.format_exc())
                job.set_status(1)
