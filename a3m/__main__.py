#!/usr/bin/env python2
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "a3m.settings.common")


from a3m.server.mcp import main
main()
