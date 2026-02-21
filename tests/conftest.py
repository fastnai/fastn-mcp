"""Shared test fixtures for Fastn MCP server tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def create_test_fastn_env(tmpdir: str) -> str:
    """Create a .fastn directory with config and registry for testing.

    Returns the path to config.json.
    """
    fastn_dir = Path(tmpdir) / ".fastn"
    fastn_dir.mkdir()

    config = {
        "api_key": "test-api-key",
        "project_id": "test-project-id",
        "stage": "LIVE",
    }
    (fastn_dir / "config.json").write_text(json.dumps(config))

    registry = {
        "version": "2025.02.14",
        "connectors": {
            "slack": {
                "id": "conn_slack_001",
                "display_name": "Slack",
                "category": "messaging",
                "tools": {
                    "send_message": {
                        "actionId": "act_slack_send_message",
                        "description": "Send a message",
                    },
                },
            },
            "hubspot": {
                "id": "conn_hubspot_001",
                "display_name": "HubSpot",
                "category": "crm",
                "tools": {
                    "get_contacts": {
                        "actionId": "act_hubspot_get_contacts",
                        "description": "Get contacts",
                    },
                },
            },
        },
    }
    (fastn_dir / "registry.json").write_text(json.dumps(registry))

    return str(fastn_dir / "config.json")
