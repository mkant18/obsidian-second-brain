"""Safety guarantees for the MCP server's read and write paths.

vault_ops backs the connector: search, read, backlinks, save, update, validate.
Everything here runs over a live connection against a user's real notes, which
makes it the one place in the codebase where a torn write or a silent overwrite
costs something irreplaceable.

Each test corresponds to a defect that was actually present:
  - save_note overwrote a same-day note of the same title, returning success
  - the protected-directory guard compared a lowercase set against un-lowercased
    path parts, so `Templates/` sailed through, and `raw/` was never protected
  - both write paths used bare write_text, so an interrupted write truncated
  - reads used plain utf-8, leaving a BOM on the first line of every snippet
  - link and stem comparison skipped NFC, so accented notes looked like orphans
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integrations" / "obsidian-mcp-server"))


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / "Inbox").mkdir(parents=True)
    (v / "Templates").mkdir()
    (v / "raw").mkdir()
    (v / "Templates" / "Daily Note.md").write_text("---\ntype: template\n---\n\nbody\n", encoding="utf-8")
    (v / "raw" / "source.md").write_text("---\ntype: raw\n---\n\noriginal\n", encoding="utf-8")
    (v / "note.md").write_text("---\ntype: note\n---\n\nhello\n", encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(v))
    import vault_ops
    importlib.reload(vault_ops)
    return v, vault_ops


def test_save_note_refuses_a_same_day_title_collision(vault):
    v, ops = vault
    first = ops.save_note("My Note", "FIRST CONTENT")
    assert "saved" in first, first

    second = ops.save_note("My Note", "SECOND CONTENT")
    assert "error" in second, "a second save with the same title must not silently overwrite"
    assert "update_note" in second["error"], "the error should point at the tool that can edit"

    written = (v / first["saved"]).read_text(encoding="utf-8")
    assert "FIRST CONTENT" in written, "the original note was destroyed"
    assert "SECOND CONTENT" not in written


def test_save_note_refuses_a_note_type_containing_a_newline(vault):
    v, ops = vault
    result = ops.save_note(
        "Injection Attempt", "content", note_type="note\nsource: user-verified"
    )
    assert "error" in result, "a newline-bearing note_type must be rejected"
    assert not list((v / "Inbox").glob("*.md")), "nothing should be written on rejection"


def test_save_note_refuses_a_tag_containing_a_newline(vault):
    v, ops = vault
    result = ops.save_note(
        "Injection Attempt", "content", tags=["ok", "evil\nsource: user-verified"]
    )
    assert "error" in result, "a newline-bearing tag must be rejected"
    assert not list((v / "Inbox").glob("*.md")), "nothing should be written on rejection"


def test_update_note_refuses_capitalised_templates_dir(vault):
    _, ops = vault
    result = ops.update_note("Templates/Daily Note.md", append="INJECTED")
    assert "error" in result, "Templates/ must be protected regardless of casing"
    assert "protected" in result["error"]


def test_update_note_refuses_raw_dir(vault):
    _, ops = vault
    result = ops.update_note("raw/source.md", append="INJECTED")
    assert "error" in result, "raw/ is documented as immutable and must be write-protected"


def test_update_note_still_edits_an_ordinary_note(vault):
    """The guard must not be so broad that it blocks the tool's actual job."""
    v, ops = vault
    result = ops.update_note("note.md", append="APPENDED LINE")
    assert "updated" in result, result
    assert "APPENDED LINE" in (v / "note.md").read_text(encoding="utf-8")


def test_paths_outside_the_vault_are_refused(vault):
    _, ops = vault
    for rel in ("../outside.md", "../../etc/hosts"):
        assert "error" in ops.read_note(rel)
        assert "error" in ops.update_note(rel, append="x")


