import argparse
import logging
import uuid

import pygfried
from django.db import transaction

from a3m.databaseFunctions import getUTCDate
from a3m.databaseFunctions import insertIntoEvents
from a3m.fpr.models import FormatVersion
from a3m.main.models import File
from a3m.main.models import FileFormatVersion
from a3m.main.models import FileID


logger = logging.getLogger(__name__)

TOOL_DESCRIPTION = "pygfried/siegfried"
TOOL_VERSION = pygfried.version()


def write_file_format_version(file_obj, format_version_obj):
    (ffv, created) = FileFormatVersion.objects.get_or_create(
        file_uuid=file_obj, defaults={"format_version": format_version_obj}
    )
    if not created:  # Update the version if it wasn't created new
        ffv.format_version = format_version_obj
        ffv.save()


def write_identification_event(file_uuid, puid=None, success=True):
    event_detail_text = 'program="{}"; version="{}"'.format(
        TOOL_DESCRIPTION, TOOL_VERSION
    )
    if success:
        event_outcome_text = "Positive"
    else:
        event_outcome_text = "Not identified"

    if not puid or puid == "UNKNOWN":
        puid = "No Matching Format"

    date = getUTCDate()

    insertIntoEvents(
        fileUUID=file_uuid,
        eventIdentifierUUID=str(uuid.uuid4()),
        eventType="format identification",
        eventDateTime=date,
        eventDetail=event_detail_text,
        eventOutcome=event_outcome_text,
        eventOutcomeDetailNote=puid,
    )


def write_file_id(file_id, format_version_obj):
    """
    Write the identified format to the DB.
    """
    FileID.objects.create(
        file_id=file_id,
        format_name=format_version_obj.format.description,
        format_version=format_version_obj.version or "",
        format_registry_name="PRONOM",
        format_registry_key=format_version_obj.pronom_id,
    )


def identify_file_format(file_path, file_id, disable_reidentify):
    # If reidentification is disabled and a format identification event exists for this file, exit
    file_obj = File.objects.get(uuid=file_id)
    if (
        disable_reidentify
        and file_obj.event_set.filter(event_type="format identification").exists()
    ):
        logger.debug(
            "This file has already been identified, and re-identification is disabled. Skipping."
        )
        return 0

    try:
        puid = pygfried.identify(file_path)
    except Exception as err:
        logger.error("Error running pygfried: %s", err)
        return 255

    if not puid or puid == "UNKNOWN":
        write_identification_event(file_id, success=False)
        return 255

    try:
        format_version_obj = FormatVersion.active.get(pronom_id=puid)
    except FormatVersion.DoesNotExist:
        write_identification_event(file_id, success=False)
        return 255

    write_file_format_version(file_obj, format_version_obj)
    write_identification_event(file_id, puid=puid)
    write_file_id(file_id, format_version_obj)

    return 0


def call(jobs):
    parser = argparse.ArgumentParser(description="Identify file formats.")
    parser.add_argument("file_path", type=str, help="%relativeLocation%")
    parser.add_argument("file_uuid", type=str, help="%fileUUID%")
    parser.add_argument(
        "--disable-reidentify",
        action="store_true",
        help="Disable identification if it has already happened for this file.",
    )

    with transaction.atomic():
        for job in jobs:
            with job.JobContext():
                args = parser.parse_args(job.args[1:])
                job.set_status(
                    identify_file_format(
                        args.file_path,
                        args.file_uuid,
                        args.disable_reidentify,
                    )
                )
