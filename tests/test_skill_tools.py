# tests/test_skill_tools.py
import json

import pytest

from astra.tools.skill_tools.skills import configure_skills_dir, create_skill, list_skills, view_skill


def test_create_list_view_roundtrip(tmp_path):
    configure_skills_dir(tmp_path)

    r = json.loads(create_skill({"name": "Refund Policy", "description": "How to handle refunds", "content": "Always refund within 30 days."}))
    assert r["status"] == "created"
    assert r["name"] == "refund-policy"

    listed = json.loads(list_skills({}))
    assert len(listed) == 1
    assert listed[0]["name"] == "refund-policy"
    assert listed[0]["description"] == "How to handle refunds"

    viewed = json.loads(view_skill({"skill_name": "Refund Policy"}))  # original casing, gets slugified
    assert viewed["name"] == "refund-policy"
    assert "30 days" in viewed["content"]


def test_create_duplicate_skill_rejected(tmp_path):
    configure_skills_dir(tmp_path)
    create_skill({"name": "dup", "description": "d", "content": "c"})
    r = json.loads(create_skill({"name": "dup", "description": "d2", "content": "c2"}))
    assert r.get("error_type") == "already_exists"


def test_view_missing_skill_returns_error(tmp_path):
    configure_skills_dir(tmp_path)
    r = json.loads(view_skill({"skill_name": "does-not-exist"}))
    assert "error" in r


def test_create_skill_missing_fields_rejected(tmp_path):
    configure_skills_dir(tmp_path)
    assert "error" in json.loads(create_skill({"description": "d", "content": "c"}))
    assert "error" in json.loads(create_skill({"name": "x", "content": "c"}))


def test_list_skills_respects_limit(tmp_path):
    configure_skills_dir(tmp_path)
    for i in range(5):
        create_skill({"name": f"skill-{i}", "description": f"desc {i}", "content": "x"})
    listed = json.loads(list_skills({"limit": 2}))
    assert len(listed) == 2


def test_skill_name_slugification(tmp_path):
    configure_skills_dir(tmp_path)
    r = json.loads(create_skill({"name": "Weird Name!! With Spaces", "description": "d", "content": "c"}))
    assert r["status"] == "created"
    assert " " not in r["name"]
    assert "!" not in r["name"]
