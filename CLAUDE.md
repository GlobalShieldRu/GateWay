# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GSG Smart Gateway is a single-page HTML dashboard for managing a VPN gateway device. The interface allows users to:
- Manage network devices and their routing modes (VPN/Smart/Bypass)
- Configure routing rules and rule-sets
- Manage DHCP and network settings
- Control proxy nodes and view system logs

The UI has two modes: "Simple" (basic device management) and "Expert" (full configuration with tabs).

## Development

This is a static HTML file using Tailwind CSS via CDN. No build step or package manager is required.

To view: Open `index.html` directly in a browser, or serve it with any static file server:
```bash
# Python
python3 -m http.server 8000

# Node.js (if npx available)
npx serve .
```

## Architecture

- Single-file application (`index.html`) with inline CSS and JavaScript
- Tailwind CSS loaded from CDN for styling
- UI state managed via vanilla JavaScript (mode toggle, tab switching)
- All text is in Russian (target audience)