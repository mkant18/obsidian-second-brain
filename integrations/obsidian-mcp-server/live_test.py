"""Live MCP round-trip test for the Obsidian connector.

Drives server.py with a real MCP client over stdio - the same protocol Hermes
Agent / Claude Desktop / Cursor use - launched the same way every real client
launches it: `uv run --with 'mcp<2' python server.py` (see README.md). Proves
the connector works end-to-end without needing Hermes itself installed.

Exercises all ten registered tools: obsidian_search, obsidian_read_note,
obsidian_save_note, obsidian_capture, obsidian_update_note,
obsidian_validate_note, obsidian_backlinks, obsidian_vault_health,
obsidian_list_skills, obsidian_get_skill.

Usage:
    OBSIDIAN_VAULT_PATH=/path/to/vault uv run --with 'mcp<2' python live_test.py
    OBSIDIAN_VAULT_PATH=/path/to/vault uv run --with 'mcp<2' python live_test.py --save "query"

Without --save the run is read-only (safe against a real vault): it exercises
the seven tools that never write. With --save it also exercises the three
write tools (obsidian_save_note, obsidian_capture, obsidian_update_note),
writing a couple of test notes to the vault's Inbox/ - only pass --save
against a throwaway vault, never a real one.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = (Path(__file__).parent / "server.py").as_posix()

# The real launch path every client (README.md, Claude Desktop/Code configs)
# uses. Spawning via sys.executable directly would test a path nothing in
# production actually takes. No shell is involved in the spawn (StdioServerParameters
# execs the argv directly), so "mcp<2" is passed as a plain arg - the quotes
# in the README/usage strings above are shell syntax for a human typing the
# command, not part of the literal value.
UV_COMMAND = ["run", "--with", "mcp<2", "python", SERVER]

READ_ONLY_TOOLS = {
    "obsidian_validate_note",
    "obsidian_backlinks",
    "obsidian_vault_health",
    "obsidian_list_skills",
    "obsidian_get_skill",
    "obsidian_search",
    "obsidian_read_note",
}
WRITE_TOOLS = {"obsidian_save_note", "obsidian_capture", "obsidian_update_note"}
ALL_TOOLS = READ_ONLY_TOOLS | WRITE_TOOLS


async def main(query: str, do_save: bool) -> None:
    vault = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    if not vault:
        sys.exit("set OBSIDIAN_VAULT_PATH first")

    params = StdioServerParameters(
        command="uv",
        args=UV_COMMAND,
        env={**os.environ, "OBSIDIAN_VAULT_PATH": vault},
    )

    called: set = set()
    errors: list = []

    def record(name: str, payload, *, allow_error: bool = False) -> dict:
        called.add(name)
        if not allow_error and isinstance(payload, dict) and payload.get("error"):
            errors.append(f"{name}: {payload['error']}")
        return payload if isinstance(payload, dict) else {}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            discovered = {t.name for t in tools.tools}
            print("HANDSHAKE OK. tools:", sorted(discovered))
            missing = ALL_TOOLS - discovered
            if missing:
                errors.append(f"server did not register expected tools: {sorted(missing)}")

            saved_path = None
            if do_save:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

                sv = await session.call_tool(
                    "obsidian_save_note",
                    {
                        "title": f"MCP live test {stamp}",
                        "content": (
                            f"Written by the Obsidian MCP connector live round-trip test ({stamp})."
                        ),
                        "type": "note",
                        "tags": ["test", "mcp"],
                    },
                )
                sv_data = record("obsidian_save_note", json.loads(sv.content[0].text))
                saved_path = sv_data.get("saved")
                print("\nSAVE ->", sv_data)

                cp = await session.call_tool(
                    "obsidian_capture",
                    {"text": f"MCP live-test capture {stamp}", "tags": ["test", "mcp"]},
                )
                cp_data = record("obsidian_capture", json.loads(cp.content[0].text))
                print("\nCAPTURE ->", cp_data)

                if saved_path:
                    up = await session.call_tool(
                        "obsidian_update_note",
                        {
                            "path": saved_path,
                            "append": "Updated by the live-test round-trip.",
                            "heading": "Live test update",
                            "set_fields": {"reviewed": "true"},
                        },
                    )
                    up_data = record("obsidian_update_note", json.loads(up.content[0].text))
                    print("\nUPDATE_NOTE ->", up_data)
                else:
                    errors.append("obsidian_save_note did not return a 'saved' path; "
                                   "cannot exercise obsidian_update_note")

            search_query = query if not do_save or not saved_path else "MCP live test"
            r = await session.call_tool("obsidian_search", {"query": search_query, "limit": 3})
            search_data = record("obsidian_search", json.loads(r.content[0].text))
            results = search_data.get("results", [])
            print(f"\nSEARCH '{search_query}' -> {len(results)} hits")
            for h in results:
                print("  -", h["path"])

            read_target = saved_path or (results[0]["path"] if results else None)
            if read_target:
                rd = await session.call_tool("obsidian_read_note", {"path": read_target})
                rd_data = record("obsidian_read_note", json.loads(rd.content[0].text))
                body = rd_data.get("content", "")
                print(f"\nREAD {read_target} -> {body[:160]!r}")
            else:
                # Read-only mode against an empty/no-match vault: no note exists
                # to read. Still call the tool so its wiring is exercised; a
                # "not found" response here is the expected, correct behavior,
                # not a failure, so it does not count as an error.
                rd = await session.call_tool("obsidian_read_note", {"path": "__live_test_missing__.md"})
                record("obsidian_read_note", json.loads(rd.content[0].text), allow_error=True)

            # validate_note: on a real target if one exists, else the same
            # missing-path guard case as the read_note fallback above (still
            # exercises the tool's wiring; a "not found" response is expected).
            vn = await session.call_tool(
                "obsidian_validate_note", {"path": read_target or "__live_test_missing__.md"}
            )
            vn_data = record("obsidian_validate_note", json.loads(vn.content[0].text),
                              allow_error=read_target is None)
            print("\nVALIDATE_NOTE ->", vn_data)

            bl_target = saved_path or read_target or "README"
            bl = await session.call_tool("obsidian_backlinks", {"target": bl_target})
            bl_data = record("obsidian_backlinks", json.loads(bl.content[0].text))
            print(f"\nBACKLINKS '{bl_target}' -> {bl_data.get('count')} refs")

            vh = await session.call_tool("obsidian_vault_health", {})
            vh_data = record("obsidian_vault_health", json.loads(vh.content[0].text))
            print("\nVAULT_HEALTH ->", {k: v for k, v in vh_data.items() if k != "wanted_notes"})

            ls = await session.call_tool("obsidian_list_skills", {})
            ls_data = record("obsidian_list_skills", json.loads(ls.content[0].text))
            skills = ls_data.get("skills", [])
            print(f"\nLIST_SKILLS -> {len(skills)} skills")

            skill_name = skills[0]["name"] if skills else "obsidian-find"
            gs = await session.call_tool("obsidian_get_skill", {"name": skill_name})
            gs_data = record("obsidian_get_skill", json.loads(gs.content[0].text))
            print(f"\nGET_SKILL '{skill_name}' -> {'ok' if gs_data.get('instructions') else gs_data}")

    expected = ALL_TOOLS if do_save else READ_ONLY_TOOLS
    uncalled = expected - called
    if uncalled:
        errors.append(f"tools never called this run: {sorted(uncalled)}")

    if errors:
        print("\nFAILED:")
        for e in errors:
            print("  -", e)
        sys.exit(1)

    print(f"\nOK: {len(called)} tools exercised, 0 errors.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--save"]
    asyncio.run(main(args[0] if args else "hermes", "--save" in sys.argv))
