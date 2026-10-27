from coppermind.embeds import rewrite_embeds

FILED = "_Attachments/2026-09/SRC1-r0001-x-image-01.jpg"
IMAGES = [("SRC1", "x-image-01.jpg", FILED)]


def rewrite(body: str, images=IMAGES) -> str:
    return rewrite_embeds(body, images, attachments_folder="_Attachments")


def test_wikilink_and_markdown_embeds_name_an_artifact():
    assert rewrite("![[x-image-01.jpg]]\n![](x-image-01.jpg)\n![alt](x-image-01.jpg)") == (
        f"![[{FILED}]]\n![]({FILED})\n![alt]({FILED})"
    )


def test_alias_is_kept_and_unmatched_links_are_untouched():
    body = "![[x-image-01.jpg|300]] ![[other.jpg]] ![](y.jpg) [[x-image-01.jpg]] ![](http://h/x-image-01.jpg)"
    assert rewrite(body) == body.replace("![[x-image-01.jpg|300]]", f"![[{FILED}|300]]")


def test_no_images_leaves_body_alone():
    assert rewrite("![[x-image-01.jpg]]", []) == "![[x-image-01.jpg]]"


def test_rewriting_twice_changes_nothing():
    once = rewrite("![[x-image-01.jpg]] ![](x-image-01.jpg)")
    assert rewrite(once) == once


def test_new_revision_repoints_earlier_filed_links():
    current = [("SRC1", "x-image-01.jpg", "_Attachments/2026-09/SRC1-r0002-x-image-01.jpg")]
    assert rewrite(f"![[{FILED}]] ![]({FILED})", current) == (
        "![[_Attachments/2026-09/SRC1-r0002-x-image-01.jpg]] "
        "![](_Attachments/2026-09/SRC1-r0002-x-image-01.jpg)"
    )


def test_other_sources_filed_copies_are_not_repointed():
    other = "_Attachments/2026-09/SRC2-r0001-x-image-01.jpg"
    assert rewrite(f"![[{other}]]") == f"![[{other}]]"


def test_spaces_are_encoded_only_in_markdown_form():
    filed = "_Attachments/2026-09/SRC1-r0001-my photo.jpg"
    images = [("SRC1", "my photo.jpg", filed)]
    assert rewrite("![[my photo.jpg]]", images) == f"![[{filed}]]"
    assert (
        rewrite("![](my%20photo.jpg)", images)
        == "![](_Attachments/2026-09/SRC1-r0001-my%20photo.jpg)"
    )


def test_fenced_code_is_not_rewritten():
    body = "```\n![[x-image-01.jpg]]\n```\n![[x-image-01.jpg]]\n"
    assert rewrite(body) == f"```\n![[x-image-01.jpg]]\n```\n![[{FILED}]]\n"


def test_inline_code_spans_are_not_rewritten():
    body = "Use `![[x-image-01.jpg]]` or ``![](x-image-01.jpg)`` then ![[x-image-01.jpg]]"
    assert rewrite(body) == (
        f"Use `![[x-image-01.jpg]]` or ``![](x-image-01.jpg)`` then ![[{FILED}]]"
    )


def test_unclosed_backtick_does_not_hide_a_real_embed():
    assert rewrite("a ` lone\n\n![[x-image-01.jpg]]") == f"a ` lone\n\n![[{FILED}]]"


def test_fences_nested_in_blockquotes_and_lists_are_not_rewritten():
    body = (
        "> ```\n> ![[x-image-01.jpg]]\n> ```\n"
        "- item\n  ```md\n  ![[x-image-01.jpg]]\n  ```\n"
        "~~~\n![[x-image-01.jpg]]\n~~~\n"
        "![[x-image-01.jpg]]\n"
    )
    assert rewrite(body) == body[: body.rindex("![[")] + f"![[{FILED}]]\n"


def test_indented_code_blocks_are_not_rewritten():
    body = "text\n\n    ![[x-image-01.jpg]]\n    ![](x-image-01.jpg)\n\n![[x-image-01.jpg]]\n"
    assert rewrite(body) == body[: body.rindex("![[")] + f"![[{FILED}]]\n"


def test_list_item_content_and_continuations_are_rewritten():
    body = (
        "- item\n\n    ![[x-image-01.jpg]]\n"
        "- other\n  ![](x-image-01.jpg)\n\n  ![[x-image-01.jpg]]\n"
        "1. numbered\n\n      ![[x-image-01.jpg]]\n"
        "- lazy\ncontinued ![[x-image-01.jpg]]\n"
        "\t- tabbed ![[x-image-01.jpg]]\n"
    )
    assert rewrite(body) == body.replace("x-image-01.jpg", FILED)


def test_nested_list_embeds_are_rewritten():
    body = "- a\n  - b\n\n      ![[x-image-01.jpg]]\n    - c\n\n      ![[x-image-01.jpg]]\n"
    assert rewrite(body) == body.replace("x-image-01.jpg", FILED)


def test_indented_code_inside_a_list_item_is_not_rewritten():
    body = "- item\n\n      ![[x-image-01.jpg]]\n\n  ![[x-image-01.jpg]]\n"
    assert rewrite(body) == body[: body.rindex("![[")] + f"![[{FILED}]]\n"


def test_prose_after_a_list_ends_it():
    body = "- item\n\npara\n\n    ![[x-image-01.jpg]]\n"
    assert rewrite(body) == body


def test_markdown_titles_and_angle_destinations_are_resolved():
    assert rewrite('![photo](x-image-01.jpg "Caption")') == f'![photo]({FILED} "Caption")'
    assert rewrite("![photo](x-image-01.jpg 'Cap')") == f"![photo]({FILED} 'Cap')"
    assert rewrite("![p](<x-image-01.jpg>)") == f"![p](<{FILED}>)"
    images = [("SRC1", "my photo.jpg", "_Attachments/2026-09/SRC1-r0001-my photo.jpg")]
    assert rewrite('![p](<my photo.jpg> "T")', images) == (
        '![p](<_Attachments/2026-09/SRC1-r0001-my photo.jpg> "T")'
    )
    assert rewrite('![p](other.jpg "x-image-01.jpg")') == '![p](other.jpg "x-image-01.jpg")'


def test_balanced_parentheses_in_unbracketed_destinations():
    name = "meeting(1)-image-01.jpg"
    filed = "_Attachments/2026-09/SRC1-r0001-meeting(1)-image-01.jpg"
    images = [("SRC1", name, filed)]
    encoded = "_Attachments/2026-09/SRC1-r0001-meeting%281%29-image-01.jpg"
    assert rewrite(f"![]({name})", images) == f"![]({encoded})"
    assert rewrite(f'![p]({name} "Caption")', images) == f'![p]({encoded} "Caption")'
    assert rewrite(f"![p](<{name}>)", images) == f"![p](<{filed}>)"
    assert rewrite(f"![]({encoded})", images) == f"![]({encoded})"
    deep = [("SRC1", "a(b(c)).jpg", "_Attachments/2026-09/SRC1-r0001-a(b(c)).jpg")]
    assert rewrite("![](a(b(c)).jpg) tail", deep).endswith(".jpg) tail")
    assert "SRC1-r0001" in rewrite("![](a(b(c)).jpg) tail", deep)
    assert rewrite("![](a(b.jpg)", deep) == "![](a(b.jpg)"