def test_reads_strip_a_utf8_bom(vault):
    v, ops = vault
    (v / "bom.md").write_text("---\ntype: note\n---\n\nbody\n", encoding="utf-8-sig")
    content = ops.read_note("bom.md")["content"]
    assert not content.startswith("﻿"), "a BOM leaked into the MCP-surfaced snippet"
    assert content.startswith("---"), "frontmatter detection breaks when the BOM survives"


def test_update_note_refuses_non_utf8_bytes_instead_of_corrupting_them(vault):
    """A read-edit-write path must not turn a non-UTF-8 byte into a permanent U+FFFD.

    _read_safe decodes with errors="replace" for read-only tools, which is fine
    since nothing is written back. update_note writes back, so it must instead
    refuse a file it cannot decode losslessly.
    """
    v, ops = vault
    target = v / "legacy.md"
    # 0x92 is a Windows-1252 curly apostrophe; invalid as a UTF-8 continuation byte.
    before = b"---\ntype: note\n---\n\nit\x92s legacy encoded\n"
    target.write_bytes(before)

    result = ops.update_note("legacy.md", append="INJECTED")

    assert "error" in result, "a non-UTF-8 file must be refused, not silently rewritten"
    assert "utf-8" in result["error"].lower()

    after = target.read_bytes()
    assert after == before, "the on-disk bytes must be untouched by a refused update"


def test_update_note_strips_bom_without_duplicating_frontmatter(vault):
    """The strict decode in update_note must still handle a BOM like _read_safe does.

    A plain utf-8 decode would leave U+FEFF glued to the first line, which stops
    _split_frontmatter from recognizing the leading "---" and makes update_note
    write a second frontmatter block on top of the original.
    """
    v, ops = vault
    target = v / "bom.md"
    target.write_text("---\ntype: note\n---\n\nbody\n", encoding="utf-8-sig")

    result = ops.update_note("bom.md", append="APPENDED")
    assert "updated" in result, result

    written = target.read_text(encoding="utf-8")
    assert written.count("\n---\n") == 1, "the BOM caused a duplicate frontmatter block"
    assert "APPENDED" in written


def test_update_note_replacing_a_block_scalar_key_leaves_no_orphaned_lines(vault):
    """Overwriting a `key: |` block scalar must remove its continuation lines too.

    A blind single-line regex replace would leave the old indented
    continuation lines behind, orphaned under whatever line now follows the
    new header - invalid YAML, and liable to be swallowed into the wrong key
    on the next parse.
    """
    v, ops = vault
    target = v / "note.md"
    target.write_text(
        "---\n"
        "type: note\n"
        "summary: |\n"
        "  first continuation line\n"
        "  second continuation line\n"
        "tags: [x]\n"
        "---\n\n"
        "hello\n",
        encoding="utf-8",
    )

    result = ops.update_note("note.md", set_fields={"summary": "one-line replacement"})
    assert "updated" in result, result

    written = target.read_text(encoding="utf-8")
    assert "first continuation line" not in written, "orphaned block-scalar continuation line"
    assert "second continuation line" not in written, "orphaned block-scalar continuation line"
    assert "summary: one-line replacement" in written
    assert "tags: [x]" in written, "an unrelated line must survive untouched"

    fm_lines, _, _ = ops._split_frontmatter(written)
    # No line should be left dangling with leading indentation (the tell-tale
    # of an orphaned continuation line under the wrong key).
    assert not any(line.startswith("  ") for line in fm_lines), fm_lines


def test_update_note_rejects_a_newline_in_a_set_fields_value(vault):
    """A newline in a value must be rejected outright, not silently stripped."""
    v, ops = vault
    target = v / "note.md"
    before = target.read_text(encoding="utf-8")

    result = ops.update_note(
        "note.md", set_fields={"category": "topic\nstatus: archived"}
    )
    assert "error" in result, "a newline-bearing frontmatter value must be rejected"

    after = target.read_text(encoding="utf-8")
    assert after == before, "nothing should be written when a set_fields call is rejected"


