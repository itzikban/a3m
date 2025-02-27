# Generated by Django 2.2.12 on 2020-04-21 05:30
import uuid

import django.db.models.deletion
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):

    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.CreateModel(
            name="Format",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        help_text="Unique identifier",
                        unique=True,
                    ),
                ),
                (
                    "description",
                    models.CharField(
                        help_text="Common name of format",
                        max_length=128,
                        verbose_name="description",
                    ),
                ),
                ("slug", models.SlugField(unique=True, verbose_name="slug")),
            ],
            options={"verbose_name": "Format", "ordering": ["group", "description"]},
        ),
        migrations.CreateModel(
            name="FormatGroup",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        help_text="Unique identifier",
                        unique=True,
                    ),
                ),
                (
                    "description",
                    models.CharField(max_length=128, verbose_name="description"),
                ),
                ("slug", models.SlugField(unique=True, verbose_name="slug")),
            ],
            options={"verbose_name": "Format group", "ordering": ["description"]},
        ),
        migrations.CreateModel(
            name="FormatVersion",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("enabled", models.BooleanField(default=True, verbose_name="enabled")),
                (
                    "lastmodified",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="last modified"
                    ),
                ),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        help_text="Unique identifier",
                        unique=True,
                    ),
                ),
                (
                    "version",
                    models.CharField(
                        blank=True, max_length=10, null=True, verbose_name="version"
                    ),
                ),
                (
                    "pronom_id",
                    models.CharField(
                        blank=True, max_length=32, null=True, verbose_name="pronom id"
                    ),
                ),
                (
                    "description",
                    models.CharField(
                        blank=True,
                        help_text="Formal name to go in the METS file.",
                        max_length=128,
                        null=True,
                        verbose_name="description",
                    ),
                ),
                (
                    "access_format",
                    models.BooleanField(default=False, verbose_name="access format"),
                ),
                (
                    "preservation_format",
                    models.BooleanField(
                        default=False, verbose_name="preservation format"
                    ),
                ),
                ("slug", models.SlugField()),
                (
                    "format",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="version_set",
                        to="fpr.Format",
                        to_field="uuid",
                        verbose_name="the related format",
                    ),
                ),
                (
                    "replaces",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="fpr.FormatVersion",
                        to_field="uuid",
                        verbose_name="the related model",
                    ),
                ),
            ],
            options={
                "verbose_name": "Format version",
                "ordering": ["format", "description"],
            },
        ),
        migrations.CreateModel(
            name="FPCommand",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("enabled", models.BooleanField(default=True, verbose_name="enabled")),
                (
                    "lastmodified",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="last modified"
                    ),
                ),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        help_text="Unique identifier",
                        unique=True,
                    ),
                ),
                (
                    "description",
                    models.CharField(max_length=256, verbose_name="description"),
                ),
                ("command", models.TextField(verbose_name="command")),
                (
                    "script_type",
                    models.CharField(
                        choices=[
                            ("bashScript", "Bash script"),
                            ("pythonScript", "Python script"),
                            ("command", "Command line"),
                            ("as_is", "No shebang needed"),
                        ],
                        max_length=16,
                        verbose_name="script type",
                    ),
                ),
                (
                    "output_location",
                    models.TextField(
                        blank=True, null=True, verbose_name="output location"
                    ),
                ),
                (
                    "command_usage",
                    models.CharField(
                        choices=[
                            ("characterization", "Characterization"),
                            ("event_detail", "Event Detail"),
                            ("extraction", "Extraction"),
                            ("normalization", "Normalization"),
                            ("transcription", "Transcription"),
                            ("validation", "Validation"),
                            ("verification", "Verification"),
                        ],
                        max_length=16,
                        verbose_name="command usage",
                    ),
                ),
                (
                    "event_detail_command",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="fpr.FPCommand",
                        to_field="uuid",
                        verbose_name="the related event detail command",
                    ),
                ),
                (
                    "output_format",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="fpr.FormatVersion",
                        to_field="uuid",
                        verbose_name="the related output format",
                    ),
                ),
                (
                    "replaces",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="fpr.FPCommand",
                        to_field="uuid",
                        verbose_name="the related model",
                    ),
                ),
            ],
            options={
                "verbose_name": "Format policy command",
                "ordering": ["description"],
            },
        ),
        migrations.CreateModel(
            name="FPTool",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        help_text="Unique identifier",
                        unique=True,
                    ),
                ),
                (
                    "description",
                    models.CharField(
                        help_text="Name of tool",
                        max_length=256,
                        verbose_name="description",
                    ),
                ),
                ("version", models.CharField(max_length=64, verbose_name="version")),
                ("enabled", models.BooleanField(default=True, verbose_name="enabled")),
                ("slug", models.SlugField(unique=True, verbose_name="slug")),
            ],
            options={"verbose_name": "Normalization tool"},
        ),
        migrations.CreateModel(
            name="FPRule",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("enabled", models.BooleanField(default=True, verbose_name="enabled")),
                (
                    "lastmodified",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="last modified"
                    ),
                ),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        help_text="Unique identifier",
                        unique=True,
                    ),
                ),
                (
                    "purpose",
                    models.CharField(
                        choices=[
                            ("access", "Access"),
                            ("characterization", "Characterization"),
                            ("extract", "Extract"),
                            ("preservation", "Preservation"),
                            ("thumbnail", "Thumbnail"),
                            ("transcription", "Transcription"),
                            ("validation", "Validation"),
                            ("policy_check", "Validation against a policy"),
                            ("default_access", "Default access"),
                            ("default_characterization", "Default characterization"),
                            ("default_thumbnail", "Default thumbnail"),
                        ],
                        max_length=32,
                        verbose_name="purpose",
                    ),
                ),
                (
                    "command",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="fpr.FPCommand",
                        to_field="uuid",
                        verbose_name="the related command",
                    ),
                ),
                (
                    "format",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="fpr.FormatVersion",
                        to_field="uuid",
                        verbose_name="the related format",
                    ),
                ),
                (
                    "replaces",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="fpr.FPRule",
                        to_field="uuid",
                        verbose_name="the related model",
                    ),
                ),
            ],
            options={"verbose_name": "Format policy rule"},
        ),
        migrations.AddField(
            model_name="fpcommand",
            name="tool",
            field=models.ForeignKey(
                limit_choices_to={"enabled": True},
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="fpr.FPTool",
                to_field="uuid",
                verbose_name="the related tool",
            ),
        ),
        migrations.AddField(
            model_name="fpcommand",
            name="verification_command",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="fpr.FPCommand",
                to_field="uuid",
                verbose_name="the related verification command",
            ),
        ),
        migrations.AddField(
            model_name="format",
            name="group",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="fpr.FormatGroup",
                to_field="uuid",
                verbose_name="the related group",
            ),
        ),
    ]
