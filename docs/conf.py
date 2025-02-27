import os
import sys
from datetime import datetime

sys.path.append(os.path.abspath("../"))

from a3m import __version__
from a3m.cli.common import init_django

init_django()

needs_sphinx = "3.2"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinxcontrib.mermaid",
    # Temporarily disabled.
    # "releases",
]

autoclass_content = "both"
autodoc_member_order = "bysource"
source_suffix = ".rst"
master_doc = "index"
project = "a3m"
author = "%d Artefactual Systems Inc." % datetime.now().year

version = f"v{__version__}"
release = f"v{__version__}"

language = None
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
pygments_style = "sphinx"
todo_include_todos = True

html_theme = "alabaster"
html_theme_options = {
    "description": "Lightweight Archivematica",
    "fixed_sidebar": True,
    "github_user": "artefactual-labs",
    "github_repo": "a3m",
    "github_banner": True,
    "github_button": False,
}
html_static_path = ["_static"]
htmlhelp_basename = "a3mdoc"

suppress_warnings = ["image.nonlocal_uri"]

releases_github_path = "artefactual-labs/a3m"

mermaid_version = "8.8.2"
