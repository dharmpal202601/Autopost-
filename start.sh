#!/bin/bash
# Start the app with xvfb-run to provide a virtual display for Playwright
xvfb-run --auto-servernum --server-args="-screen 0 1280x800x24" python main.py