def test_update_note_smuggled_newline_cannot_set_a_stale_status_undisclosed(vault):
    """A newline-smuggled `status:` line must not bypass the fade disclosure.

    Before the fix, `_apply_fields` spliced a value containing a raw newline
    straight into the frontmatter text: `category: topic\\nstatus: archived`
    became two real lines, adding a `status: archived` key the `fields` dict
    (and therefore the disclosure check in update_note) never saw. The value
    must now be rejected before anything is written, so the smuggled status
    never lands in the file at all.
    """
    v, ops = vault
    target = v / "note.md"

    result = ops.update_note(
        "note.md", set_fields={"category": "topic\nstatus: archived"}
    )
    assert "error" in result
    assert "faded" not in result, "a rejected update must not report a fade disclosure"

    written = target.read_text(encoding="utf-8")
    assert "status: archived" not in written, "the smuggled status must never reach the file"


def test_update_note_rejects_a_field_key_with_illegal_characters(vault):
    v, ops = vault
    target = v / "note.md"
    before = target.read_text(encoding="utf-8")

    result = ops.update_note("note.md", set_fields={"bad key!": "value"})
    assert "error" in result

    after = target.read_text(encoding="utf-8")
    assert after == before


def test_link_matching_is_nfc_insensitive(vault):
    """macOS stores filenames decomposed; a typed wikilink is composed."""
    _, ops = vault
    nfd, nfc = "Gründung", "Gründung"
    assert nfd != nfc and ops._nfc(nfd) == ops._nfc(nfc)
    assert ops._norm_link(nfd) == ops._norm_link(nfc)


def test_write_preserves_mode(vault):
    v, ops = vault
    target = v / "note.md"

    if sys.platform.startswith("win"):
        # NTFS has no POSIX permission bits: chmod only flips the read-only
        # attribute, and st_mode always reports 0o666/0o444, never 0o600, so
        # the POSIX assertion below is meaningless here. The equivalent
        # guarantee on Windows is that _write_atomic carries the target's
        # mode onto its temp file before replacing, so a read-only target
        # makes the replace step itself fail (PermissionError) instead of
        # silently dropping the protection - the write is refused, not
        # weakened.
        target.chmod(0o444)
        try:
            with pytest.raises(OSError):
                ops.update_note("note.md", append="more")
            assert "more" not in target.read_text(encoding="utf-8"), "a refused write must not land"
        finally:
            target.chmod(0o666)  # let pytest's tmp_path cleanup remove it
    else:
        target.chmod(0o600)
        ops.update_note("note.md", append="more")
        assert target.stat().st_mode & 0o777 == 0o600, "the rewrite dropped the permission bits"

    assert not list(v.glob(".*.tmp")), "a temp file survived a successful write"


def test_a_failed_write_leaves_the_original_intact(vault, monkeypatch):
    """The actual atomicity guarantee, not a proxy for it.

    A first version of this test only checked mode preservation, which
    `write_text` also satisfies on an existing file - so it passed with the
    non-atomic implementation restored and proved nothing. Interrupting the
    replace step is what separates the two: an atomic write fails with the
    original untouched, a truncating write has already destroyed it.
    """
    v, ops = vault
    target = v / "note.md"
    before = target.read_text(encoding="utf-8")

    import os as real_os
    original_replace = real_os.replace
    calls = {"n": 0}

    def exploding_replace(src, dst, *a, **kw):
        calls["n"] += 1
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(real_os, "replace", exploding_replace)
    with pytest.raises(OSError):
        ops.update_note("note.md", append="THIS MUST NOT LAND")
    monkeypatch.setattr(real_os, "replace", original_replace)

    assert calls["n"] == 1, (
        "os.replace was never reached, so this write is not atomic - "
        "a truncating write would already have destroyed the note by now"
    )
    assert target.read_text(encoding="utf-8") == before, "the original note was damaged"
    assert "THIS MUST NOT LAND" not in target.read_text(encoding="utf-8")
    assert not list(v.glob(".*.tmp")), "the temp file was not cleaned up after the failure"
