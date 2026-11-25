"""Check local case selection, validity, and read-only lookup."""

import pytest

from server.agent.cases import CaseStore


def test_lookup_returns_two_current_cases_in_request_order(tmp_path, write_case):
    write_case(tmp_path, "user.first", rules={"official/preload-cache": "a" * 64})
    write_case(tmp_path, "user.second")
    store = CaseStore.load(None, tmp_path, {"official/preload-cache": "a" * 64})
    ids = [row["id"] for row in store.index()]
    assert ids == ["user.first", "user.second"]
    reply = store.tool().invoke({"ids": list(reversed(ids))})
    assert [case["id"] for case in reply["cases"]] == list(reversed(ids))
    assert all(case["handling"] for case in reply["cases"])
    assert reply["cases"][0]["evidence"] == ["Two reads differed"]
    assert reply["unavailable"] == []


def test_long_case_summary_and_body_are_available(tmp_path, write_case):
    symptom = "Long symptom description " * 10
    handling = "Check the timestamp and scope of each read. " * 100
    write_case(tmp_path, "user.long", when=symptom.strip(), handling=handling.strip())

    store = CaseStore.load(None, tmp_path, {})

    assert store.index() == [{"id": "user.long", "when": symptom.strip()}]
    (case,) = store.tool().invoke({"ids": ["user.long"]})["cases"]
    assert case["handling"] == handling.strip()


def test_missing_and_outdated_cases_do_not_block_valid_case(tmp_path, write_case):
    write_case(tmp_path, "user.healthy")
    write_case(tmp_path, "user.old", rules={"user/preload": "a" * 64})
    store = CaseStore.load(None, tmp_path, {"user/preload": "b" * 64})
    assert [summary["id"] for summary in store.index()] == ["user.healthy"]
    reply = store.tool().invoke({"ids": ["user.old", "user.healthy"]})
    assert [case["id"] for case in reply["cases"]] == ["user.healthy"]
    assert reply["unavailable"] == [{"id": "user.old", "code": "outdated"}]
    assert store.tool().invoke({"ids": ["user.missing"]})["unavailable"] == [
        {"id": "user.missing", "code": "not_found"},
    ]


def test_invalid_files_links_and_duplicate_declarations(tmp_path, write_case):
    official, user = tmp_path / "official", tmp_path / "user"
    write_case(official, "builtin.shared")
    write_case(user, "builtin.shared", filename="user.shadow.json")
    outside = write_case(tmp_path, "user.link")
    (user / "_cases" / "user.link.json").symlink_to(outside)
    write_case(user, "user.broken").write_text("{", encoding="utf-8")
    write_case(user, "user.valid")

    store = CaseStore.load(official, user, {})
    assert [summary["id"] for summary in store.index()] == ["user.valid"]
    for identifier, code in (
        ("builtin.shared", "duplicate"), ("user.shadow", "invalid"), ("user.link", "invalid"),
        ("user.broken", "invalid"),
    ):
        assert store.tool().invoke({"ids": [identifier]})["unavailable"] == [
            {"id": identifier, "code": code},
        ]


def test_case_directory_symlink_is_ignored(tmp_path, write_case):
    write_case(tmp_path, "user.link")
    user = tmp_path / "user"
    user.mkdir()
    (user / "_cases").symlink_to(tmp_path / "_cases", target_is_directory=True)
    assert CaseStore.load(None, user, {}).index() == []


@pytest.mark.parametrize("args", [
    {"ids": []},
    {"ids": ["user.one", "user.two", "user.three"]},
    {"ids": ["user.one", "user.one"]},
    {"ids": ["../../secret"]},
    {"ids": ["user.one"], "path": "/secret"},
])
def test_case_tool_rejects_invalid_requests(args):
    with pytest.raises(ValueError):
        CaseStore.load(None, None, {}).tool().invoke(args)


def test_round_uses_one_case_snapshot(tmp_path, write_case):
    write_case(tmp_path, "user.example", when="Original symptom")
    store = CaseStore.load(None, tmp_path, {})

    write_case(tmp_path, "user.example", when="New symptom")
    assert store.index()[0]["when"] == "Original symptom"
    assert store.tool().invoke({"ids": ["user.example"]})["cases"][0]["when"] == "Original symptom"
    assert CaseStore.load(None, tmp_path, {}).index()[0]["when"] == "New symptom"
